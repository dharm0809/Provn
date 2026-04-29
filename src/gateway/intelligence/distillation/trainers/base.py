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
        # Side-cars (vocab.json, idf.npy, dictvec.pkl, …) — concrete trainers
        # that need them override `_write_sidecars`. Default: no-op (intent's
        # end-to-end string-input ONNX needs none — TF-IDF state is embedded
        # in the ONNX graph by skl2onnx). Sanity adapters for safety /
        # schema_mapper REQUIRE these files to exist; missing side-cars
        # surface as a sanity FAILURE (block promotion) — see
        # `gateway.intelligence.sanity_adapters`.
        self._write_sidecars(pipeline, version, candidates_dir)
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

    def _write_sidecars(
        self,
        pipeline: Any,
        version: str,
        candidates_dir: Path,
    ) -> None:
        """Write per-candidate side-cars next to the ONNX file.

        Default implementation is a no-op (intent's end-to-end ONNX
        carries all featurizer state inside the graph). Concrete
        trainers that need to expose featurizer state at sanity time
        (safety, schema_mapper) override this hook. Side-car filename
        convention: `{model}-{version}.{name}` so they sit next to the
        candidate ONNX and the calibration JSON in the same dir.
        """
        return None

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
