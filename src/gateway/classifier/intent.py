"""Intent classifier for the gateway pipeline.

Two-tier architecture:
  Tier 1 — Deterministic rules (100% accurate, <0.1ms, covers ~70% of requests)
  Tier 2 — ONNX model (93%+ accurate, ~5ms, covers remaining 30%)

When ONNX confidence is below threshold, defaults to "normal" (safe transparent proxy).
Every decision is logged with confidence for audit trail.
"""

from __future__ import annotations

import logging
import os
import json as _json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gateway.intelligence.verdict_buffer import VerdictBuffer

logger = logging.getLogger(__name__)

# Intent labels
NORMAL = "normal"
WEB_SEARCH = "web_search"
RAG = "rag"
MCP_TOOLS = "mcp_tools"
REASONING = "reasoning"
SYSTEM_TASK = "system_task"

ALL_INTENTS = (NORMAL, WEB_SEARCH, RAG, MCP_TOOLS, REASONING, SYSTEM_TASK)

# Confidence thresholds
CONFIDENCE_AUTO = 0.95      # auto-accept ML prediction
CONFIDENCE_FLAG = 0.70      # accept but flag for review
# below CONFIDENCE_FLAG → default to NORMAL


@dataclass(frozen=True)
class IntentResult:
    """Classification result with confidence and audit metadata."""
    intent: str
    confidence: float
    tier: str              # "deterministic" or "ml"
    reason: str            # human-readable explanation


