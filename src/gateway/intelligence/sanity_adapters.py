"""Per-model `infer_fn` factories for the SanityRunner gate.

`SanityRunner.run(model_name, infer_fn)` is intentionally model-agnostic:
the runner just iterates a labeled fixture and calls `infer_fn(input)` on
each example. The model-specific plumbing — how to build an
`InferenceSession`, how to encode the input, how to decode the output back
into a label string — lives here as a strategy table keyed by model name.

Wiring status (after the production-shape refactor)
---------------------------------------------------

  * intent          — fully wired. The trainer
                      (`gateway.intelligence.distillation.trainers.intent_trainer`)
                      exports a classifier-only ONNX with
                      `FloatTensorType([None, n_features])` input
                      (skl2onnx cannot convert `analyzer='char_wb'`
                      TfidfVectorizer in-graph) plus three side-cars
                      per candidate: `intent-{version}.labels.json`,
                      `intent-{version}.vocab.json`,
                      `intent-{version}.idf.npy`. The adapter applies
                      char_wb TF-IDF in Python before running the
                      session.
  * safety          — fully wired. The trainer emits a candidate ONNX
                      whose input shape is `(None, 200)` — the
                      production-`SafetyClassifier` post-SVD vector
                      dimension. Featurization is performed by this
                      adapter using the SAME packaged production
                      sidecars the production class uses
                      (`safety_tfidf_vocab.json`,
                      `safety_tfidf_idf.npy`,
                      `safety_svd_components.npy`,
                      `safety_tfidf_config.json`,
                      `safety_classifier_labels.json`). The trainer
                      side-cars are NOT featurizer state — they are
                      label list + provenance hashes — so the adapter
                      ignores trainer side-cars for featurization but
                      requires them to exist as a sanity contract
                      check (a candidate without side-cars came from
                      a stale trainer; block promotion).
  * schema_mapper   — fully wired. The trainer emits a candidate
                      ONNX whose input shape is `(None, 139)` — the
                      production `extract_features` dimensionality.
                      Featurization is performed by this adapter
                      using `gateway.schema.features.flatten_json` +
                      `extract_features`, the same code production
                      runs at serve time. As above, the trainer
                      side-cars (`labels.json` +
                      `featurizer_ref.json`) are required as a
                      contract check but are not featurizer state.

Backward compatibility: candidates emitted by older trainer revisions
(those that wrote `vocab.json/idf.npy` for safety, or
`dictvec.pkl/feature_names.json` for schema_mapper) won't have the
new `featurizer_ref.json` side-car. The adapters require the new
side-car file specifically — so an old candidate surfaces as
`FileNotFoundError`, the gate treats it as a sanity FAILURE, and
promotion is blocked. This is intentional: an old candidate has the
WRONG ONNX shape and would crash production reload anyway; failing
loud at the gate is much cheaper than a partial promote.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


# ── Adapter protocol ──────────────────────────────────────────────────────

class SanityAdapter(Protocol):
    """Builds a per-candidate `infer_fn` for the SanityRunner.

    Implementations open the candidate ONNX session and return a callable
    that takes a fixture input and returns the predicted label string.
    """

    def __call__(self, candidate_path: Path) -> Callable[[Any], str]: ...


# ── Intent (fully wired) ──────────────────────────────────────────────────


def _intent_adapter(candidate_path: Path) -> Callable[[Any], str]:
    """Build an `infer_fn` for the `intent` candidate.

    The trainer exports a classifier-only ONNX with
    `FloatTensorType([None, n_features])` input — the char_wb
    TfidfVectorizer is NOT in the graph (skl2onnx only supports
    `tokenizer='word'` for in-graph TF-IDF). The adapter therefore
    reproduces char_wb TF-IDF in Python from the side-cars
    (`vocab.json`, `idf.npy`) and feeds the float matrix to the
    session.

    Misuse modes:
      * candidate file missing → `FileNotFoundError` → caught by the gate
        as a sanity failure (block promotion).
      * any required side-car missing → `FileNotFoundError` with a
        clear message → gate blocks.
      * topology mismatch (e.g. wrong input shape) → `RuntimeError` from
        ORT → caught per-example by SanityRunner.run, counted as an
        error, and surfaced via `failing_classes` / `error_count`.
    """
    # Existence check FIRST so a missing file surfaces as
    # `FileNotFoundError` (the gate's "candidate file missing"
    # branch) regardless of whether onnxruntime is installed —
    # otherwise the gate sees a noisy ModuleNotFoundError when the
    # genuine problem was upstream.
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)

    vocab_path = _sidecar_path(candidate_path, "vocab.json")
    idf_path = _sidecar_path(candidate_path, "idf.npy")
    _require_sidecar(vocab_path, "intent", candidate_path)
    _require_sidecar(idf_path, "intent", candidate_path)

    import numpy as np
    from onnxruntime import InferenceSession

    vocab = json.loads(vocab_path.read_text())
    idf = np.load(str(idf_path))

    session = InferenceSession(str(candidate_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    _ngram_range = (3, 5)
    _n_features = len(idf) if idf is not None and idf.ndim == 1 else len(vocab)

    def _featurize(text: str) -> np.ndarray:
        text_lower = f" {text.lower()} "
        ngrams: dict[str, int] = {}
        for n in range(_ngram_range[0], _ngram_range[1] + 1):
            for i in range(len(text_lower) - n + 1):
                gram = text_lower[i:i + n]
                if gram in vocab:
                    ngrams[gram] = ngrams.get(gram, 0) + 1
        tf_vector = np.zeros(_n_features, dtype=np.float32)
        for gram, count in ngrams.items():
            idx = vocab.get(gram)
            if idx is not None and idx < _n_features:
                tf_vector[idx] = np.log1p(count)  # sublinear_tf
        if idf is not None:
            tf_vector *= idf
        norm = np.linalg.norm(tf_vector)
        if norm > 0:
            tf_vector /= norm
        return tf_vector.reshape(1, -1)

    def _infer(text: Any) -> str:
        # Defensive truncation matches the production inference site
        # (`_intent_infer_on_session` in classifier/unified.py); a fixture
        # row longer than the trainer's max would surprise the model.
        s = str(text)[:1000]
        features = _featurize(s)
        features = np.nan_to_num(features, nan=0.0, posinf=1.0, neginf=-1.0)
        outputs = session.run(None, {input_name: features})
        return str(outputs[0][0])

    return _infer


# ── Side-car path helpers ─────────────────────────────────────────────────


def _sidecar_path(candidate_path: Path, suffix: str) -> Path:
    """Return the sibling side-car path for `candidate_path`.

    `candidate_path` is `…/candidates/{model}-{version}.onnx`; side-cars
    live alongside as `…/candidates/{model}-{version}.{suffix}` (e.g.
    `safety-v1.featurizer_ref.json`). Strips the trailing `.onnx` and
    re-suffixes rather than parsing the filename — a candidate naming
    scheme that drifts from `{model}-{version}.onnx` would still produce
    a coherent side-car path here (the trainer is the source of truth
    for the name).
    """
    stem = candidate_path.stem  # e.g. "safety-v1"
    return candidate_path.parent / f"{stem}.{suffix}"


def _require_sidecar(path: Path, model: str, candidate: Path) -> None:
    """Raise FileNotFoundError with a clear message if a side-car is missing.

    The gate's `_run_sanity_check` translates FileNotFoundError into
    a sanity FAILURE (block promotion) — the message here explains
    WHICH file is missing so the operator can tell whether the
    candidate was emitted by a stale trainer (no side-cars) versus
    a transient FS issue.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"sanity adapter for {model!r}: side-car missing at {path} "
            f"(candidate {candidate.name} likely emitted by a trainer "
            "that doesn't write side-cars; re-train to produce a complete "
            "candidate set)"
        )


