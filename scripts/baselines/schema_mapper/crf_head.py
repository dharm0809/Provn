"""Sibling-aware CRF over per-field logits within ONE JSON parent object.

Architecture (SATO VLDB 2020):
- Treat fields with the SAME parent_object_path as one "sequence" of
  the CRF (ordered alphabetically by leaf path within the group).
- 20×20 learned transition matrix (CANONICAL_LABELS).
- COOCCUR_BIAS positive priors are added to transitions at init time
  to bias the model toward known canonical co-occurrences (e.g.
  prompt_tokens + completion_tokens, content + thinking_content on
  reasoners). Data refines from there.
- EXCLUSIVE_GROUPS is enforced as a Viterbi-time mask: within one
  parent group, no two fields may share the same label that is part
  of an exclusive group (e.g. one parent dict has ONE prompt_tokens
  leaf, not two — same logic for tool_call_id, tool_call_name, etc.).

Why CRF runs in numpy at inference, not in ONNX:
  onnxruntime has no first-class Viterbi decoder. Encoder is exported
  as ONNX FP32→INT8; CRF transitions + start/end log-probs are dumped
  to schema_mapper_crf.npz; runtime gateway code (mapper.py) reads
  the npz and runs Viterbi in pure numpy. Inference cost is O(N·K²)
  where N=number of fields per dict and K=20 — well under 1ms on CPU.
"""
from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn
from torchcrf import CRF

from canonical_schema import (
    CANONICAL_LABELS,
    COOCCUR_BIAS,
    EXCLUSIVE_GROUPS,
    LABEL_TO_ID,
)

NUM_LABELS = len(CANONICAL_LABELS)


def _initial_transitions(strength: float = 0.5) -> torch.Tensor:
    """Build the initial transition matrix from COOCCUR_BIAS.

    Symmetric — co-occurrence is a parent-level signal, not directional.
    """
    T = torch.zeros(NUM_LABELS, NUM_LABELS)
    for a, b, w in COOCCUR_BIAS:
        ia, ib = LABEL_TO_ID[a], LABEL_TO_ID[b]
        T[ia, ib] += w * strength
        T[ib, ia] += w * strength
    return T


def _exclusive_label_ids() -> list[set[int]]:
    """List of label-id sets, one per EXCLUSIVE_GROUPS entry."""
    return [set(LABEL_TO_ID[m] for m in members) for members in EXCLUSIVE_GROUPS.values()]


class FieldCRF(nn.Module):
    """Sibling-aware CRF wrapper around pytorch-crf's CRF layer.

    Forward (training):
        nll = self(logits, labels, mask) → scalar negative log likelihood
    Decode (inference):
        labels = self.decode(logits, mask) → list[list[int]]
            (one int label per field, masked positions ignored)
    """

    def __init__(self) -> None:
        super().__init__()
        self.crf = CRF(num_tags=NUM_LABELS, batch_first=True)
        # Seed transitions from COOCCUR_BIAS
        with torch.no_grad():
            self.crf.transitions.copy_(_initial_transitions())
        self._exclusive_groups = _exclusive_label_ids()

    def forward(self, logits: torch.Tensor, labels: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        """Returns scalar NLL (sum over batch, normalised by mask sum)."""
        return -self.crf(logits, labels, mask=mask, reduction="mean")

    def decode(self, logits: torch.Tensor, mask: torch.Tensor) -> list[list[int]]:
        """Viterbi decode + apply EXCLUSIVE_GROUPS post-hoc.

        For each parent-group sequence, if Viterbi assigned the same
        label twice within an exclusive group (e.g. two prompt_tokens),
        the lower-confidence sibling is demoted to UNKNOWN.
        """
        decoded = self.crf.decode(logits, mask=mask)
        cleaned: list[list[int]] = []
        for seq, seq_logits, seq_mask in zip(decoded, logits, mask):
            cleaned.append(self._enforce_exclusion(seq, seq_logits, seq_mask))
        return cleaned

    def _enforce_exclusion(
        self,
        seq: list[int],
        seq_logits: torch.Tensor,
        seq_mask: torch.Tensor,
    ) -> list[int]:
        # For each exclusive group, find duplicates and keep only the
        # highest-logit position; demote the rest to UNKNOWN (id 0).
        unk_id = LABEL_TO_ID["UNKNOWN"]
        out = list(seq)
        for group in self._exclusive_groups:
            for label_id in group:
                positions = [i for i, l in enumerate(out) if l == label_id and bool(seq_mask[i])]
                if len(positions) <= 1:
                    continue
                # Keep the position with the largest logit at this label
                best = max(positions, key=lambda i: float(seq_logits[i, label_id]))
                for p in positions:
                    if p != best:
                        out[p] = unk_id
        return out

    # ── ONNX-export helpers ────────────────────────────────────────────────

    def export_params(self) -> dict[str, np.ndarray]:
        """Serialize CRF params to numpy for runtime numpy Viterbi."""
        return {
            "transitions": self.crf.transitions.detach().cpu().numpy(),
            "start_transitions": self.crf.start_transitions.detach().cpu().numpy(),
            "end_transitions": self.crf.end_transitions.detach().cpu().numpy(),
            "labels": np.array(CANONICAL_LABELS, dtype=object),
        }
