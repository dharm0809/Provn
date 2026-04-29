"""Per-model `infer_fn` factories for the SanityRunner gate.

`SanityRunner.run(model_name, infer_fn)` is intentionally model-agnostic:
the runner just iterates a labeled fixture and calls `infer_fn(input)` on
each example. The model-specific plumbing — how to build an
`InferenceSession`, how to encode the input, how to decode the output back
into a label string — lives here as a strategy table keyed by model name.

Wiring status:

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
  * safety          — fully wired. The trainer
                      (`gateway.intelligence.distillation.trainers.safety_trainer`)
                      exports a classifier-only ONNX with
                      `FloatTensorType([None, n_features])` input
                      (skl2onnx cannot convert `analyzer='char_wb'`
                      TfidfVectorizer in-graph) plus three side-cars
                      per candidate:
                      `safety-{version}.labels.json`,
                      `safety-{version}.vocab.json`,
                      `safety-{version}.idf.npy`.
                      The adapter REQUIRES the side-cars to exist — a
                      candidate emitted by a stale trainer that wrote
                      only the ONNX surfaces as a sanity FAILURE
                      (FileNotFoundError → block promotion). At
                      inference the adapter applies char_wb TF-IDF in
                      Python (using vocab + idf) and feeds the
                      resulting float matrix to the candidate session
                      — mirroring the production
                      `SafetyClassifier._tfidf_transform` split.
  * schema_mapper   — fully wired. The trainer
                      (`gateway.intelligence.distillation.trainers.schema_trainer`)
                      exports a classifier-only ONNX with
                      `FloatTensorType([None, n_features])` input
                      whose column ordering is fixed by a
                      `DictVectorizer.feature_names_`. Side-cars
                      emitted: `schema_mapper-{version}.dictvec.pkl`
                      (fitted DictVectorizer) and
                      `schema_mapper-{version}.feature_names.json`.
                      The adapter loads the pickled DictVectorizer to
                      transform fixture row-dicts into the float
                      matrix the candidate ONNX expects, then runs
                      the ORT session.

Backward compatibility: candidates emitted by older trainer revisions
that don't write side-cars surface as `FileNotFoundError` (with a
clear message identifying the missing file). `_run_sanity_check`
treats this as a sanity FAILURE and blocks promotion — promotion is
never silently approved on the basis of "no side-car available".

Pickle policy (schema_mapper): see `schema_trainer.py` docstring. The
.dictvec.pkl file is produced and consumed locally on the same
gateway host under the controlled `candidates/` directory; it never
crosses a network boundary, and the adapter only ever loads from
that one fixed path.
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
    `safety-v1.vocab.json`). Strips the trailing `.onnx` and re-suffixes
    rather than parsing the filename — a candidate naming scheme that
    drifts from `{model}-{version}.onnx` would still produce a coherent
    side-car path here (the trainer is the source of truth for the name).
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


# ── Safety (fully wired) ──────────────────────────────────────────────────


def _safety_adapter(candidate_path: Path) -> Callable[[Any], str]:
    """Build an `infer_fn` for the `safety` candidate.

    The trainer exports a classifier-only ONNX with
    `FloatTensorType([None, n_features])` input — the char_wb
    TfidfVectorizer is NOT in the graph (skl2onnx only supports
    `tokenizer='word'` for in-graph TF-IDF). The adapter therefore
    reproduces char_wb TF-IDF in Python from the side-cars
    (`vocab.json`, `idf.npy`) and feeds the float matrix to the
    session — mirroring the production
    `SafetyClassifier._tfidf_transform` split.

    Misuse modes:
      * candidate file missing → `FileNotFoundError` → gate blocks.
      * any required side-car missing → `FileNotFoundError` with a
        clear message → gate blocks (the candidate is treated as
        un-validated rather than silently approved).
    """
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)

    labels_path = _sidecar_path(candidate_path, "labels.json")
    vocab_path = _sidecar_path(candidate_path, "vocab.json")
    idf_path = _sidecar_path(candidate_path, "idf.npy")
    _require_sidecar(labels_path, "safety", candidate_path)
    _require_sidecar(vocab_path, "safety", candidate_path)
    _require_sidecar(idf_path, "safety", candidate_path)

    import numpy as np
    from onnxruntime import InferenceSession

    # Validate side-cars parse — corrupted JSON / npy is a
    # well-formedness failure too. The exception bubbles for the
    # gate to record.
    labels = json.loads(labels_path.read_text())
    vocab = json.loads(vocab_path.read_text())
    idf = np.load(str(idf_path))
    logger.info(
        "safety sanity adapter: loaded side-cars (%d labels, %d vocab terms, idf=%s)",
        len(labels), len(vocab), tuple(idf.shape),
    )

    session = InferenceSession(str(candidate_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    # The trainer's TF-IDF uses ngram_range=(3, 5) and char_wb
    # boundaries — match those exactly so the float matrix lines up
    # with what the classifier was trained on.
    _ngram_range = (3, 5)
    _n_features = len(idf) if idf is not None and idf.ndim == 1 else len(vocab)

    def _featurize(text: str) -> np.ndarray:
        # char_wb adds a single space at each word boundary; for the
        # whole-string fallback we just pad with spaces. Mirrors
        # `SafetyClassifier._tfidf_transform`.
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
        s = str(text)[:5000]
        features = _featurize(s)
        features = np.nan_to_num(features, nan=0.0, posinf=1.0, neginf=-1.0)
        outputs = session.run(None, {input_name: features})
        return str(outputs[0][0])

    return _infer


# ── Schema mapper (fully wired) ───────────────────────────────────────────


def _schema_mapper_adapter(candidate_path: Path) -> Callable[[Any], str]:
    """Build an `infer_fn` for the `schema_mapper` candidate.

    The candidate ONNX is classifier-only with a
    `FloatTensorType([None, n_features])` input — the DictVectorizer
    that was fit during training is NOT in the graph. The adapter
    therefore loads the pickled DictVectorizer side-car and uses it
    to `.transform([row_dict])` each fixture input into the float
    matrix the candidate expects.

    Pickle is used by deliberate convention here (see schema_trainer
    docstring): the file lives under the controlled `candidates/`
    directory on the same gateway host that produced it, never
    crosses a network boundary, and the adapter only ever loads from
    that one fixed location. JSON-serializing a fitted sklearn
    estimator isn't supported and the alternative (reimplementing
    DictVectorizer.transform from feature_names.json) would silently
    diverge from sklearn's actual semantics if sklearn ever changes.

    Misuse modes:
      * candidate file missing → `FileNotFoundError` → gate blocks.
      * dictvec.pkl side-car missing → `FileNotFoundError` → gate blocks.
      * pickle load fails (corrupt / wrong sklearn version) →
        exception bubbles → gate blocks (treated as a failure).
    """
    if not candidate_path.exists():
        raise FileNotFoundError(candidate_path)

    pkl_path = _sidecar_path(candidate_path, "dictvec.pkl")
    names_path = _sidecar_path(candidate_path, "feature_names.json")
    _require_sidecar(pkl_path, "schema_mapper", candidate_path)
    # feature_names.json is informational (the pickle carries the
    # ordering inside DictVectorizer.feature_names_) but its absence
    # still indicates an incomplete trainer write.
    _require_sidecar(names_path, "schema_mapper", candidate_path)

    import numpy as np
    from onnxruntime import InferenceSession

    # Local trusted path — see module docstring on pickle policy.
    import pickle  # noqa: S403
    with open(pkl_path, "rb") as fh:
        vec = pickle.load(fh)  # noqa: S301

    # Cross-check: feature_names.json should match vec.feature_names_;
    # a mismatch means the side-cars are inconsistent. Log but don't
    # raise — the pickle is the authoritative source.
    try:
        names_json = json.loads(names_path.read_text())
        if list(getattr(vec, "feature_names_", [])) != list(names_json):
            logger.warning(
                "schema_mapper sanity adapter: feature_names.json does not "
                "match pickled DictVectorizer.feature_names_ "
                "(json=%d items, pkl=%d items)",
                len(names_json), len(getattr(vec, "feature_names_", [])),
            )
    except (ValueError, OSError):
        logger.debug(
            "schema_mapper sanity adapter: feature_names.json unparseable",
            exc_info=True,
        )

    session = InferenceSession(str(candidate_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    n_features = len(getattr(vec, "feature_names_", []) or [])
    logger.info(
        "schema_mapper sanity adapter: loaded DictVectorizer "
        "(%d features) for %s", n_features, candidate_path.name,
    )

    def _infer(row: Any) -> str:
        # Coerce to a flat dict[str, float] — fixture inputs are
        # dicts but defensively handle JSON strings (matches the
        # trainer's `_parse_features`).
        if isinstance(row, str):
            try:
                row = json.loads(row)
            except (ValueError, TypeError):
                row = {}
        if not isinstance(row, dict):
            row = {}
        # DictVectorizer.transform returns a (1, n_features) matrix
        # in the trained column ordering. Cast to float32 — the
        # candidate ONNX was exported with FloatTensorType (= float32).
        matrix = vec.transform([row]).astype(np.float32)
        # Defensive nan/inf cleanup mirroring the production
        # SchemaMapper inference site (`_classify_onnx`).
        matrix = np.nan_to_num(matrix, nan=0.0, posinf=1.0, neginf=-1.0)
        outputs = session.run(None, {input_name: matrix})
        return str(outputs[0][0])

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