# ── Safety (fully wired, production-shape) ────────────────────────────────


def _safety_adapter(candidate_path: Path) -> Callable[[Any], str]:
    """Build an `infer_fn` for the `safety` candidate.

    The candidate ONNX is shape-compatible with production
    `safety_classifier.onnx`: `(None, 200)` float input, int64 label
    output. Featurization runs OUTSIDE the graph using the SAME
    production sidecars
    (`gateway.content.safety_*.{json,npy}`) that the production class
    loads at serve time. The trainer side-cars
    (`labels.json` + `featurizer_ref.json`) are required as a
    contract check (their absence means the candidate was emitted by
    a stale trainer revision and the ONNX shape is presumed wrong)
    but the adapter does not consume them as featurizer state.

    The output of `session.run` is an `int64` index; the adapter maps
    it back to a label string via the candidate's `labels.json`. That
    file MUST match the production labels list — the trainer
    enforces this at fit time, so any mismatch here is a deeper
    pipeline corruption.

    Misuse modes:
      * candidate file missing → `FileNotFoundError` → gate blocks.
      * any required side-car missing → `FileNotFoundError` with a
        clear message → gate blocks (the candidate is treated as
        un-validated rather than silently approved).
      * production featurizer files missing or unparseable →
        adapter raises (`FileNotFoundError` / `ValueError`) → gate
        blocks. Without those files we cannot featurize fixtures
        the same way production does.
    """
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)

    labels_path = _sidecar_path(candidate_path, "labels.json")
    ref_path = _sidecar_path(candidate_path, "featurizer_ref.json")
    _require_sidecar(labels_path, "safety", candidate_path)
    _require_sidecar(ref_path, "safety", candidate_path)

    import numpy as np
    from onnxruntime import InferenceSession

    # Trainer side-car: label list. Index lookup target for the
    # candidate ONNX's int64 output.
    labels = json.loads(labels_path.read_text())
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise ValueError(
            f"safety sanity adapter: trainer labels.json at {labels_path} "
            "is not a list[str]"
        )

    # Production featurizer state — same files SafetyClassifier loads
    # at serve time. We deliberately ignore the trainer's side-cars
    # for featurization to avoid drift between candidate and
    # production.
    from gateway.intelligence.distillation.trainers.safety_trainer import (
        _load_production_featurizer,
        featurize_batch,
    )
    feat = _load_production_featurizer()
    logger.info(
        "safety sanity adapter: loaded production featurizer "
        "(%d labels, expected_input_dim=%d)",
        len(labels), feat.svd_components.shape[0],
    )

    session = InferenceSession(str(candidate_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    def _infer(text: Any) -> str:
        s = str(text)[:5000]
        features = featurize_batch([s], feat)
        outputs = session.run(None, {input_name: features})
        idx = int(np.asarray(outputs[0])[0])
        # int64 → label string. Out-of-range indices fall back to the
        # production "safe" label so the runner still gets a string
        # back rather than raising; the SanityRunner will mark it as
        # a wrong prediction for the fixture's expected class.
        if 0 <= idx < len(labels):
            return labels[idx]
        return "safe"

    return _infer


# ── Schema mapper (fully wired, production-shape) ─────────────────────────


def _schema_mapper_adapter(candidate_path: Path) -> Callable[[Any], str]:
    """Build an `infer_fn` for the `schema_mapper` candidate.

    The candidate ONNX is shape-compatible with production
    `schema_mapper.onnx`: `(None, 139)` float input, int64 label
    output. Featurization runs OUTSIDE the graph using the SAME
    `gateway.schema.features.extract_features` function the
    production `SchemaMapper._classify_onnx` calls at serve time.
    Each fixture row is interpreted in the same priority order the
    trainer applies (FlatField-like dict → raw response JSON →
    placeholder zero vector); see
    `gateway.intelligence.distillation.trainers.schema_trainer` docstring.

    Trainer side-cars (`labels.json`, `featurizer_ref.json`) are
    required as a contract check.

    Misuse modes:
      * candidate file missing → `FileNotFoundError` → gate blocks.
      * any required side-car missing → `FileNotFoundError` → gate blocks.
    """
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)

    labels_path = _sidecar_path(candidate_path, "labels.json")
    ref_path = _sidecar_path(candidate_path, "featurizer_ref.json")
    _require_sidecar(labels_path, "schema_mapper", candidate_path)
    _require_sidecar(ref_path, "schema_mapper", candidate_path)

    import numpy as np
    from onnxruntime import InferenceSession

    labels = json.loads(labels_path.read_text())
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise ValueError(
            f"schema_mapper sanity adapter: trainer labels.json at {labels_path} "
            "is not a list[str]"
        )

    from gateway.intelligence.distillation.trainers.schema_trainer import (
        _load_production_featurizer,
        _featurize_row,
    )
    feat = _load_production_featurizer()
    logger.info(
        "schema_mapper sanity adapter: loaded production featurizer "
        "(%d labels, expected_input_dim=%d)",
        len(labels), feat.feature_dim,
    )

    session = InferenceSession(str(candidate_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    def _infer(row: Any) -> str:
        # `_featurize_row` is itself defensive — it accepts dicts,
        # JSON strings, and falls back to a zero vector for
        # un-coercible inputs.
        vec = _featurize_row(row, feat.feature_dim).reshape(1, -1).astype(np.float32)
        vec = np.nan_to_num(vec, nan=0.0, posinf=1.0, neginf=-1.0)
        outputs = session.run(None, {input_name: vec})
        idx = int(np.asarray(outputs[0])[0])
        if 0 <= idx < len(labels):
            return labels[idx]
        return "UNKNOWN"

    return _infer


# ── Strategy table ────────────────────────────────────────────────────────


_ADAPTERS: dict[str, SanityAdapter] = {
    "intent": _intent_adapter,
    "safety": _safety_adapter,
    "schema_mapper": _schema_mapper_adapter,
}


# Models with a fully-wired adapter. Now that every canonical model has
# an adapter that loads its trainer-emitted side-cars, all three names
# are wired. `is_wired` is kept as a function (rather than inlining
# the `model_name in WIRED_MODELS` check) so the gate doesn't need a
# rewrite if a future model is added in a deferred state.
WIRED_MODELS: frozenset[str] = frozenset({"intent", "safety", "schema_mapper"})


def build_infer_fn(model_name: str, candidate_path: Path) -> Callable[[Any], str]:
    """Return the inference callable for `model_name`'s candidate.

    Raises `KeyError` for an unknown model name (canonical names live in
    `ModelRegistry.ALLOWED_MODEL_NAMES`) and `FileNotFoundError` when a
    candidate ONNX or any required side-car is missing. Both surface
    in the gate as a sanity failure (block promotion).
    """
    factory = _ADAPTERS[model_name]
    return factory(candidate_path)


def is_wired(model_name: str) -> bool:
    """Whether `model_name` has a real adapter (i.e. sanity actually runs)."""
    return model_name in WIRED_MODELS
