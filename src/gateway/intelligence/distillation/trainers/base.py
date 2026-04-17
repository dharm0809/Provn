"""Shared trainer contract.

Each concrete trainer (intent, schema_mapper, safety) implements
`train(X, y, version, candidates_dir)` and returns the path to the
emitted `.onnx` file. The base class owns the no-op validation and the
calibration-JSON writer so the concrete subclasses can focus on
the sklearn-specific parts.
"""
from __future__ import annotations

import abc
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TrainingError(RuntimeError):
    """Raised for conditions a trainer won't attempt to train through."""


class Trainer(abc.ABC):
    """Abstract base for per-model trainers.

    Concrete subclasses fix `model_name` and implement `_fit` (returns
    the fitted sklearn pipeline) and `_to_onnx` (serializes it). The
    template method `train` handles validation, the ONNX write, and the
    calibration JSON.
    """

    model_name: str = ""

    def train(
        self,
        X: list[Any],
        y: list[str],
        version: str,
        candidates_dir: Path,
    ) -> Path:
        self._validate(X, y, version)
        candidates_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = candidates_dir / f"{self.model_name}-{version}.onnx"
        calibration_path = (
            candidates_dir / f"{self.model_name}-{version}-calibration.json"
        )

        pipeline = self._fit(X, y)
        onnx_bytes = self._to_onnx(pipeline, X)
        candidate_path.write_bytes(onnx_bytes)
        self._write_calibration(y, pipeline, version, calibration_path)
        logger.info(
            "%s trainer: wrote candidate=%s calibration=%s (rows=%d)",
            self.model_name, candidate_path, calibration_path, len(X),
        )
        return candidate_path

    # ── Hooks for subclasses ───────────────────────────────────────────

    @abc.abstractmethod
    def _fit(self, X: list[Any], y: list[str]) -> Any:
        """Fit a sklearn pipeline and return it."""

    @abc.abstractmethod
    def _to_onnx(self, pipeline: Any, X_sample: list[Any]) -> bytes:
        """Serialize the fitted pipeline to ONNX bytes via skl2onnx."""

    # ── Shared helpers ─────────────────────────────────────────────────

    def _validate(self, X: list[Any], y: list[str], version: str) -> None:
        if not X or not y:
            raise TrainingError("training set is empty")
        if len(X) != len(y):
            raise TrainingError(
                f"X/y length mismatch: {len(X)} vs {len(y)}"
            )
        if len(set(y)) < 2:
            raise TrainingError(
                f"need at least 2 classes to train, got {set(y)!r}"
            )
        if not version or "/" in version or ".." in version:
            raise TrainingError(f"invalid version string {version!r}")

    def _write_calibration(
        self,
        y: list[str],
        pipeline: Any,
        version: str,
        path: Path,
    ) -> None:
        counts = Counter(y)
        total = sum(counts.values())
        payload = {
            "model_name": self.model_name,
            "version": version,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "classes": sorted(counts.keys()),
            "class_counts": dict(counts),
            "class_priors": {
                cls: round(n / total, 6) for cls, n in counts.items()
            },
            "total_samples": total,
        }
        path.write_text(json.dumps(payload, sort_keys=True, indent=2))
