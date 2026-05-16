"""End-to-end schema-mapper model.

Forward pass:
  1. linearize(FlatField) -> str               [done outside the module]
  2. tokenizer(strs) -> input_ids, attn_mask   [done outside the module]
  3. encoder(input_ids, attn_mask) -> [N, 384]  CLS-pooled embeddings
  4. concat with engineered_features [N, 167] -> [N, 551]
  5. mlp_head([N, 551]) -> [N, 20]              logits (CANONICAL_LABELS)
  6. crf(logits, parent_group_mask) -> [N]      Viterbi-decoded labels

Training loss (composed):
  total = ce(logits, gold)
        + λ_crf * crf_nll
        + λ_aux * sibling_relation_loss          (DODUO multi-task; +1.2 F1)

  λ_crf = 0.3, λ_aux = 0.1 (initial; tune on val)

Sibling-relation auxiliary head:
  Takes pairs of field embeddings (i, j) where i, j share the same parent
  object, predicts one of {same_kind, related, unrelated}. Synthetic
  supervision derived from the provider specs:
    same_kind   = labels are in the same EXCLUSIVE_GROUPS bucket
                  (e.g. both tool_call_*; both *_tokens)
    related     = labels co-occur in COOCCUR_BIAS
    unrelated   = otherwise
  Trained jointly with the primary classification objective.
"""
from __future__ import annotations

from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from canonical_schema import (
    CANONICAL_LABELS,
    COOCCUR_BIAS,
    EXCLUSIVE_GROUPS,
    LABEL_TO_ID,
)
from crf_head import NUM_LABELS, FieldCRF
from encoder import ENCODER_HIDDEN, FieldEncoder

FEATURE_DIM_V2_DEFAULT = 167  # matches src/gateway/schema/features.py:FEATURE_DIM_V2
SIBLING_RELATION_CLASSES = 3  # same_kind / related / unrelated


class SchemaMapper(nn.Module):
    """Transformer + features + CRF, with auxiliary sibling-relation head."""

    def __init__(
        self,
        encoder_pretrained: str | None = None,
        feature_dim: int = FEATURE_DIM_V2_DEFAULT,
        head_hidden: int = 256,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        if encoder_pretrained is not None:
            self.encoder = FieldEncoder(pretrained_name=encoder_pretrained)
        else:
            self.encoder = FieldEncoder()
        enc_dim = self.encoder.hidden_dim
        self.feature_dim = feature_dim
        joined_dim = enc_dim + feature_dim
        self.head = nn.Sequential(
            nn.Linear(joined_dim, head_hidden),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(head_hidden, NUM_LABELS),
        )
        # Sibling-relation auxiliary: takes a pair of [enc_dim] embeddings,
        # concatenates difference + product (Sentence-BERT trick), classifies.
        self.sibling_head = nn.Sequential(
            nn.Linear(enc_dim * 4, head_hidden),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(head_hidden, SIBLING_RELATION_CLASSES),
        )
        self.crf = FieldCRF()

    # ── Forward ────────────────────────────────────────────────────────────

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        features: torch.Tensor,
    ) -> dict[str, torch.Tensor]:
        """Single-field forward (no CRF grouping yet — that's the caller's job).

        Args:
            input_ids:      [B*N, T] tokenized linearised strings
            attention_mask: [B*N, T]
            features:       [B*N, F] engineered features (FEATURE_DIM_V2)
        Returns:
            {"embeddings": [B*N, enc_dim], "logits": [B*N, NUM_LABELS]}
        """
        emb = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        joined = torch.cat([emb, features], dim=-1)
        logits = self.head(joined)
        return {"embeddings": emb, "logits": logits}

    # ── Loss composition (training only) ───────────────────────────────────

    def compute_loss(
        self,
        logits_grouped: torch.Tensor,        # [B, N, NUM_LABELS]
        labels_grouped: torch.Tensor,        # [B, N]
        mask: torch.Tensor,                  # [B, N] bool
        sibling_pairs: torch.Tensor | None,  # [P, 2*enc_dim*2] or None
        sibling_labels: torch.Tensor | None, # [P]
        lambda_crf: float = 0.3,
        lambda_aux: float = 0.1,
    ) -> dict[str, torch.Tensor]:
        # Primary CE — flatten and mask out pads
        flat_logits = logits_grouped.reshape(-1, NUM_LABELS)
        flat_labels = labels_grouped.reshape(-1)
        flat_mask = mask.reshape(-1)
        ce = F.cross_entropy(flat_logits[flat_mask], flat_labels[flat_mask])

        # Structured CRF NLL
        crf_nll = self.crf(logits_grouped, labels_grouped, mask)

        # Auxiliary sibling relation
        if sibling_pairs is not None and sibling_labels is not None and sibling_pairs.numel() > 0:
            aux_logits = self.sibling_head(sibling_pairs)
            aux_loss = F.cross_entropy(aux_logits, sibling_labels)
        else:
            aux_loss = torch.zeros((), device=ce.device)

        total = ce + lambda_crf * crf_nll + lambda_aux * aux_loss
        return {
            "total": total,
            "ce": ce.detach(),
            "crf_nll": crf_nll.detach(),
            "aux": aux_loss.detach(),
        }

    # ── Decode (inference) ─────────────────────────────────────────────────

    def decode(
        self,
        logits_grouped: torch.Tensor,
        mask: torch.Tensor,
    ) -> list[list[int]]:
        """Viterbi-decode per parent group, with EXCLUSIVE_GROUPS enforcement."""
        return self.crf.decode(logits_grouped, mask)

    # ── Sibling-pair embedding builder (Sentence-BERT-style) ───────────────

    @staticmethod
    def make_sibling_pair_features(emb_a: torch.Tensor, emb_b: torch.Tensor) -> torch.Tensor:
        """Concat [a, b, |a-b|, a*b] for the auxiliary classifier."""
        return torch.cat([emb_a, emb_b, torch.abs(emb_a - emb_b), emb_a * emb_b], dim=-1)

    @staticmethod
    def sibling_relation_label(label_a: int, label_b: int) -> int:
        """Map a pair of canonical labels to {0=same_kind, 1=related, 2=unrelated}."""
        if label_a == label_b:
            return 0
        # Same EXCLUSIVE_GROUPS bucket → same_kind
        for members in EXCLUSIVE_GROUPS.values():
            ids = {LABEL_TO_ID[m] for m in members}
            if label_a in ids and label_b in ids:
                return 0
        # COOCCUR_BIAS → related
        for a, b, _ in COOCCUR_BIAS:
            if {LABEL_TO_ID[a], LABEL_TO_ID[b]} == {label_a, label_b}:
                return 1
        return 2
