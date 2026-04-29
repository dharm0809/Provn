"""SchemaMapper trainer: production-featurized inputs → GradientBoosting → ONNX.

Mirrors the production `SchemaMapper` topology EXACTLY (see
`src/gateway/schema/mapper.py`). The candidate ONNX is shape-identical
to `schema_mapper.onnx`:

  * input  — `FloatTensorType([None, 139])` (the dimensionality of
             `gateway.schema.features.extract_features`)
  * output — `tensor(int64)` label indices into the production
             `schema_mapper_labels.json` ordering

This is a deliberate departure from the previous "fit a fresh
`DictVectorizer` on whatever raw feature dicts the harvester wrote and
ship the pickled vectorizer as a side-car" design. That design
produced an ONNX whose input dimension was determined by the
distillation corpus's distinct keys, not by the production
`extract_features` contract — typically a much smaller and entirely
different feature set than the 139-d vector production runs ORT
against. The candidate could pass the sanity gate (which used the same
DictVectorizer pickle) but would fail to drop into the production
`SchemaMapper._classify_onnx` pipeline because the input shape
mismatched.

The fix
-------
Trainer reuses production's deterministic `extract_features` pipeline.
Each X item is parsed into a `FlatField` and fed through the SAME
function `mapper._classify_onnx` calls at serve time. There is no
DictVectorizer — `extract_features` is itself the featurizer, and its
output dimensionality is fixed (`features.FEATURE_DIM == 139`). Only
the GradientBoostingClassifier is retrained, on (N, 139) float
features. The candidate ONNX is therefore drop-in compatible: copying
`schema_mapper-{version}.onnx` on top of
`production/schema_mapper.onnx` is enough.

Training-data shape
-------------------
The Phase 25 verdict log stores ONE row per `map_response` call (whole
response → one label) and `from_inference` is invoked WITHOUT a
`features=` kwarg, so `input_features_json` is `"{}"` for every
schema_mapper verdict in production today. The trainer accepts X as
JSON-serialized dicts and tries, in priority order, to interpret each
as:

  1. A "FlatField-like" dict — keys among `path`, `key`, `value`,
     `value_type`, `depth`, `parent_key`, `sibling_keys`,
     `sibling_types`, `int_siblings`. Constructs a `FlatField` and
     featurizes directly. This is the format a future per-field
     harvester revision would emit.
  2. A raw response JSON — flatten via `flatten_json`, take the FIRST
     non-trivial leaf field as the example, and featurize. Coarse but
     produces a plausible 139-d vector.
  3. Anything else (incl. `"{}"`) — featurize a placeholder
     `FlatField` (zero features). The classifier still trains; it just
     can't learn anything from a degenerate row. The dataset
     builder's per-class balance + `min_samples` gate keeps this from
     polluting promotion.

Known gap
---------
A useful candidate requires per-FIELD training signal, but the verdict
log only carries per-RESPONSE labels (the SchemaMapperHarvester
back-writes one canonical label per overflow burst). Until the
harvester is upgraded to emit per-field rows (or `from_inference` is
called with `features=extract_features(field)` at the inference site),
the trainer cannot match the production model's accuracy on its
in-graph features. The shape contract is intact, but the candidate
will likely fail the `SanityRunner.run` accuracy gate and be blocked
from promotion. That's the correct behavior — silently approving a
weakly-supervised candidate would be worse.

Side-cars emitted
-----------------
On `train()`, the trainer writes next to `schema_mapper-{version}.onnx`:

  * `schema_mapper-{version}.labels.json`        — copy of the
    production label list (must match production ordering for int64
    indices to align). Mirrors `schema_mapper_labels.json`
    byte-for-byte.
  * `schema_mapper-{version}.featurizer_ref.json` — sha256 hash of
    the production labels file plus a record of `FEATURE_DIM`. The
    featurizer itself is code (`extract_features`), so its
    "fingerprint" is the source-file hash of `schema/features.py`,
    captured here for promotion-time drift detection.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from gateway.intelligence.distillation.trainers.base import Trainer, TrainingError

logger = logging.getLogger(__name__)


# ── Production featurizer paths ──────────────────────────────────────────────


def _prod_dir() -> Path:
    from gateway.schema import mapper as _prod
    return Path(_prod.__file__).parent


def _prod_labels_path() -> Path:
    return _prod_dir() / "schema_mapper_labels.json"


def _features_module_path() -> Path:
    from gateway.schema import features as _features
    return Path(_features.__file__)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


@dataclass(frozen=True)
class _ProductionFeaturizer:
    """Frozen snapshot of production featurizer state.

    Schema mapper's "featurizer" is the function
    `gateway.schema.features.extract_features` (139-d output). There
    is no fitted state — the dim is constant, the rules are constant.
    We hash the source file so a promotion-time check can detect a
    breaking change to the feature pipeline.
    """
    feature_dim: int
    labels: list[str]
    file_hashes: dict[str, str]


def _load_production_featurizer() -> _ProductionFeaturizer:
    from gateway.schema.features import FEATURE_DIM

    labels_path = _prod_labels_path()
    if not labels_path.exists():
        raise TrainingError(
            f"schema_mapper trainer: production labels missing — {labels_path}. "
            "Cannot produce a shape-compatible candidate without this state."
        )

    try:
        labels = json.loads(labels_path.read_text())
    except (ValueError, OSError) as e:
        raise TrainingError(
            f"schema_mapper trainer: failed to parse {labels_path}: {e}"
        ) from e

    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise TrainingError(
            f"schema_mapper trainer: {labels_path} is not a list[str]"
        )

    features_src = _features_module_path()
    file_hashes = {
        "labels": _sha256(labels_path),
        "features_module": _sha256(features_src),
    }

    return _ProductionFeaturizer(
        feature_dim=int(FEATURE_DIM),
        labels=list(labels),
        file_hashes=file_hashes,
    )


# ── Production-pipeline featurization ────────────────────────────────────────


def _featurize_row(x: Any, dim: int) -> np.ndarray:
    """Coerce one X item into a (dim,) float32 vector via production rules.

    See the module docstring for the priority order of accepted input
    shapes. Always returns a vector of `dim` floats — never raises on
    shape; degenerate rows become zero vectors.
    """
    from gateway.schema.features import (
        FEATURE_DIM,
        FlatField,
        extract_features,
        flatten_json,
    )

    payload: Any = x
    if isinstance(x, str):
        try:
            payload = json.loads(x)
        except (ValueError, TypeError):
            payload = None

    field: FlatField | None = None

    if isinstance(payload, dict):
        # 1. FlatField-like — construct directly.
        if "path" in payload and "value_type" in payload:
            try:
                field = FlatField(
                    path=str(payload.get("path", "")),
                    key=str(payload.get("key", "")),
                    value=payload.get("value"),
                    value_type=str(payload.get("value_type", "null")),
                    depth=int(payload.get("depth", 0) or 0),
                    parent_key=str(payload.get("parent_key", "")),
                    sibling_keys=list(payload.get("sibling_keys") or []),
                    sibling_types=list(payload.get("sibling_types") or []),
                    int_siblings=list(payload.get("int_siblings") or []),
                )
            except (TypeError, ValueError):
                field = None
        else:
            # 2. Raw response — flatten and pick the first leaf-ish field.
            try:
                fields = flatten_json(payload)
            except Exception:  # noqa: BLE001 — flatten is defensive but cheap
                fields = []
            for f in fields:
                if f.value_type not in ("object", "array", "null"):
                    field = f
                    break

    if field is None:
        # 3. Placeholder zero-vector field. Won't carry signal but
        #    preserves shape / row count.
        field = FlatField(
            path="", key="", value=None, value_type="null",
            depth=0, parent_key="",
            sibling_keys=[], sibling_types=[], int_siblings=[],
        )

    vec = np.asarray(extract_features(field), dtype=np.float32)
    if vec.shape[0] != FEATURE_DIM:
        # Should never happen unless features.py is mid-edit and the
        # cached module disagrees with FEATURE_DIM.
        raise TrainingError(
            f"schema_mapper trainer: extract_features returned dim={vec.shape[0]}, "
            f"expected FEATURE_DIM={FEATURE_DIM}."
        )
    return vec


def featurize_batch(rows: list[Any], dim: int) -> np.ndarray:
    if not rows:
        return np.zeros((0, dim), dtype=np.float32)
    matrix = np.vstack([_featurize_row(r, dim) for r in rows]).astype(np.float32, copy=False)
    matrix = np.nan_to_num(matrix, nan=0.0, posinf=1.0, neginf=-1.0)
    return matrix


# ── Trainer ──────────────────────────────────────────────────────────────────


class SchemaMapperTrainer(Trainer):
    model_name = "schema_mapper"

    def _fit(self, X: list[Any], y: list[str]) -> Any:
        try:
            from sklearn.ensemble import GradientBoostingClassifier
        except ImportError as e:
            raise TrainingError(f"sklearn not available: {e}") from e

        feat = _load_production_featurizer()

        unknown = sorted({lbl for lbl in y if lbl not in feat.labels})
        if unknown:
            raise TrainingError(
                f"schema_mapper trainer: y contains labels not in production label set: "
                f"{unknown!r}. Production labels: {feat.labels!r}. Either correct the "
                "harvester output or ship a new packaged labels.json before training."
            )
        label_to_idx = {lbl: i for i, lbl in enumerate(feat.labels)}
        y_int = np.array([label_to_idx[lbl] for lbl in y], dtype=np.int64)

        X_features = featurize_batch(X, feat.feature_dim)

        clf = GradientBoostingClassifier(
            n_estimators=50,
            max_depth=3,
            learning_rate=0.1,
            random_state=42,
        )
        clf.fit(X_features, y_int)
        return _FittedSchemaMapperModel(clf=clf, featurizer=feat)

    def _to_onnx(self, fitted: Any, X_sample: list[Any]) -> bytes:
        try:
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import FloatTensorType
        except ImportError as e:
            raise TrainingError(f"skl2onnx not available: {e}") from e

        if not isinstance(fitted, _FittedSchemaMapperModel):
            raise TrainingError(
                "schema_mapper trainer: _to_onnx expected _FittedSchemaMapperModel, "
                f"got {type(fitted)!r}"
            )

        n_features = fitted.featurizer.feature_dim
        initial_type = [("features", FloatTensorType([None, n_features]))]
        onx = convert_sklearn(fitted.clf, initial_types=initial_type)
        return onx.SerializeToString()

    def _write_sidecars(
        self,
        fitted: Any,
        version: str,
        candidates_dir: Path,
    ) -> None:
        if not isinstance(fitted, _FittedSchemaMapperModel):
            logger.warning(
                "schema_mapper trainer: unexpected fitted object type %s; side-cars skipped",
                type(fitted),
            )
            return

        labels_path = candidates_dir / f"{self.model_name}-{version}.labels.json"
        labels_path.write_text(json.dumps(fitted.featurizer.labels))

        ref = {
            "labels_sha256":          fitted.featurizer.file_hashes["labels"],
            "features_module_sha256": fitted.featurizer.file_hashes["features_module"],
            "expected_input_dim":     int(fitted.featurizer.feature_dim),
        }
        ref_path = candidates_dir / f"{self.model_name}-{version}.featurizer_ref.json"
        ref_path.write_text(json.dumps(ref, sort_keys=True, indent=2))


@dataclass
class _FittedSchemaMapperModel:
    clf: Any
    featurizer: _ProductionFeaturizer
