"""Dataclass types for the intelligence layer.

Defines `ModelVerdict` — the canonical record produced every time an ONNX
(or Ollama-backed) model renders a prediction inside the gateway. Verdicts
are later harvested by the shadow/self-learning loop (self-learning loop),
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


@dataclass(frozen=True)
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
    # Production model version that produced this verdict. NULL on
    # pre-migration rows and on call sites that haven't been wired
    # through reload yet — callers that need per-version isolation
    # must pass `version` explicitly.
    version: str | None = None

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence}")

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
        version: str | None = None,
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
            version=version,
        )
