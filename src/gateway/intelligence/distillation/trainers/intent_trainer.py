"""Intent trainer: TF-IDF char n-grams → LogisticRegression → classifier-only ONNX.

Matches the topology of the production intent model (see
`src/gateway/classifier/model.onnx`) so a candidate produced here can
slot into the registry's `candidates/` directory and be shadow-tested
(Phase F) without any pipeline rework.

ONNX shape
----------
The candidate ONNX is **classifier-only**: the featurizer state is
emitted as side-cars (vocab.json, idf.npy, labels.json) so production
/ sanity-adapter loading can apply char_wb TF-IDF in Python before
running the ONNX. This matches what production already does and
avoids the skl2onnx limitation that only `tokenizer='word'` is
supported for in-graph TfidfVectorizer (`char_wb` raises
`NotImplementedError`).

Heavy imports (sklearn, skl2onnx) are deferred to `_fit` / `_to_onnx`
so the module can be imported on the hot path with zero cost.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from gateway.intelligence.distillation.trainers.base import Trainer, TrainingError

logger = logging.getLogger(__name__)


class IntentTrainer(Trainer):
    model_name = "intent"

    def _fit(self, X: list[Any], y: list[str]) -> Any:
        """Fit TfidfVectorizer(char_wb, 3-5) + LogisticRegression.

        Wraps the vectorizer + classifier in a single sklearn `Pipeline`.
        skl2onnx cannot convert `analyzer='char_wb'` TF-IDF (only
        `tokenizer='word'` is supported), so `_to_onnx` serializes the
        classifier alone — featurizer state is emitted as side-cars and
        applied by the loader before invoking the ONNX session.
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
        """Convert ONLY the classifier sub-step to ONNX.

        skl2onnx cannot convert a `TfidfVectorizer(analyzer='char_wb')`
        (raises `NotImplementedError`), so we serialize the
        LogisticRegression classifier alone with a `FloatTensorType`
        input matching the fitted TF-IDF dimension. Loaders apply
        char_wb TF-IDF in Python (using the side-car vocab.json +
        idf.npy) and feed the resulting float matrix here.
        """
        try:
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import FloatTensorType
        except ImportError as e:
            raise TrainingError(f"skl2onnx not available: {e}") from e

        try:
            tfidf = pipeline.named_steps["tfidf"]
            clf = pipeline.named_steps["clf"]
        except (AttributeError, KeyError) as e:
            raise TrainingError(
                f"intent pipeline missing expected steps (tfidf/clf): {e}"
            ) from e

        n_features = len(getattr(tfidf, "vocabulary_", {}))
        if n_features == 0:
            raise TrainingError(
                "intent pipeline tfidf has no vocabulary_ (was it fitted?)"
            )

        initial_type = [("features", FloatTensorType([None, n_features]))]
        onx = convert_sklearn(clf, initial_types=initial_type)
        # `SerializeToString()` gives the wire bytes directly — no temp
        # file needed, and the base class writes them to the candidate
        # path atomically with `Path.write_bytes`.
        return onx.SerializeToString()

    def _write_sidecars(
        self,
        pipeline: Any,
        version: str,
        candidates_dir: Path,
    ) -> None:
        """Emit labels / vocab / idf side-cars next to the candidate ONNX.

        The candidate ONNX is classifier-only (see `_to_onnx`); the
        loader must reproduce char_wb TF-IDF in Python from these
        side-cars before running the session:

          * `intent-{version}.vocab.json`  — fitted `vocabulary_`.
          * `intent-{version}.idf.npy`     — fitted IDF weights.
          * `intent-{version}.labels.json` — classifier `classes_`.
        """
        try:
            import numpy as np
        except ImportError as e:  # pragma: no cover — sklearn implies numpy
            raise TrainingError(f"numpy not available: {e}") from e

        try:
            tfidf = pipeline.named_steps["tfidf"]
            clf = pipeline.named_steps["clf"]
        except (AttributeError, KeyError) as e:
            logger.warning(
                "intent trainer: could not extract pipeline steps for "
                "side-car emission (%s); skipping", e,
            )
            return

        vocab = getattr(tfidf, "vocabulary_", None)
        if vocab is not None:
            vocab_path = candidates_dir / f"{self.model_name}-{version}.vocab.json"
            vocab_path.write_text(
                json.dumps({k: int(v) for k, v in vocab.items()}, sort_keys=True)
            )
        else:
            logger.warning("intent trainer: tfidf has no vocabulary_; vocab side-car skipped")

        idf = getattr(tfidf, "idf_", None)
        if idf is not None:
            idf_path = candidates_dir / f"{self.model_name}-{version}.idf.npy"
            np.save(str(idf_path), np.asarray(idf, dtype=np.float32))
        else:
            logger.warning("intent trainer: tfidf has no idf_; idf side-car skipped")

        classes = getattr(clf, "classes_", None)
        if classes is not None:
            labels_path = candidates_dir / f"{self.model_name}-{version}.labels.json"
            labels_path.write_text(json.dumps([str(c) for c in classes]))
        else:
            logger.warning("intent trainer: clf has no classes_; labels side-car skipped")
