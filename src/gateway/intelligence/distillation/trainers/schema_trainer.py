"""SchemaMapper trainer: value-aware features → GradientBoosting → ONNX.

The production SchemaMapper is a per-FIELD classifier over numeric
features from `gateway.schema.features.extract_features`. Training
candidates here reuse the same feature dimension so a produced
candidate can drop directly into `production/schema_mapper.onnx` at
promotion time.

Input `X` is a list of feature-dict JSON strings (one per divergent
verdict row; the verdict log stores these as `input_features_json`).
The trainer parses them into a numeric matrix and fits a
GradientBoostingClassifier targeting `y` labels produced by the
SchemaMapper harvester (canonical labels from `_PATH_FALLBACK_RULES`).

Known caveat
------------
The Phase 25 verdict log stores ONE row per `map_response` call, not
per field. The SchemaMapper harvester (Task 14) emits a single
canonical label for the whole response based on overflow-key analysis.
This trainer therefore trains a coarser classifier than the production
per-field model. Tasks 21-25 (Phase F) will shadow-test candidates
against the production classifier — a candidate that doesn't match the
field-level topology will simply fail the shadow gates, which is the
correct behavior until the verdict log is extended to carry per-field
verdicts.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np

from gateway.intelligence.distillation.trainers.base import Trainer, TrainingError

logger = logging.getLogger(__name__)


class SchemaMapperTrainer(Trainer):
    model_name = "schema_mapper"

    def _fit(self, X: list[Any], y: list[str]) -> Any:
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.feature_extraction import DictVectorizer
            from sklearn.pipeline import Pipeline
        except ImportError as e:
            raise TrainingError(f"sklearn not available: {e}") from e

        # Parse feature JSONs to dicts. Invalid / non-dict rows are
        # coerced to empty dicts — DictVectorizer will represent them
        # as a zero vector, which is the correct "no signal" fallback.
        dicts = [self._parse_features(x) for x in X]
        pipeline = Pipeline([
            # `DictVectorizer(sparse=False)` is required because
            # GradientBoostingClassifier doesn't accept sparse input
            # and skl2onnx converts dense arrays more reliably.
            ("vec", DictVectorizer(sparse=False)),
            ("clf", GradientBoostingClassifier(
                n_estimators=50,
                max_depth=3,
                learning_rate=0.1,
                random_state=42,
            )),
        ])
        pipeline.fit(dicts, y)
        return pipeline

    def _to_onnx(self, pipeline: Any, X_sample: list[Any]) -> bytes:
        try:
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import FloatTensorType
        except ImportError as e:
            raise TrainingError(f"skl2onnx not available: {e}") from e

        # Feature width is determined by the fitted DictVectorizer —
        # skl2onnx needs this in the initial type so the graph has a
        # fixed-shape input tensor.
        vec = pipeline.named_steps["vec"]
        n_features = len(vec.feature_names_)
        initial_type = [("features", FloatTensorType([None, n_features]))]
        onx = convert_sklearn(pipeline, initial_types=initial_type)
        return onx.SerializeToString()

    @staticmethod
    def _parse_features(x: Any) -> dict[str, float]:
        """Coerce an X item (JSON string or dict) into a flat feature dict."""
        if isinstance(x, dict):
            return {str(k): float(v) for k, v in x.items() if _is_numeric(v)}
        if isinstance(x, str):
            try:
                data = json.loads(x)
            except (ValueError, TypeError):
                return {}
            if not isinstance(data, dict):
                return {}
            return {str(k): float(v) for k, v in data.items() if _is_numeric(v)}
        return {}


def _is_numeric(v: Any) -> bool:
    # `isinstance(True, int)` is True in Python — guard against that.
    return isinstance(v, (int, float)) and not isinstance(v, bool)
