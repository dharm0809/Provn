"""Safety trainer: production-featurized inputs → GradientBoosting → classifier-only ONNX.

Mirrors the production `SafetyClassifier` topology EXACTLY (see
`src/gateway/content/safety_classifier.py`). The candidate ONNX is
shape-identical to `safety_classifier.onnx`:

  * input  — `FloatTensorType([None, 200])` (post-SVD feature matrix)
  * output — `tensor(int64)` label indices into the production
             `safety_classifier_labels.json` ordering

This is a deliberate departure from the previous "fit a fresh
TfidfVectorizer + classifier and ship the new vocab/idf as side-cars"
design. That design produced an ONNX with input shape `(None, n_vocab)`
where `n_vocab` was whatever sklearn fit on the (small) distillation
corpus; production expects `(None, 200)` after a SVD reduction whose
components are NOT recoverable from the corpus alone. The result was a
candidate that could pass the sanity gate (which used the same drifted
side-cars) but would fail to drop into the production
`SafetyClassifier._featurize → session.run` pipeline because the input
shape didn't match.

The fix
-------
Trainer reuses production's packaged featurizer state — vocab, IDF,
SVD components, ngram_range, label list — instead of refitting. Only
the GradientBoostingClassifier is retrained, on (N, 200) float
features built by the production featurization pipeline. The candidate
ONNX is therefore drop-in compatible: copying `safety-{version}.onnx`
on top of `production/safety_classifier.onnx` is enough; the existing
vocab/idf/svd side-cars don't change.

Side-cars emitted
-----------------
On `train()`, the trainer writes next to `safety-{version}.onnx`:

  * `safety-{version}.labels.json`        — copy of the production
    label list (must match production ordering for int64 indices to
    align). Mirrors `safety_classifier_labels.json` byte-for-byte.
  * `safety-{version}.featurizer_ref.json` — sha256 hashes of every
    production sidecar file actually used at training time
    (vocab, idf, svd, config). Lets ops verify that the candidate
    was trained against THE SAME featurizer state that's shipping in
    production at promote time. If any hash drifts, the candidate is
    presumed incompatible and promotion should be blocked.

Training data shape
-------------------
`X` must be a list of strings (raw text). `y` must be a list of label
strings drawn from the production label set. Any `y` not in the
production labels list raises `TrainingError` (silent label remapping
would mask data corruption).
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
#
# Path module: gateway.content.safety_classifier
#
# We deliberately re-derive the directory rather than importing the
# private `_MODEL_DIR` module-level so a future refactor that moves the
# constant doesn't silently break us. The paths are stable as long as
# the package layout is.

def _prod_dir() -> Path:
    from gateway.content import safety_classifier as _prod
    return Path(_prod.__file__).parent


def _prod_paths() -> dict[str, Path]:
    d = _prod_dir()
    return {
        "vocab": d / "safety_tfidf_vocab.json",
        "idf": d / "safety_tfidf_idf.npy",
        "svd": d / "safety_svd_components.npy",
        "config": d / "safety_tfidf_config.json",
        "labels": d / "safety_classifier_labels.json",
    }


# ── Production featurization (mirrors SafetyClassifier._featurize) ──────────
#
# Replicated here rather than imported from `safety_classifier` because
# the production class loads and caches state at construction time
# (intended for serving), and the trainer needs a stateless functional
# pipeline driven from explicit sidecar paths.

# Pattern features must match `_extract_safety_features` in
# `gateway.content.safety_classifier`. We import them rather than
# re-defining to guarantee zero drift.
from gateway.content.safety_classifier import _extract_safety_features


@dataclass(frozen=True)
class _ProductionFeaturizer:
    """Frozen snapshot of production featurizer state.

    Loaded once per training run; passed to `_featurize` for each X
    item. Holds NO sklearn object — the production featurization is
    pure numpy, so the trainer never has to round-trip through a
    sklearn transformer that might rev its serialization format.
    """
    vocab: dict[str, int]
    idf: np.ndarray
    svd_components: np.ndarray
    ngram_range: tuple[int, int]
    labels: list[str]
    # File hashes, recorded at load time so the sidecar can claim
    # provenance without re-reading the files later.
    file_hashes: dict[str, str]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_production_featurizer() -> _ProductionFeaturizer:
    """Load production sidecar state.

    Raises `TrainingError` with a precise message if any required file
    is missing — without these the trainer cannot produce a
    shape-compatible candidate, so failing loud is correct.
    """
    paths = _prod_paths()
    for name, p in paths.items():
        if not p.exists():
            raise TrainingError(
                f"safety trainer: production featurizer file missing — {name}={p}. "
                "Cannot produce a shape-compatible candidate without this state."
            )

    try:
        vocab = json.loads(paths["vocab"].read_text())
        idf = np.load(str(paths["idf"]))
        svd = np.load(str(paths["svd"]))
        config = json.loads(paths["config"].read_text())
        labels = json.loads(paths["labels"].read_text())
    except (ValueError, OSError) as e:
        raise TrainingError(f"safety trainer: failed to parse production sidecars: {e}") from e

    ngram_range_raw = config.get("ngram_range", [3, 5])
    if not (isinstance(ngram_range_raw, (list, tuple)) and len(ngram_range_raw) == 2):
        raise TrainingError(
            f"safety trainer: production tfidf_config.ngram_range malformed: {ngram_range_raw!r}"
        )
    ngram_range = (int(ngram_range_raw[0]), int(ngram_range_raw[1]))

    if svd.ndim != 2:
        raise TrainingError(
            f"safety trainer: production svd_components.npy has unexpected shape {svd.shape}; "
            "expected 2D (n_components, n_input_features)."
        )
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise TrainingError(
            "safety trainer: production safety_classifier_labels.json is not a list[str]"
        )

    file_hashes = {name: _sha256(p) for name, p in paths.items()}

    return _ProductionFeaturizer(
        vocab={k: int(v) for k, v in vocab.items()},
        idf=np.asarray(idf, dtype=np.float32),
        svd_components=np.asarray(svd, dtype=np.float32),
        ngram_range=ngram_range,
        labels=list(labels),
        file_hashes=file_hashes,
    )


def _tfidf_transform(text: str, feat: _ProductionFeaturizer) -> np.ndarray:
    """Reproduce `SafetyClassifier._tfidf_transform` exactly.

    Char_wb n-grams over a space-padded lowered string, sublinear TF
    (log1p), IDF multiply, L2 normalize.
    """
    text_lower = f" {text.lower()} "
    ngrams: dict[str, int] = {}
    n_lo, n_hi = feat.ngram_range
    for n in range(n_lo, n_hi + 1):
        for i in range(len(text_lower) - n + 1):
            gram = text_lower[i:i + n]
            if gram in feat.vocab:
                ngrams[gram] = ngrams.get(gram, 0) + 1

    n_features = len(feat.idf)
    tf_vector = np.zeros(n_features, dtype=np.float32)
    for gram, count in ngrams.items():
        idx = feat.vocab.get(gram)
        if idx is not None and idx < n_features:
            tf_vector[idx] = np.log1p(count)

    tf_vector *= feat.idf
    norm = np.linalg.norm(tf_vector)
    if norm > 0:
        tf_vector /= norm
    return tf_vector


def _featurize_row(text: str, feat: _ProductionFeaturizer) -> np.ndarray:
    """Produce one (200,) feature row matching production's `_featurize`.

    TF-IDF (n_vocab,) ++ pattern features (13,) → SVD reduce → 200d.
    """
    tfidf_vec = _tfidf_transform(text, feat)
    pattern_vec = np.array(_extract_safety_features(text), dtype=np.float32)
    combined = np.concatenate([tfidf_vec, pattern_vec])

    # Defensive: production svd_components shape is (n_out, n_in).
    expected_in = feat.svd_components.shape[1]
    if combined.shape[0] != expected_in:
        raise TrainingError(
            f"safety trainer: featurization input dim {combined.shape[0]} "
            f"does not match svd_components input axis {expected_in}. "
            "Production sidecars are inconsistent with each other."
        )
    reduced = combined @ feat.svd_components.T
    return reduced.astype(np.float32, copy=False)


def featurize_batch(texts: list[str], feat: _ProductionFeaturizer) -> np.ndarray:
    """Stack `_featurize_row` over a batch into an (N, 200) float32 matrix."""
    if not texts:
        return np.zeros((0, feat.svd_components.shape[0]), dtype=np.float32)
    rows = [_featurize_row(t, feat) for t in texts]
    matrix = np.vstack(rows).astype(np.float32, copy=False)
    matrix = np.nan_to_num(matrix, nan=0.0, posinf=1.0, neginf=-1.0)
    return matrix


# ── Trainer ──────────────────────────────────────────────────────────────────


class SafetyTrainer(Trainer):
    """Train a classifier on production-featurized inputs and emit a
    drop-in-compatible ONNX candidate.

    The fitted artifact this returns from `_fit` is a small dataclass
    bundle (`_FittedSafetyModel`) carrying the sklearn classifier and
    the featurizer it was fit against. Subsequent hooks (`_to_onnx`,
    `_write_sidecars`, `_write_calibration`) read from the bundle.
    """

    model_name = "safety"

    def _fit(self, X: list[Any], y: list[str]) -> Any:
        try:
            from sklearn.ensemble import GradientBoostingClassifier
        except ImportError as e:
            raise TrainingError(f"sklearn not available: {e}") from e

        feat = _load_production_featurizer()

        # Validate y BEFORE featurizing — featurization is the slow
        # step, and rejecting bad labels early saves the cycle.
        unknown = sorted({lbl for lbl in y if lbl not in feat.labels})
        if unknown:
            raise TrainingError(
                f"safety trainer: y contains labels not in production label set: {unknown!r}. "
                f"Production labels: {feat.labels!r}. Either correct the harvester output or "
                "ship a new packaged labels.json before training."
            )
        # Convert y → int64 indices in PRODUCTION label order. The
        # ONNX classifier output thus aligns with production's
        # `_labels[pred_idx]` lookup at inference time.
        label_to_idx = {lbl: i for i, lbl in enumerate(feat.labels)}
        y_int = np.array([label_to_idx[lbl] for lbl in y], dtype=np.int64)

        texts = [str(x) for x in X]
        X_features = featurize_batch(texts, feat)
        if X_features.shape[1] != feat.svd_components.shape[0]:
            # Sanity assertion: catches a future regression in
            # featurize_batch silently changing shape.
            raise TrainingError(
                f"safety trainer: featurized matrix shape {X_features.shape} "
                f"does not match expected SVD output dim {feat.svd_components.shape[0]}."
            )

        clf = GradientBoostingClassifier(
            n_estimators=50,
            max_depth=4,
            learning_rate=0.1,
            random_state=42,
        )
        clf.fit(X_features, y_int)
        return _FittedSafetyModel(clf=clf, featurizer=feat)

    def _to_onnx(self, fitted: Any, X_sample: list[Any]) -> bytes:
        """Convert ONLY the classifier to ONNX with a `(None, 200)` input.

        Featurization happens upstream of the graph (at training time
        here, at inference time in production via
        `SafetyClassifier._featurize`). The graph output is a tensor of
        int64 label indices — index lookup against
        `safety_classifier_labels.json` is the consumer's job.
        """
        try:
            from skl2onnx import convert_sklearn
            from skl2onnx.common.data_types import FloatTensorType
        except ImportError as e:
            raise TrainingError(f"skl2onnx not available: {e}") from e

        if not isinstance(fitted, _FittedSafetyModel):
            raise TrainingError(
                f"safety trainer: _to_onnx expected _FittedSafetyModel, got {type(fitted)!r}"
            )

        n_features = fitted.featurizer.svd_components.shape[0]
        initial_type = [("features", FloatTensorType([None, n_features]))]
        onx = convert_sklearn(fitted.clf, initial_types=initial_type)
        return onx.SerializeToString()

    def _write_sidecars(
        self,
        fitted: Any,
        version: str,
        candidates_dir: Path,
    ) -> None:
        """Write labels.json + featurizer_ref.json side-cars.

        The vocab/idf/svd files are NOT re-emitted: they are shared
        production state. Re-emitting our own copy would invite drift
        between candidate and production at promotion time. Instead we
        record the sha256 hashes of the production files we used so a
        diff at promote time can detect mismatch.
        """
        if not isinstance(fitted, _FittedSafetyModel):
            logger.warning(
                "safety trainer: unexpected fitted object type %s; side-cars skipped",
                type(fitted),
            )
            return

        # labels.json — copy of production order. The candidate ONNX
        # emits int64 indices into this list; the file MUST match
        # production exactly for the indices to map back to the same
        # label strings post-promotion.
        labels_path = candidates_dir / f"{self.model_name}-{version}.labels.json"
        labels_path.write_text(json.dumps(fitted.featurizer.labels))

        # featurizer_ref.json — provenance of the featurizer state.
        # Shape:
        #   {
        #     "vocab_sha256":  "...",
        #     "idf_sha256":    "...",
        #     "svd_sha256":    "...",
        #     "config_sha256": "...",
        #     "labels_sha256": "...",
        #     "expected_input_dim": 200,
        #     "ngram_range": [3, 5]
        #   }
        # Promotion gates can sha256 the live production files and
        # compare; a mismatch means production has moved on since this
        # candidate was trained, and the candidate is presumed
        # incompatible.
        ref = {
            "vocab_sha256":  fitted.featurizer.file_hashes["vocab"],
            "idf_sha256":    fitted.featurizer.file_hashes["idf"],
            "svd_sha256":    fitted.featurizer.file_hashes["svd"],
            "config_sha256": fitted.featurizer.file_hashes["config"],
            "labels_sha256": fitted.featurizer.file_hashes["labels"],
            "expected_input_dim": int(fitted.featurizer.svd_components.shape[0]),
            "ngram_range": list(fitted.featurizer.ngram_range),
        }
        ref_path = candidates_dir / f"{self.model_name}-{version}.featurizer_ref.json"
        ref_path.write_text(json.dumps(ref, sort_keys=True, indent=2))


@dataclass
class _FittedSafetyModel:
    """Bundle of (classifier, featurizer-state) returned from `_fit`."""
    clf: Any
    featurizer: _ProductionFeaturizer