class IntentClassifier:
    """Two-tier intent classifier for the gateway pipeline."""

    def __init__(
        self,
        onnx_model_path: str | None = None,
        has_mcp_tools: bool = False,
        verdict_buffer: "VerdictBuffer | None" = None,
    ):
        self._has_mcp_tools = has_mcp_tools
        self._onnx_session = None
        self._onnx_tokenizer = None
        self._label_map: dict[int, str] = {}
        self._verdict_buffer = verdict_buffer

        # Try loading ONNX model
        if onnx_model_path and Path(onnx_model_path).exists():
            try:
                self._load_onnx(onnx_model_path)
                logger.info("Intent classifier: ONNX model loaded from %s", onnx_model_path)
            except Exception as e:
                logger.warning("Intent classifier: ONNX load failed, using rules only: %s", e)

        if not self._onnx_session:
            logger.info("Intent classifier: deterministic rules only (no ML model)")

    def classify(
        self,
        prompt: str,
        metadata: dict[str, Any],
        model_id: str,
    ) -> IntentResult:
        """Classify a request into an intent.

        Tier 1 checks are deterministic (100% accurate).
        Tier 2 ML runs only when Tier 1 doesn't match.
        """
        # ── Tier 1: Deterministic rules ───────────────────────────────
        tier1 = self._tier1_deterministic(prompt, metadata, model_id)
        if tier1 is not None:
            result = tier1
        elif self._onnx_session:
            # ── Tier 2: ML classification ─────────────────────────────
            result = self._tier2_onnx(prompt)
        else:
            # No ML model → safe default
            result = IntentResult(
                intent=NORMAL, confidence=0.5,
                tier="deterministic", reason="no_ml_model_default",
            )

        # Phase 25: record verdict for self-learning (observational only).
        # Never allowed to break inference — wrap the whole stanza defensively.
        if self._verdict_buffer is not None:
            try:
                from gateway.intelligence.types import ModelVerdict
                self._verdict_buffer.record(
                    ModelVerdict.from_inference(
                        model_name="intent",
                        input_text=prompt,
                        prediction=result.intent,
                        confidence=float(result.confidence),
                        request_id=None,
                    )
                )
            except Exception:
                logger.debug("verdict recording failed", exc_info=True)

        return result

    def _tier1_deterministic(
        self, prompt: str, metadata: dict, model_id: str,
    ) -> IntentResult | None:
        """100% accurate deterministic checks based on explicit signals."""

        # 1. System task — OpenWebUI auto-generated
        request_type = metadata.get("request_type", "")
        if isinstance(request_type, str) and request_type.startswith("system_task"):
            return IntentResult(
                intent=SYSTEM_TASK, confidence=1.0,
                tier="deterministic", reason=f"request_type={request_type}",
            )
        if prompt.lstrip().startswith("### Task:"):
            return IntentResult(
                intent=SYSTEM_TASK, confidence=1.0,
                tier="deterministic", reason="prompt_starts_with_task",
            )

        # 2. Reasoning model — o1/o3/o4
        if model_id and any(
            model_id.startswith(p) for p in ("o1-", "o1", "o3-", "o3", "o4-", "o4")
        ):
            return IntentResult(
                intent=REASONING, confidence=1.0,
                tier="deterministic", reason=f"reasoning_model={model_id}",
            )

        # 3. Web search — user explicitly toggled in OpenWebUI
        body_meta = metadata.get("_body_metadata") or {}
        features = body_meta.get("features") or {}
        if features.get("web_search") is True:
            return IntentResult(
                intent=WEB_SEARCH, confidence=1.0,
                tier="deterministic", reason="features.web_search=true",
            )

        # 4. MCP tools — admin configured external tool servers
        if self._has_mcp_tools:
            return IntentResult(
                intent=MCP_TOOLS, confidence=1.0,
                tier="deterministic", reason="mcp_servers_configured",
            )

        # 5. RAG — files attached or context detected
        audit = metadata.get("walacor_audit") or {}
        if audit.get("has_rag_context"):
            return IntentResult(
                intent=RAG, confidence=1.0,
                tier="deterministic", reason="has_rag_context=true",
            )
        files = body_meta.get("files") or metadata.get("files") or []
        if files:
            return IntentResult(
                intent=RAG, confidence=1.0,
                tier="deterministic", reason=f"files_attached={len(files)}",
            )

        return None

    # ── Tier 2: ONNX model ────────────────────────────────────────────

    def _tier2_onnx(self, prompt: str) -> IntentResult:
        """ONNX classification with confidence gating."""
        try:
            import numpy as np

            # sklearn pipeline ONNX model expects string input named "prompt"
            input_name = self._onnx_session.get_inputs()[0].name
            input_shape = self._onnx_session.get_inputs()[0].shape

            if input_name == "prompt":
                # sklearn TF-IDF + LR pipeline — string input
                inp = np.array([[prompt[:1000]]]).reshape(1, 1)
                outputs = self._onnx_session.run(None, {input_name: inp})
                # Output 0 = label, Output 1 = probability dict
                intent = str(outputs[0][0])
                prob_dict = outputs[1][0]  # {class: prob}
                confidence = float(max(prob_dict.values()))
            else:
                # Transformer model — tokenized input
                inputs = self._tokenize(prompt)
                outputs = self._onnx_session.run(None, inputs)
                logits = outputs[0][0]
                exp_logits = np.exp(logits - np.max(logits))
                probs = exp_logits / exp_logits.sum()
                idx = int(np.argmax(probs))
                confidence = float(probs[idx])
                intent = self._label_map.get(idx, NORMAL)

            if confidence >= CONFIDENCE_AUTO:
                return IntentResult(
                    intent=intent, confidence=confidence,
                    tier="ml_onnx", reason="onnx_auto_accept",
                )
            elif confidence >= CONFIDENCE_FLAG:
                return IntentResult(
                    intent=intent, confidence=confidence,
                    tier="ml_onnx", reason="onnx_flagged_for_review",
                )
            else:
                return IntentResult(
                    intent=NORMAL, confidence=confidence,
                    tier="ml_onnx", reason="onnx_low_confidence_default",
                )
        except Exception as e:
            logger.warning("ONNX classification failed: %s", e)
            return IntentResult(
                intent=NORMAL, confidence=0.0,
                tier="ml_onnx", reason=f"onnx_error: {e}",
            )

    def _tokenize(self, text: str) -> dict:
        """Tokenize text for ONNX model input."""
        if self._onnx_tokenizer:
            tokens = self._onnx_tokenizer(
                text[:512], padding="max_length", truncation=True,
                max_length=128, return_tensors="np",
            )
            return {k: v for k, v in tokens.items()}
        # Fallback: character-level encoding (placeholder)
        import numpy as np
        ids = [ord(c) % 30000 for c in text[:128]]
        ids = ids + [0] * (128 - len(ids))
        return {
            "input_ids": np.array([ids], dtype=np.int64),
            "attention_mask": np.array([[1] * len(text[:128]) + [0] * (128 - len(text[:128]))], dtype=np.int64),
        }

    # ── Model loading ─────────────────────────────────────────────────

    def _load_onnx(self, path: str) -> None:
        """Load ONNX model + optional tokenizer + label map."""
        from onnxruntime import InferenceSession
        self._onnx_session = InferenceSession(
            path, providers=["CPUExecutionProvider"],
        )
        # Load label map
        label_path = path.replace(".onnx", "_labels.json")
        if os.path.exists(label_path):
            with open(label_path) as f:
                labels = _json.load(f)
                self._label_map = {i: l for i, l in enumerate(labels)}
        else:
            self._label_map = {i: l for i, l in enumerate(ALL_INTENTS)}

        # Try loading tokenizer (from transformers)
        tokenizer_path = Path(path).parent / "tokenizer"
        if tokenizer_path.exists():
            try:
                from transformers import AutoTokenizer
                self._onnx_tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_path))
                logger.info("ONNX tokenizer loaded from %s", tokenizer_path)
            except Exception:
                logger.debug("ONNX tokenizer not loaded — using fallback")
