"""Safety trainer: TF-IDF char_wb + GradientBoosting → ONNX.

Mirrors the production `SafetyClassifier` topology (see
`src/gateway/content/safety_classifier.py`). The 8 output labels match
the production model exactly so a candidate can drop into
`production/safety.onnx` without any serving-side changes.

Side-cars
---------
On `train()`, the trainer emits the following files next to the
candidate ONNX (`safety-{version}.onnx`) so the offline sanity gate
(see `gateway.intelligence.sanity_adapters`) can verify the candidate's
featurizer state matches what was actually fit:

  * `safety-{version}.labels.json`  — sorted class labels (mirrors
    production `safety_classifier_labels.json` shape).
  * `safety-{version}.vocab.json`   — the fitted TfidfVectorizer's
    `vocabulary_` (term → column index).
  * `safety-{version}.idf.npy`      — the fitted IDF weights.

The current trainer pipeline is end-to-end (TF-IDF inside the ONNX
graph via skl2onnx), so the candidate ONNX accepts a string input
directly — same shape as the intent candidate. The side-cars are
emitted so the sanity adapter can REQUIRE them as a "well-formed
candidate" precondition (a candidate emitted by a stale trainer that
doesn't write side-cars surfaces as a loud sanity FAILURE rather than a
silent skip). Consumers that need to manually featurize (e.g. future
classifier-only ONNX exports that mirror the production split) can use
the same files without trainer changes.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
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

    def _write_sidecars(
        self,
        pipeline: Any,
        version: str,
        candidates_dir: Path,
    ) -> None:
        """Emit labels / vocab / idf side-cars next to the candidate ONNX.

        The fitted `TfidfVectorizer` and `GradientBoostingClassifier` are
        attributes of the pipeline:

          * `pipeline.named_steps["tfidf"].vocabulary_`  → vocab.json
          * `pipeline.named_steps["tfidf"].idf_`         → idf.npy
          * `pipeline.named_steps["clf"].classes_`       → labels.json

        Stable-sorted JSON for vocab + labels so two trainings on the
        same data produce byte-identical side-cars (helps reproduce
        canonical-version diffs at audit time). NaN/Inf in idf is left
        as-is — the sanity adapter is responsible for nan_to_num at
        inference, mirroring the production featurizer.
        """
        try:
            import numpy as np
        except ImportError as e:  # pragma: no cover — sklearn implies numpy
            raise TrainingError(f"numpy not available: {e}") from e

        try:
            tfidf = pipeline.named_steps["tfidf"]
            clf = pipeline.named_steps["clf"]
        except (AttributeError, KeyError) as e:
            # Defensive: a stub trainer or future refactor may not
            # expose these steps. Side-cars are non-critical to the
            # `.onnx` write itself; log and skip rather than failing
            # the whole train() call.
            logger.warning(
                "safety trainer: could not extract pipeline steps for "
                "side-car emission (%s); skipping", e,
            )
            return

        # Vocabulary: sorted by term for deterministic output. The
        # values are int indices into the IDF/feature vector.
        vocab = getattr(tfidf, "vocabulary_", None)
        if vocab is not None:
            vocab_path = candidates_dir / f"{self.model_name}-{version}.vocab.json"
            vocab_path.write_text(
                json.dumps({k: int(v) for k, v in vocab.items()}, sort_keys=True)
            )
        else:
            logger.warning("safety trainer: tfidf has no vocabulary_; vocab side-car skipped")

        # IDF weights as float32 npy — consumed by the sanity adapter
        # if it wants to reproduce production featurization. Falls
        # back to a no-op if the vectorizer didn't compute idf.
        idf = getattr(tfidf, "idf_", None)
        if idf is not None:
            idf_path = candidates_dir / f"{self.model_name}-{version}.idf.npy"
            np.save(str(idf_path), np.asarray(idf, dtype=np.float32))
        else:
            logger.warning("safety trainer: tfidf has no idf_; idf side-car skipped")

        # Classes: list of label strings in classifier index order.
        # Mirrors `safety_classifier_labels.json` in production.
        classes = getattr(clf, "classes_", None)
        if classes is not None:
            labels_path = candidates_dir / f"{self.model_name}-{version}.labels.json"
            labels_path.write_text(
                json.dumps([str(c) for c in classes])
            )
        else:
            logger.warning("safety trainer: clf has no classes_; labels side-car skipped")
