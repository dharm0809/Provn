"""Dataclass types for the intelligence layer.

Defines `ModelVerdict` — the canonical record produced every time an ONNX
(or Ollama-backed) model renders a prediction inside the gateway. Verdicts
are later harvested by the shadow/self-learning loop (Phase 25 Tasks 13-16),
which may populate `divergence_signal` / `divergence_source` after the fact.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ModelVerdict:
    model_name: str
    input_hash: str
    input_features_json: str
    prediction: str
    confidence: float
    request_id: str | None
    timestamp: str = field(default_factory=_utcnow_iso)
    divergence_signal: str | None = None
    divergence_source: str | None = None

    @classmethod
    def from_inference(
        cls,
        *,
        model_name: str,
        input_text: str,
        prediction: str,
        confidence: float,
        request_id: str | None = None,
        features: dict[str, Any] | None = None,
    ) -> "ModelVerdict":
        input_hash = hashlib.sha256(input_text.encode()).hexdigest()
        # sort_keys so logically-equal feature dicts produce identical JSON —
        # lets downstream dedup/fingerprinting treat key order as insignificant.
        features_json = json.dumps(features or {}, sort_keys=True)
        return cls(
            model_name=model_name,
            input_hash=input_hash,
            input_features_json=features_json,
            prediction=prediction,
            confidence=confidence,
            request_id=request_id,
        )
