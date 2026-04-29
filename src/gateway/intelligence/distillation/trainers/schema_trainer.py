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

ONNX shape
----------
The candidate ONNX is **classifier-only**: it takes a
`FloatTensorType([None, n_features])` input (the DictVectorizer's
column ordering is fixed at training time). The DictVectorizer is
NOT in the ONNX graph — `skl2onnx` doesn't faithfully convert the
dict→float coercion as part of an end-to-end Pipeline (it produces
a graph whose float-input topology silently diverges from
`pipeline.predict`). Instead, the trainer ships the fitted
DictVectorizer as a pickle side-car so the loader can reproduce the
exact column ordering before running the ONNX. Production
(`schema/mapper.py`) already does the split this way.

Side-cars
---------
On `train()` this trainer emits, next to `schema_mapper-{version}.onnx`:

  * `schema_mapper-{version}.dictvec.pkl`         — pickled fitted
    DictVectorizer (used by the adapter to call `.transform([row])`).
    Pickle is acceptable here: these files are produced and consumed
    locally on the same gateway host; they never round-trip over a
    network boundary. The adapter loads with `pickle.load` from a
    file path under the controlled `candidates/` directory.
  * `schema_mapper-{version}.feature_names.json` — the
    DictVectorizer's `feature_names_` list (column ordering). Useful
    for diff/audit tools that don't want to unpickle a sklearn object.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
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
        """Convert ONLY the classifier sub-step to ONNX.

        Converting the full `Pipeline[DictVectorizer, GBC]` with a
        `FloatTensorType` initial type produces an ONNX graph whose
        behavior diverges from `pipeline.predict` — DictVectorizer
        expects dict input, not a float matrix, and skl2onnx silently
        emits an inconsistent topology. The honest serialization is to
        convert the fitted classifier alone and ship the
        DictVectorizer as a pickle side-car (loaders apply
        `.transform([row_dict])` before calling the ONNX session, which
        is what production already does).
        """
        try:
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import FloatTensorType
        except ImportError as e:
            raise TrainingError(f"skl2onnx not available: {e}") from e

        try:
            vec = pipeline.named_steps["vec"]
            clf = pipeline.named_steps["clf"]
        except (AttributeError, KeyError) as e:
            raise TrainingError(
                f"schema_mapper pipeline missing expected steps (vec/clf): {e}"
            ) from e

        n_features = len(getattr(vec, "feature_names_", []) or [])
        if n_features == 0:
            raise TrainingError(
                "schema_mapper pipeline DictVectorizer has no feature_names_ "
                "(was it fitted?)"
            )

        initial_type = [("features", FloatTensorType([None, n_features]))]
        onx = convert_sklearn(clf, initial_types=initial_type)
        return onx.SerializeToString()

    def _write_sidecars(
        self,
        pipeline: Any,
        version: str,
        candidates_dir: Path,
    ) -> None:
        """Emit DictVectorizer pickle + feature names side-cars.

        The candidate ONNX expects a `(None, n_features)` float matrix
        whose columns are ordered by the fitted DictVectorizer's
        `feature_names_`. The sanity adapter must reproduce that
        ordering EXACTLY — easiest path is to ship the fitted
        DictVectorizer itself (pickled) so the adapter can call
        `.transform([row_dict])`. We additionally emit a JSON list of
        feature names so audit / diff tools don't need pickle access.

        Pickle is safe here: files are produced AND consumed locally on
        the same gateway host under the controlled `candidates/`
        directory. They never traverse a network boundary, and the
        adapter only ever loads from that controlled path.
        """
        try:
            vec = pipeline.named_steps["vec"]
        except (AttributeError, KeyError) as e:
            logger.warning(
                "schema_mapper trainer: could not extract vec step (%s); "
                "side-cars skipped", e,
            )
            return

        # JSON: stable ordering for diffability. The list ordering IS
        # the column ordering the candidate ONNX expects.
        feature_names = list(getattr(vec, "feature_names_", []) or [])
        names_path = (
            candidates_dir / f"{self.model_name}-{version}.feature_names.json"
        )
        names_path.write_text(json.dumps(feature_names))

        # Pickle: import locally to keep the symbol scoped to this
        # method (it's never used by the inference path).
        import pickle  # noqa: S403 — see module docstring; trusted local path
        pkl_path = candidates_dir / f"{self.model_name}-{version}.dictvec.pkl"
        with open(pkl_path, "wb") as fh:
            pickle.dump(vec, fh, protocol=pickle.HIGHEST_PROTOCOL)

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
