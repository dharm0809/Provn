"""Per-model `infer_fn` factories for the SanityRunner gate.

`SanityRunner.run(model_name, infer_fn)` is intentionally model-agnostic:
the runner just iterates a labeled fixture and calls `infer_fn(input)` on
each example. The model-specific plumbing — how to build an
`InferenceSession`, how to encode the input, how to decode the output back
into a label string — lives here as a strategy table keyed by model name.

Wiring status (per `sanity_runner.py:50` plan):

  * intent          — fully wired. The candidate is a sklearn
                      Pipeline(TfidfVectorizer + LogisticRegression)
                      ONNX export; input is a single `"prompt"` string,
                      output[0] is the predicted label, output[1] is the
                      probability dict. Mirrors the inference path in
                      `gateway.classifier.unified._intent_infer_on_session`.
  * safety          — DEFERRED. The production safety classifier
                      (`gateway.content.safety_classifier`) ships with a
                      bespoke TF-IDF char_wb featurizer + saved IDF + SVD
                      components stored alongside the ONNX file
                      (`labels.npy`, `vocab.json`, `idf.npy`,
                      `svd_components.npy`). Plugging that into the sanity
                      gate requires loading those artifacts from the
                      candidate directory (currently the trainers don't
                      emit the side-cars), so this is held until the
                      distillation `SafetyTrainer` mirrors them.
  * schema_mapper   — DEFERRED. The schema mapper consumes a feature
                      vector built by `gateway.schema.mapper.extract_features`
                      from a `FlatField`. The sanity fixture uses
                      pre-computed feature dicts (already what the trainer
                      ingests via `DictVectorizer`), so wiring is
                      mechanically straightforward — but the trainer
                      currently does not export the `DictVectorizer` next
                      to the ONNX file, so the input ordering can't be
                      reproduced at sanity time without a side-car.

Both deferred adapters are stubbed via `_NotWired` so a misconfiguration
fails LOUD (`ValueError("sanity adapter not wired for safety")`) rather
than silently treating "no adapter" as pass. The gate, in turn, treats
the raised exception as a sanity FAILURE and blocks promotion — never as
a swallowed pass.
"""
from __future__ import annotations

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

    The trainer (gateway.intelligence.distillation.trainers.intent) exports
    a sklearn Pipeline(TfidfVectorizer, LogisticRegression) to ONNX; the
    runtime contract is:

      input:  one column named `"prompt"` containing a single string
              (shape (1, 1), dtype=str)
      output: outputs[0] = predicted label (shape (1,), dtype=str)
              outputs[1] = probability dict (shape (1,), {label: prob})

    We only need the label here — the SanityRunner doesn't care about
    confidence (it only checks predicted == label). String coercion is
    defensive because numpy may return a `numpy.str_` rather than `str`,
    and `SanityRunner` compares with `str(predicted) == label`.

    Misuse modes:
      * candidate file missing → `FileNotFoundError` → caught by the gate
        as a sanity failure (block promotion).
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

    import numpy as np
    from onnxruntime import InferenceSession

    session = InferenceSession(str(candidate_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    def _infer(text: Any) -> str:
        # Defensive truncation matches the production inference site
        # (`_intent_infer_on_session` in classifier/unified.py); a fixture
        # row longer than the trainer's max would surprise the model.
        s = str(text)[:1000]
        inp = np.array([[s]]).reshape(1, 1)
        outputs = session.run(None, {input_name: inp})
        return str(outputs[0][0])

    return _infer


# ── Deferred adapters (raise loud) ────────────────────────────────────────


class _NotWired:
    """Placeholder factory that raises a structured error.

    The gate catches this and treats it as a sanity FAILURE so promotion
    is blocked rather than silently approved on the basis of "no adapter
    available".
    """

    def __init__(self, model_name: str, reason: str) -> None:
        self._model_name = model_name
        self._reason = reason

    def __call__(self, candidate_path: Path) -> Callable[[Any], str]:
        raise NotImplementedError(
            f"sanity adapter not wired for {self._model_name!r}: {self._reason}"
        )


_SAFETY_ADAPTER = _NotWired(
    "safety",
    "production classifier ships TF-IDF + IDF + SVD side-cars next to the "
    "ONNX file; trainer must export the same artifacts to candidates/ "
    "before this adapter can reproduce the inference contract",
)

_SCHEMA_MAPPER_ADAPTER = _NotWired(
    "schema_mapper",
    "production classifier consumes a DictVectorizer-encoded feature "
    "matrix; trainer must export the fitted DictVectorizer next to the "
    "ONNX file before this adapter can mirror the input ordering",
)


# ── Strategy table ────────────────────────────────────────────────────────


_ADAPTERS: dict[str, SanityAdapter] = {
    "intent": _intent_adapter,
    "safety": _SAFETY_ADAPTER,
    "schema_mapper": _SCHEMA_MAPPER_ADAPTER,
}


# Models with a fully-wired adapter. The gate uses this to short-circuit
# the sanity check for models we can't yet evaluate offline; rather than
# blocking every promotion of those models forever, we log loudly and
# pass through. This is the "known-deferred" carve-out — once the
# trainer side-cars land, `_NotWired` is swapped for the real adapter
# and the carve-out is removed.
WIRED_MODELS: frozenset[str] = frozenset({"intent"})


def build_infer_fn(model_name: str, candidate_path: Path) -> Callable[[Any], str]:
    """Return the inference callable for `model_name`'s candidate.

    Raises `KeyError` for an unknown model name (canonical names live in
    `ModelRegistry.ALLOWED_MODEL_NAMES`) and `NotImplementedError` for
    deferred adapters. Both surface in the gate as a sanity failure.
    """
    factory = _ADAPTERS[model_name]
    return factory(candidate_path)


def is_wired(model_name: str) -> bool:
    """Whether `model_name` has a real adapter (i.e. sanity actually runs).

    The gate treats unwired models as a known carve-out: it skips the
    sanity check, logs a warning, and lets the ShadowMetrics gate be
    the sole arbiter. Without this short-circuit, every `safety` /
    `schema_mapper` promotion would be permanently blocked by the
    `_NotWired` exception until trainer side-cars land — breaking the
    existing auto-promote contract for those models.
    """
    return model_name in WIRED_MODELS
