"""Safety trainer: TF-IDF char_wb + GradientBoosting → ONNX.

Mirrors the production `SafetyClassifier` topology (see
`src/gateway/content/safety_classifier.py`). The 8 output labels match
the production model exactly so a candidate can drop into
`production/safety.onnx` without any serving-side changes.
"""
from __future__ import annotations

import logging
from typing import Any

from gateway.intelligence.distillation.trainers.base import Trainer, TrainingError

logger = logging.getLogger(__name__)


class SafetyTrainer(Trainer):
    model_name = "safety"

    def _fit(self, X: list[Any], y: list[str]) -> Any:
        """TfidfVectorizer(char_wb, 3-5) → GradientBoostingClassifier."""
        try:
            from sklearn.ensemble import GradientBoostingClassifier
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.pipeline import Pipeline
        except ImportError as e:
            raise TrainingError(f"sklearn not available: {e}") from e

        pipeline = Pipeline([
            ("tfidf", TfidfVectorizer(
                analyzer="char_wb",
                ngram_range=(3, 5),
                min_df=1,
                max_df=1.0,
                sublinear_tf=True,
            )),
            ("clf", GradientBoostingClassifier(
                # Matches the hyperparameters of the packaged production
                # safety model (as closely as the distillation-size
                # dataset allows). Lower `n_estimators` than a from-
                # scratch train because the distillation dataset is
                # small — 50 trees keeps training time bounded.
                n_estimators=50,
                max_depth=4,
                learning_rate=0.1,
                random_state=42,
            )),
        ])
        pipeline.fit([str(x) for x in X], y)
        return pipeline

    def _to_onnx(self, pipeline: Any, X_sample: list[Any]) -> bytes:
        try:
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import StringTensorType
        except ImportError as e:
            raise TrainingError(f"skl2onnx not available: {e}") from e

        initial_type = [("text", StringTensorType([None, 1]))]
        onx = convert_sklearn(pipeline, initial_types=initial_type)
        return onx.SerializeToString()
