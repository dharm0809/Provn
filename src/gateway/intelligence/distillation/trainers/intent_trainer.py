"""Intent trainer: TF-IDF char n-grams → LogisticRegression → ONNX.

Matches the topology of the production intent model (see
`src/gateway/classifier/model.onnx`) so a candidate produced here can
slot into the registry's `candidates/` directory and be shadow-tested
(Phase F) without any pipeline rework.

Heavy imports (sklearn, skl2onnx) are deferred to `_fit` / `_to_onnx`
so the module can be imported on the hot path with zero cost.
"""
from __future__ import annotations

import logging
import numpy as np
from typing import Any

from gateway.intelligence.distillation.trainers.base import Trainer, TrainingError

logger = logging.getLogger(__name__)


class IntentTrainer(Trainer):
    model_name = "intent"

    def _fit(self, X: list[Any], y: list[str]) -> Any:
        """Fit TfidfVectorizer(char_wb, 3-5) + LogisticRegression.

        Wraps the vectorizer + classifier in a single sklearn `Pipeline`
        so skl2onnx converts them into one ONNX graph that accepts the
        raw string input `"prompt"` directly — matching the shape the
        existing IntentClassifier expects.
        """
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression
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
            ("clf", LogisticRegression(
                # `liblinear` handles small multiclass sets well and works
                # for both L1 and L2 penalties. `max_iter` bumped so small
                # datasets always converge.
                solver="liblinear",
                max_iter=1000,
                class_weight="balanced",
            )),
        ])
        # Cast X to strings — upstream storage is TEXT so this should
        # be a no-op in production, but defensive against numpy/bytes
        # leakage in test fixtures.
        pipeline.fit([str(x) for x in X], y)
        return pipeline

    def _to_onnx(self, pipeline: Any, X_sample: list[Any]) -> bytes:
        """Convert the fitted sklearn pipeline to ONNX bytes.

        Uses skl2onnx with a string input type so the ONNX graph can be
        fed the raw prompt at serving time — consistent with the
        production model's input contract.
        """
        try:
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import StringTensorType
        except ImportError as e:
            raise TrainingError(f"skl2onnx not available: {e}") from e

        initial_type = [("prompt", StringTensorType([None, 1]))]
        onx = convert_sklearn(pipeline, initial_types=initial_type)
        # `SerializeToString()` gives the wire bytes directly — no temp
        # file needed, and the base class writes them to the candidate
        # path atomically with `Path.write_bytes`.
        return onx.SerializeToString()
