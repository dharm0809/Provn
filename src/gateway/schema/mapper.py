"""SchemaMapper — ML-powered JSON response mapping.

Maps any LLM API response to the canonical schema using a trained ONNX model
that understands VALUE SEMANTICS (not just field names). The model classifies
each field in a JSON response by analyzing its value type, magnitude,
relationships with siblings, structural context, and key name tokens.

Usage:
    mapper = SchemaMapper()  # loads ONNX model
    canonical = mapper.map_response(raw_json_dict)
    # canonical.content, canonical.usage.prompt_tokens, etc.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from gateway.schema.canonical import (
    CanonicalCitation,
    CanonicalResponse,
    CanonicalSafety,
    CanonicalTiming,
    CanonicalToolCall,
    CanonicalUsage,
    ENVELOPE_KEYS,
    ENVELOPE_LABEL,
    ENVELOPE_PATH_DISQUALIFIERS,
    IDX_TO_LABEL,
    MappingReport,
    SINGLETON_FIELDS,
    USAGE_FIELDS,
)
from gateway.schema.features import FlatField, extract_features, flatten_json

if TYPE_CHECKING:
    from gateway.intelligence.registry import ModelRegistry
    from gateway.intelligence.verdict_buffer import VerdictBuffer

logger = logging.getLogger(__name__)

_MODEL_DIR = Path(__file__).parent
_ONNX_PATH = _MODEL_DIR / "schema_mapper.onnx"
_LABELS_PATH = _MODEL_DIR / "schema_mapper_labels.json"


class LabelsMismatchError(RuntimeError):
    """Raised when ``schema_mapper_labels.json`` row count diverges from the
    ONNX model's emitted class count.

    Catching this is intentional: the gateway should fail loud at boot
    rather than silently mislabel every request. ``main.py``'s SchemaMapper
    init site already wraps construction in a broad ``except`` for fail-open
    behaviour; that wrapper logs the error and the gateway degrades to the
    heuristic fallback path until the labels file is fixed.
    """


# Path-name patterns that strongly indicate a canonical field.
# Used as safety net when ONNX says UNKNOWN but the path is obvious,
# and reused by the Phase 25 SchemaMapper harvester to label overflow
# keys for training signal capture. Format:
#   (path must contain ALL of these tokens, leaf key must match exactly, → label)
# Module-level so the harvester can import without touching class internals.
#
# The trailing block of `ENVELOPE_LABEL` rules covers provider response-shape
# boilerplate (``object``, ``created``, ``role``, …) — the same set declared
# in ``canonical.ENVELOPE_KEYS``. These entries make the harvester back-write
# ``divergence_signal="envelope"`` on the corresponding verdict row, giving
# the trainer a positive label for "the model correctly UNKNOWN'd a piece of
# envelope". After D3's filtering these keys never make it to overflow at
# all, but the rule entries remain so legacy verdict rows captured before
# the filter rollout can still feed the distillation pipeline.
_PATH_FALLBACK_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("content",), "content", "content"),
    (("text",), "text", "content"),
    (("generated",), "generated_text", "content"),
    (("output",), "outputText", "content"),
    (("output",), "output", "content"),
    (("reasoning",), "reasoning_content", "thinking_content"),
    (("reasoning",), "reasoning", "thinking_content"),
    (("thinking",), "thinking", "thinking_content"),
    (("tool_plan",), "tool_plan", "thinking_content"),
    (("finish",), "finish_reason", "finish_reason"),
    (("stop",), "stop_reason", "finish_reason"),
    (("done",), "done_reason", "finish_reason"),
    (("completion",), "completionReason", "finish_reason"),
    (("status",), "status", "finish_reason"),
    (("prompt",), "prompt_tokens", "prompt_tokens"),
    (("input",), "input_tokens", "prompt_tokens"),
    (("prompt",), "promptTokenCount", "prompt_tokens"),
    (("prompt",), "prompt_eval_count", "prompt_tokens"),
    (("input",), "inputTextTokenCount", "prompt_tokens"),
    (("completion",), "completion_tokens", "completion_tokens"),
    (("output",), "output_tokens", "completion_tokens"),
    (("candidates",), "candidatesTokenCount", "completion_tokens"),
    (("eval",), "eval_count", "completion_tokens"),
    (("token",), "tokenCount", "completion_tokens"),
    (("generated",), "generated_tokens", "completion_tokens"),
    (("total",), "total_tokens", "total_tokens"),
    (("total",), "totalTokenCount", "total_tokens"),
    (("cache",), "cached_tokens", "cached_tokens"),
    (("cache", "read"), "cache_read_input_tokens", "cached_tokens"),
    (("cache", "hit"), "prompt_cache_hit_tokens", "cached_tokens"),
    (("cache", "creation"), "cache_creation_input_tokens", "cache_creation_tokens"),
    # Envelope keys — no path-token gating, the key alone is the signal.
    ((), "object", ENVELOPE_LABEL),
    ((), "created", ENVELOPE_LABEL),
    ((), "index", ENVELOPE_LABEL),
    ((), "role", ENVELOPE_LABEL),
    ((), "refusal", ENVELOPE_LABEL),
    ((), "logprobs", ENVELOPE_LABEL),
    ((), "service_tier", ENVELOPE_LABEL),
    ((), "system_fingerprint", ENVELOPE_LABEL),
    ((), "type", ENVELOPE_LABEL),
    ((), "stop_sequence", ENVELOPE_LABEL),
)


# Provider-deterministic path map — runs BEFORE the ONNX classifier.
#
# Why this exists: the ONNX GradientBoosting was trained on shallow shapes and
# confidently misclassifies nested ``*_details`` sub-objects. e.g. for OpenAI's
# ``usage.prompt_tokens_details.cached_tokens`` (an int whose path contains
# "prompt"), the model emits ``prompt_tokens`` with p=1.0. Because the label
# isn't UNKNOWN, ``_apply_path_fallbacks`` cannot rescue it; the field then
# collides with the real ``usage.prompt_tokens`` slot during assembly. Audit
# records end up with corrupted token counts (verified end-to-end against
# Walacor backend).
#
# Provider response shapes are stable and documented. Encoding them here as
# exact-path rules makes the canonical mapping 100% precise for the known
# providers (OpenAI, Anthropic, Ollama) while the ONNX model continues to
# serve as a fallback for novel/custom adapters whose paths aren't covered.
#
# Each entry: ``"exact.dotted.path" -> canonical_label``. ``UNKNOWN`` is the
# explicit "no canonical class for this field" verdict that wins over the
# ONNX prediction — preventing the silent slot collision described above.
# When iterating sequences, the path uses ``.0.``, ``.1.``, etc. (matching
# ``flatten_json`` output).
_PROVIDER_PATH_MAP: dict[str, str] = {
    # ── OpenAI chat completions ─────────────────────────────────
    "usage.prompt_tokens": "prompt_tokens",
    "usage.completion_tokens": "completion_tokens",
    "usage.total_tokens": "total_tokens",
    "usage.prompt_tokens_details.cached_tokens": "cached_tokens",
    "usage.prompt_tokens_details.audio_tokens": "UNKNOWN",
    "usage.completion_tokens_details.reasoning_tokens": "reasoning_tokens",
    "usage.completion_tokens_details.audio_tokens": "UNKNOWN",
    "usage.completion_tokens_details.accepted_prediction_tokens": "UNKNOWN",
    "usage.completion_tokens_details.rejected_prediction_tokens": "UNKNOWN",
    # ── Anthropic /v1/messages ──────────────────────────────────
    "usage.input_tokens": "prompt_tokens",
    "usage.output_tokens": "completion_tokens",
    "usage.cache_creation_input_tokens": "cache_creation_tokens",
    "usage.cache_read_input_tokens": "cached_tokens",
    # Anthropic 5m/1h cache buckets (Sonnet 4.5+)
    "usage.cache_creation.ephemeral_5m_input_tokens": "cache_creation_tokens",
    "usage.cache_creation.ephemeral_1h_input_tokens": "cache_creation_tokens",
    # ── Ollama /api/chat top-level fields ───────────────────────
    "prompt_eval_count": "prompt_tokens",
    "eval_count": "completion_tokens",
    "prompt_eval_duration": "timing_value",
    "eval_duration": "timing_value",
    "load_duration": "timing_value",
    "total_duration": "timing_value",
    # ── Canonical-content paths (deterministic-first expansion) ──
    # These previously fell through to the ONNX residual. The label set
    # here is intentionally decoupled from the 19-class ONNX
    # `schema_mapper_labels.json` (the map already emits `reasoning_tokens`,
    # which isn't an ONNX label) — `_assemble` keys off the canonical
    # label, not the ONNX index, so no retrain is needed.
    #
    # Envelope boilerplate (`object`, `created`, `role`, `index`,
    # `system_fingerprint`, …) is deliberately NOT added here: those are
    # already tagged `envelope` by `_apply_path_fallbacks`/`_is_envelope_field`
    # and excluded from overflow. Remapping them would be a behavior
    # change for known providers, which this expansion explicitly avoids.
    #
    # Array paths are always `.0.`: `flatten_json` only recurses into the
    # first element of any list ("providers use consistent array
    # structures"), so an exact-match dict suffices — no globbing/
    # normalization. Caveat: when an Anthropic `content[]` has block 0 =
    # tool_use and a later block = text, the text block is not flattened
    # at all (pre-existing `flatten_json` element-0-only limitation, out
    # of scope here).
    #
    # Shared top-level identity fields (OpenAI + Anthropic):
    "id": "response_id",
    "model": "model",
    # OpenAI Chat Completions:
    "choices.0.message.content": "content",
    "choices.0.message.reasoning_content": "thinking_content",
    "choices.0.finish_reason": "finish_reason",
    "choices.0.message.tool_calls.0.id": "tool_call_id",
    "choices.0.message.tool_calls.0.type": "tool_call_type",
    "choices.0.message.tool_calls.0.function.name": "tool_call_name",
    "choices.0.message.tool_calls.0.function.arguments": "tool_call_arguments",
    # Anthropic /v1/messages (content block 0 is exactly one type, so
    # these paths are mutually exclusive and self-disambiguating):
    "content.0.text": "content",
    "content.0.thinking": "thinking_content",
    "content.0.id": "tool_call_id",
    "content.0.name": "tool_call_name",
    # Anthropic tool_use `input` is an OBJECT (vs OpenAI's stringified
    # args). Tagging the container as tool_call_arguments is still
    # strictly better than the prior ONNX→overflow path; sub-keys
    # overflow harmlessly (raw value is hashed by Walacor regardless).
    "content.0.input": "tool_call_arguments",
    "content.0.citations.0.url": "citation_url",
    "stop_reason": "finish_reason",
    # Ollama /api/chat (distinct paths from OpenAI's choices[].message):
    "message.content": "content",
    "message.thinking": "thinking_content",
    "message.reasoning_content": "thinking_content",
    "done_reason": "finish_reason",
    "message.tool_calls.0.function.name": "tool_call_name",
    "message.tool_calls.0.function.arguments": "tool_call_arguments",
}


def _is_envelope_field(key: str, path: str) -> bool:
    """Should ``(key, path)`` be tagged as ENVELOPE rather than UNKNOWN?

    Rule:
    * ``key`` must be in ``ENVELOPE_KEYS`` (case-sensitive — these are
      well-known provider keys, not heuristics).
    * No ancestor path segment may collide with an
      ``ENVELOPE_PATH_DISQUALIFIERS`` entry (e.g. ``arguments.role``
      inside a tool call: ``role`` could be user data, so we don't
      tag).

    Pure function — module-level so the heuristic, the fallback rewriter,
    and the harvester all share one definition.
    """
    if key not in ENVELOPE_KEYS:
        return False
    # Walk the dotted path's parent segments. ``path.split(".")[:-1]`` is
    # every ancestor; we strip ``[idx]`` suffixes that ``flatten_json``
    # leaves on array indices.
    for seg in path.split(".")[:-1]:
        bracket = seg.find("[")
        if bracket != -1:
            seg = seg[:bracket]
        if seg in ENVELOPE_PATH_DISQUALIFIERS:
            return False
    return True


def classify_overflow_path(path: str) -> str | None:
    """Return the canonical label a path would receive via fallback rules, or None.

    Splits the leaf (final dotted segment, stripping trailing indices like
    `[0]`) and runs the same `leaf + path-token` match used by
    `SchemaMapper._apply_path_fallbacks`. The Phase 25 harvester uses this
    against the overflow-keys list captured at audit time to produce
    training signal for the distillation pipeline.

    Envelope keys go through the shared ``_is_envelope_field`` gate so
    paths in user-data scopes (``arguments.role``, ``input.type``) are
    NOT tagged envelope — they may carry user-defined content.
    """
    if not path:
        return None
    # Derive the leaf key: split on '.' and strip any `[index]` suffix.
    leaf = path.split(".")[-1]
    bracket_idx = leaf.find("[")
    if bracket_idx != -1:
        leaf = leaf[:bracket_idx]
    leaf_lower = leaf.lower()
    path_lower = path.lower()
    for path_tokens, leaf_match, target_label in _PATH_FALLBACK_RULES:
        if leaf_lower != leaf_match.lower() and leaf != leaf_match:
            continue
        if not all(tok in path_lower for tok in path_tokens):
            continue
        if target_label == ENVELOPE_LABEL and not _is_envelope_field(leaf, path):
            # Envelope rule matched on leaf, but the path's parent scope
            # disqualifies it (e.g. ``arguments.role``).
            continue
        return target_label
    return None


class SchemaMapper:
    """Maps any LLM API response JSON to the canonical schema.

    Loads an ONNX GradientBoosting model trained on value-aware features
    from 22 real provider formats. Falls back to heuristic mapping if
    ONNX is unavailable.
    """

    def __init__(
        self,
        onnx_path: str | None = None,
        verdict_buffer: "VerdictBuffer | None" = None,
        registry: "ModelRegistry | None" = None,
        model_name: str | None = None,
        intelligence_db: "Any | None" = None,
    ) -> None:
        self._session = None
        self._input_name = ""
        self._labels: list[str] = []
        self._label_to_idx: dict[str, int] = {}
        # The actual labels the LOADED ONNX binary can emit, derived from the
        # output_probability map keys at session-adoption time. Distinct from
        # `_label_to_idx`, which is built from `schema_mapper_labels.json`
        # and can drift ahead of the binary (it did on prod — PR #50). Used
        # by `_record_per_field_verdicts` to gate the envelope-teacher
        # suppression: a label that isn't in this set CANNOT be predicted
        # by the model, so writing it as `divergence_signal` would
        # guarantee a mismatch on every row.
        #
        # Empty default is the safe fail-mode: gates that consult this set
        # treat "unknown" as "not predictable" and suppress the teacher
        # signal — refusing to grade is better than grading every row
        # wrong. Populated by `_validate_labels()` after session adoption.
        self._model_class_labels: set[str] = set()
        self._verdict_buffer = verdict_buffer
        # D7: bounded deque tracking ONNX-timeout occurrences for the
        # `schema_mapper` tile on `/v1/connections`. We keep timestamps
        # so callers can roll the count over arbitrary windows
        # (defaults to 60s — matching the rest of the connections page).
        # Per-instance, not global, so test isolation works.
        from collections import deque
        self._timeout_events: deque[float] = deque(maxlen=512)
        # Schema-shape drift tracker. A stable fingerprint of the
        # response's (path, value_type) skeleton. The first time an
        # unseen skeleton appears AND it produced overflow (an unmapped
        # field), we log once + bump a Prometheus counter and tick a 60s
        # deque (read via `novel_shapes_60s()`) — the signal that a
        # provider changed its response shape. Per-instance (test
        # isolation); the seen-set is capped so a stream of unique
        # shapes can't grow memory without bound.
        self._seen_shape_fingerprints: set[str] = set()
        self._novel_shape_events: deque[float] = deque(maxlen=512)

        # optional `ModelRegistry` wiring — see `intelligence/reload.py`.
        from gateway.intelligence.reload import ReloadState
        self._reload_state = ReloadState(
            registry=registry, model_name=model_name, db=intelligence_db,
        )

        model_path = onnx_path or str(_ONNX_PATH)
        labels_path = str(_LABELS_PATH)

        # When a registry is wired it owns the session lifecycle — skip the
        # packaged-default load so we don't construct a throwaway session
        # that gets replaced on first inference anyway. The first call to
        # `map_response` triggers `_maybe_reload` which builds from the
        # current production file.
        if Path(model_path).exists() and self._reload_state.registry is None:
            try:
                from onnxruntime import InferenceSession
                self._session = InferenceSession(model_path, providers=["CPUExecutionProvider"])
                self._input_name = self._session.get_inputs()[0].name
                logger.info("SchemaMapper: ONNX model loaded from %s", model_path)
            except Exception as e:
                logger.warning("SchemaMapper: ONNX load failed: %s", e)

        if Path(labels_path).exists():
            with open(labels_path) as f:
                self._labels = json.load(f)
                self._label_to_idx = {l: i for i, l in enumerate(self._labels)}
        else:
            from gateway.schema.canonical import CANONICAL_LABELS
            self._labels = CANONICAL_LABELS
            self._label_to_idx = {l: i for i, l in enumerate(self._labels)}

        # D2: sanity-check labels.json against the ONNX model's class count.
        # The trained model emits indices [0, n_classes) — if labels.json
        # has fewer entries than the model emits, ``self._labels[idx]`` on
        # high indices would raise IndexError on the hot path (currently
        # the code defends with ``idx < len(self._labels)`` and silently
        # mislabels as UNKNOWN, hiding the drift). Failing loudly on init
        # surfaces label/model drift at startup — the operator catches it
        # before the first request rather than after a confusing
        # silent-mislabel storm in production.
        self._validate_labels()

    def _validate_labels(self) -> None:
        """Assert labels.json can resolve every index the ONNX model emits.

        Reads ``output_probability``'s seq-of-map shape from the loaded
        ``InferenceSession`` — the map keys are the class indices the
        model emits. The invariant we enforce is
        ``len(self._labels) >= max_emitted_class_index + 1``:

        * Model emits MORE classes than labels lists → ``LabelsMismatchError``.
          A future request whose predicted index is `≥ len(self._labels)`
          would silently alias to UNKNOWN (or, before the existing bounds
          check, raise IndexError). Fail loudly at boot.
        * Model emits FEWER classes than labels lists → log INFO only.
          Real cause is a candidate trained on a subset of the production
          label space (sklearn only emits indices for classes seen in y).
          Indices are still safe — every emitted index is in [0, n_classes)
          which is a subset of [0, len(labels)). The unused labels are
          slots a future retrain may populate.

        Adding a new label to ``schema_mapper_labels.json`` requires
        retraining the ONNX model — the file is the contract between
        the two and stays in lockstep via the trainer's
        ``_write_sidecars`` (which copies the labels file byte-for-byte).

        Fail-open when the session isn't loaded yet (registry-wired
        path defers session construction to first inference) — the
        check re-runs after ``reload()`` adopts a fresh session.
        """
        if self._session is None:
            return
        try:
            outputs = self._session.get_outputs()  # noqa: F841 - probed for shape only
        except Exception:
            logger.debug("SchemaMapper: cannot read ONNX outputs for label validation")
            return
        # ``output_probability`` is the second output: seq(map(int64, float)).
        # ORT doesn't directly expose the map key set in metadata, so we
        # do a one-shot inference with a single zero feature vector and
        # read the keys from the returned map. Cheap (a few ms) and only
        # runs once per session adoption.
        try:
            import numpy as np
            from gateway.schema.features import FEATURE_DIM
            probe = np.zeros((1, FEATURE_DIM), dtype=np.float32)
            result = self._session.run(None, {self._input_name: probe})
            if len(result) < 2:
                # Old model without probability output — fall back to
                # comparing output[0] dim (a single int label per row,
                # so we can't infer class count). Skip validation.
                return
            probs = result[1]
            if not isinstance(probs, list) or not probs:
                return
            first = probs[0]
            if not isinstance(first, dict):
                return
            class_keys = list(first.keys())
            n_classes = len(class_keys)
            max_class = max(class_keys) if class_keys else -1
        except Exception as exc:
            logger.debug("SchemaMapper: label validation probe failed: %s", exc)
            return

        # Cache the actual label set the loaded ONNX binary can emit. We
        # build it from in-bounds class indices only — if the bounds check
        # below raises, we never reach this assignment and the set stays
        # empty (the safe fail-mode for gate callers). Out-of-bounds
        # indices would otherwise IndexError into `self._labels` here.
        if max_class < len(self._labels):
            self._model_class_labels = {
                self._labels[idx] for idx in class_keys if 0 <= idx < len(self._labels)
            }

        if max_class >= len(self._labels):
            msg = (
                f"SchemaMapper label/model drift: ONNX model emits class "
                f"index {max_class} but labels.json has only "
                f"{len(self._labels)} entries — high indices would "
                f"silently alias to UNKNOWN (or raise IndexError on the "
                f"old bounds check). Retrain or re-export labels.json "
                f"before serving traffic."
            )
            logger.error(msg)
            raise LabelsMismatchError(msg)
        # Identify labels.json entries the loaded ONNX binary CANNOT
        # predict. PR #50/#51 fixed the specific case where `envelope`
        # slipped into labels.json without a matching retrain and
        # collapsed the rolling accuracy metric to 0.2%. The per-field
        # gate (mapper.py:772) is now labels.json-independent — it reads
        # `_model_class_labels`. But any code path that still consults
        # `_label_to_idx` as "what the model can emit" will misbehave.
        # This warning makes that drift self-announce at boot.
        unpredictable = sorted(set(self._labels) - self._model_class_labels)
        if unpredictable:
            logger.warning(
                "SchemaMapper label/binary drift: labels.json lists %d label(s) "
                "the loaded ONNX binary cannot predict: %s. The per-field verdict "
                "gate ignores labels.json (reads ONNX output classes directly), "
                "so accuracy metrics stay honest — but any consumer treating "
                "labels.json as 'what the model can emit' will misbehave. "
                "Retrain the binary against the current labels.json, or remove "
                "the unpredictable labels until a retrain ships.",
                len(unpredictable), unpredictable,
            )
        elif n_classes < len(self._labels):
            # Count-mismatch with no name-mismatch: every model class is
            # listed in labels.json AND every label is predictable. The
            # remaining "extra" labels are an artifact of how the set is
            # represented (no string in labels.json is unaccounted for).
            # Genuinely unreachable in current code paths, but harmless.
            logger.info(
                "SchemaMapper: ONNX model emits %d classes vs %d labels; no "
                "name-level drift detected.", n_classes, len(self._labels),
            )

    def _record_timeout(self) -> None:
        """Tick the per-instance timeout deque. D7 surface for /v1/connections."""
        import time as _t
        self._timeout_events.append(_t.time())

    def timeout_count_60s(self) -> int:
        """Number of ONNX-inference timeouts in the trailing 60s window.

        Read by ``connections/builder.build_schema_mapper_tile`` and surfaced
        as ``schema_mapper_timeouts_60s``. Window-scoped (drops events older
        than 60s on read) so the counter naturally decays — no separate
        prune thread needed.
        """
        import time as _t
        cutoff = _t.time() - 60.0
        # Walk left-to-right (oldest first) and drop expired entries. A
        # deque popleft is O(1); we don't need a fancy structure here
        # because 60s of inference activity at 100 RPS still fits inside
        # ``maxlen=512`` comfortably (the deque self-trims oldest first).
        while self._timeout_events and self._timeout_events[0] < cutoff:
            self._timeout_events.popleft()
        return len(self._timeout_events)

    def _record_novel_shape(self, fields: list[FlatField], overflow_keys) -> None:
        """Log once + tick the drift deque when a never-seen response
        skeleton appears alongside an unmapped (overflow) field.

        Fail-safe: any error here is swallowed — drift tracking must
        never break canonical mapping (same discipline as per-field
        verdict emission).
        """
        try:
            import hashlib
            import time as _t
            skeleton = "\n".join(
                sorted(f"{f.path}:{f.value_type}" for f in fields)
            )
            fp = hashlib.sha256(skeleton.encode()).hexdigest()[:16]
            if fp in self._seen_shape_fingerprints:
                return
            # Cap the seen-set so a stream of unique shapes can't grow
            # it unbounded; once capped we stop deduping (worst case: a
            # few extra log lines, never memory growth).
            if len(self._seen_shape_fingerprints) < 4096:
                self._seen_shape_fingerprints.add(fp)
            overflow = list(overflow_keys or [])
            if not overflow:
                return
            self._novel_shape_events.append(_t.time())
            logger.warning(
                "schema_mapper: novel response shape fp=%s with %d unmapped "
                "path(s) — possible provider schema drift; sample=%s",
                fp, len(overflow), overflow[:5],
            )
            try:
                from gateway.metrics.prometheus import (
                    schema_mapper_novel_shapes_total,
                )
                schema_mapper_novel_shapes_total.inc()
            except Exception:
                pass
        except Exception:
            logger.debug("novel-shape tracking failed", exc_info=True)

    def novel_shapes_60s(self) -> int:
        """Novel-shape-with-overflow events in the trailing 60s window.

        Window-scoped like ``timeout_count_60s`` (drops events older than
        60s on read, so it decays without a prune thread). Public
        accessor for a future ``schema_mapper`` connections tile / health
        surface; the always-on drift signals are the WARNING log and the
        ``schema_mapper_novel_shapes_total`` Prometheus counter.
        """
        import time as _t
        cutoff = _t.time() - 60.0
        while self._novel_shape_events and self._novel_shape_events[0] < cutoff:
            self._novel_shape_events.popleft()
        return len(self._novel_shape_events)

    def map_response(self, raw: dict[str, Any]) -> CanonicalResponse:
        """Map a raw LLM API response to the canonical schema.

        Args:
            raw: The parsed JSON response dict from any LLM provider.

        Returns:
            CanonicalResponse with all recognized fields mapped and
            unrecognized fields preserved in overflow.
        """
        # refresh session from registry if a new version was promoted.
        self._maybe_reload()

        if not isinstance(raw, dict):
            result = CanonicalResponse(mapping=MappingReport(incomplete=True))
        else:
            # 1. Flatten JSON to field list
            fields = flatten_json(raw)
            if not fields:
                result = CanonicalResponse(mapping=MappingReport(incomplete=True))
            else:
                # 2. Classify each field
                classifications = self._classify_fields(fields)
                # 3. Post-process: path-name safety net for UNKNOWN classifications
                classifications = self._apply_path_fallbacks(fields, classifications)
                # 4. Assemble canonical response
                result = self._assemble(fields, classifications, raw)
                # 5. Schema-drift signal (fail-safe; never breaks mapping)
                self._record_novel_shape(fields, result.overflow.keys())

        # Per-field verdicts are recorded inside `_classify_onnx` —
        # they carry the 139-d feature vector and a heuristic teacher
        # signal that the distillation trainer consumes. The
        # previous per-response verdict (one row, empty
        # `input_features_json`, coarse "complete"/"incomplete"
        # prediction) was retired because it carried no usable
        # training signal: the trainer needs per-field features to
        # match production's `_classify_onnx` shape.

        return result

    def _apply_path_fallbacks(self, fields: list[FlatField],
                               classifications: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """Safety net: reclassify UNKNOWN fields when path name is obvious.

        Shares the rule table with `classify_overflow_path` (module-level
        helper reused by the Phase 25 SchemaMapper harvester) so both code
        paths stay in lockstep.

        D3: also rewrites UNKNOWN→``envelope`` for provider response-shape
        keys (``object``, ``created``, ``role``, ``index``, …). These have
        no canonical class but live at predictable positions in OpenAI /
        Anthropic / Ollama responses. The ``envelope`` tag is excluded
        from both ``unmapped`` and ``overflow_keys`` so operators only
        see actionably-unmapped fields. ``ENVELOPE_PATH_DISQUALIFIERS``
        prevents the tag from swallowing user-data scopes like
        ``arguments.role`` inside a tool call.
        """
        result = list(classifications)
        for i, (f, (label, conf)) in enumerate(zip(fields, classifications)):
            # A direct ONNX/heuristic ``envelope`` prediction must still
            # honour ENVELOPE_PATH_DISQUALIFIERS. Without this, a deep
            # ``arguments.role`` (user data inside a tool call) that ONNX
            # confidently calls ``envelope`` gets silently swallowed,
            # because the disqualifier gate below only runs for UNKNOWN.
            if label == ENVELOPE_LABEL and not _is_envelope_field(f.key, f.path):
                result[i] = ("UNKNOWN", conf)
                label = "UNKNOWN"
            if label != "UNKNOWN":
                continue
            # D3: envelope tag is the top priority once a field is UNKNOWN —
            # it applies regardless of value_type so e.g. ``logprobs: null``
            # or ``logprobs: {…}`` both end up tagged correctly.
            if _is_envelope_field(f.key, f.path):
                result[i] = (ENVELOPE_LABEL, 1.0)
                continue
            # Skip structural types for the canonical-fallback table —
            # they're correctly UNKNOWN. (Envelope tagging above already
            # handled the structural envelope keys like ``logprobs``.)
            if f.value_type in ("object", "array"):
                continue
            key_lower = f.key.lower()
            path_lower = f.path.lower()
            for path_tokens, leaf_match, target_label in _PATH_FALLBACK_RULES:
                # Skip envelope-label rules during canonical fallback —
                # they're handled by the explicit depth-aware check above
                # and we don't want a deep ``role`` field accidentally
                # picked up here.
                if target_label == ENVELOPE_LABEL:
                    continue
                if key_lower == leaf_match.lower() or f.key == leaf_match:
                    if all(tok in path_lower for tok in path_tokens):
                        result[i] = (target_label, 0.75)  # Lower confidence than ONNX
                        break
        return result

    def _classify_fields(self, fields: list[FlatField]) -> list[tuple[str, float]]:
        """Classify each field via deterministic provider map first, ONNX/heuristic
        fallback for everything else.

        Architecture: ``_PROVIDER_PATH_MAP`` holds exact-path rules for the
        stable provider response shapes (OpenAI, Anthropic, Ollama). When a
        field's path is in the map it is assigned with confidence=1.0 and the
        ONNX classifier is skipped for that field. This is the fix for the
        nested-field misclassification bug — ONNX confidently misclassifies
        ``usage.prompt_tokens_details.cached_tokens`` as ``prompt_tokens``
        (p=1.0) because the path contains the substring "prompt", which then
        collides with the real ``usage.prompt_tokens`` slot and corrupts the
        canonical record. Deterministic-first eliminates the corruption for
        known providers; ONNX still serves as the fallback for novel paths.

        Returns list of (label, confidence) tuples — one per input field, in
        input order.
        """
        # Phase 1: deterministic provider-map lookup
        deterministic: dict[int, tuple[str, float]] = {}
        unresolved_indices: list[int] = []
        unresolved_fields: list[FlatField] = []
        for i, f in enumerate(fields):
            label = _PROVIDER_PATH_MAP.get(f.path)
            if label is not None:
                deterministic[i] = (label, 1.0)
            else:
                unresolved_indices.append(i)
                unresolved_fields.append(f)

        # Phase 2: ONNX (or heuristic fallback) on fields the provider map
        # didn't claim. If the provider map covered everything, skip the model
        # call entirely.
        if unresolved_fields:
            if self._session:
                from gateway.intelligence._inference_timeout import InferenceTimeout
                try:
                    unresolved_results = self._classify_onnx(unresolved_fields)
                except InferenceTimeout as e:
                    self._record_timeout()
                    logger.warning("schema-mapper ONNX timed out, using heuristic: %s", e)
                    unresolved_results = self._classify_heuristic(unresolved_fields)
            else:
                unresolved_results = self._classify_heuristic(unresolved_fields)
        else:
            unresolved_results = []

        # Merge: re-stitch deterministic + ONNX results into original field order.
        merged: list[tuple[str, float]] = [("UNKNOWN", 0.0)] * len(fields)
        for i, result in deterministic.items():
            merged[i] = result
        for slot_i, result in zip(unresolved_indices, unresolved_results):
            merged[slot_i] = result
        return merged

    def _classify_onnx(self, fields: list[FlatField]) -> list[tuple[str, float]]:
        """Batch ONNX inference on all fields.

        Side-effect: when a `verdict_buffer` is wired, every field
        emits a per-field `ModelVerdict` whose `input_features_json`
        carries the full 139-d float vector that fed the ONNX session.
        That row is what the schema_mapper trainer (and its sanity
        adapter) consume — the per-response row the previous
        revision recorded had `input_features_json="{}"` and was
        useless for training. Verdicts are non-fatal: a buffer error
        never breaks inference (caller wraps the loop defensively).
        """
        from gateway.intelligence._inference_timeout import run_with_timeout

        per_field_features = [extract_features(f) for f in fields]
        feature_matrix = np.array(per_field_features, dtype=np.float32)
        feature_matrix = np.nan_to_num(feature_matrix, nan=0.0, posinf=1.0, neginf=-1.0)

        outputs = run_with_timeout(
            self._session.run, None, {self._input_name: feature_matrix},
            model="schema_mapper",
        )
        predicted_indices = outputs[0]

        # Get probabilities if available (output[1] for sklearn models)
        if len(outputs) > 1:
            probs = outputs[1]  # list of dicts or 2d array
            results = []
            for i, idx in enumerate(predicted_indices):
                label = self._labels[idx] if idx < len(self._labels) else "UNKNOWN"
                if isinstance(probs[i], dict):
                    confidence = float(max(probs[i].values())) if probs[i] else 0.0
                else:
                    confidence = float(probs[i][idx]) if hasattr(probs[i], '__getitem__') else 0.5
                results.append((label, confidence))
        else:
            results = [
                (self._labels[idx] if idx < len(self._labels) else "UNKNOWN", 0.8)
                for idx in predicted_indices
            ]

        self._record_per_field_verdicts(fields, per_field_features, results)
        return results

    def _record_per_field_verdicts(
        self,
        fields: list["FlatField"],
        feature_vectors: list[Any],
        classifications: list[tuple[str, float]],
    ) -> None:
        """Emit one verdict per field with the actual 139-d features.

        Volume: a typical response flattens to ~10-50 fields. A 100-field
        burst maps to 100 verdict rows for that request. The bounded
        `VerdictBuffer` (default `max_size=10_000`) drops oldest on
        overflow — that's the right knob; we don't sample at the call
        site because the trainer benefits from every field, not just
        the high-confidence ones.

        Teacher signal (`divergence_signal`): we use the heuristic
        classifier as a rule-based "ground truth" reference. When the
        heuristic and ONNX agree on a non-UNKNOWN label, that's a
        positive teaching example. When they disagree (or ONNX says
        UNKNOWN and heuristic has an opinion), the heuristic label is
        the teacher. Self-distillation pitfall: we DO NOT use the ONNX
        prediction itself as the teacher — that would teach the model
        whatever it already does. Rows where the heuristic also says
        UNKNOWN get no teacher and are skipped from training (the
        dataset builder filters on `divergence_signal IS NOT NULL`).

        D5 + D6 envelope teaching: the heuristic now emits
        ``envelope`` for provider response-shape keys. We pass that
        label through as the teacher signal ONLY when the current
        production model could plausibly emit it (i.e., it's already
        in ``self._labels``). Otherwise we suppress the signal so
        ``db.compute_accuracy`` doesn't penalize ONNX's "UNKNOWN"
        on a label it was never trained to recognize. Once a future
        retrain includes ``envelope`` in ``labels.json``, this gate
        flips automatically and the teaching signal becomes active.
        """
        if self._verdict_buffer is None:
            return
        try:
            from gateway.schema.canonical import ENVELOPE_LABEL
            from gateway.util.request_context import request_id_var
            from gateway.intelligence.types import ModelVerdict
            rid = request_id_var.get() or None
            version = self._reload_state.current_version
            # Source of truth is the ONNX binary's actual output class
            # set — not labels.json. PR #50 fixed the symptom by removing
            # `envelope` from labels.json; this gate now makes the metric
            # robust to a future labels.json that's ahead of (or behind)
            # the deployed binary. Empty `_model_class_labels` (session
            # not yet probed, or validation failed) is the safe default:
            # gate fires → teacher signal suppressed → no false mismatches.
            envelope_trainable = ENVELOPE_LABEL in self._model_class_labels
            for field, feat_vec, (label, confidence) in zip(
                fields, feature_vectors, classifications,
            ):
                # Heuristic teacher signal — independent of ONNX.
                teacher, _ = self._heuristic_classify_one(field)
                if teacher == "UNKNOWN":
                    divergence_signal = None
                elif teacher == ENVELOPE_LABEL and not envelope_trainable:
                    # Don't penalize the current model for an envelope label
                    # it can't predict; see docstring above.
                    divergence_signal = None
                else:
                    divergence_signal = teacher
                # `input_text` is only used to derive `input_hash` for
                # dedupe. Per-field uniqueness comes from path + value;
                # without it every field on a response would collide.
                try:
                    value_repr = json.dumps(field.value, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    value_repr = repr(field.value)
                input_text = f"{field.path}|{field.value_type}|{value_repr}"
                # The 139-d feature vector serialized as a JSON list. The
                # trainer + sanity adapter detect this shape and skip
                # re-featurization. Coerce to a plain list of floats so
                # NumPy types don't trip json.dumps.
                features_payload = {
                    "feature_vector": [float(v) for v in feat_vec],
                    "field_path": field.path,
                }
                verdict = ModelVerdict(
                    model_name="schema_mapper",
                    input_hash=__import__("hashlib").sha256(
                        input_text.encode()
                    ).hexdigest(),
                    input_features_json=json.dumps(features_payload),
                    prediction=label,
                    confidence=float(confidence),
                    request_id=rid,
                    divergence_signal=divergence_signal,
                    divergence_source=(
                        "schema_mapper_heuristic" if divergence_signal else None
                    ),
                    version=version,
                )
                self._verdict_buffer.record(verdict)
        except Exception:
            logger.debug("per-field verdict recording failed", exc_info=True)

    def _classify_heuristic(self, fields: list[FlatField]) -> list[tuple[str, float]]:
        """Fallback heuristic classification when ONNX is unavailable."""
        results = []
        for f in fields:
            label, conf = self._heuristic_classify_one(f)
            results.append((label, conf))
        return results

    def _heuristic_classify_one(self, f: FlatField) -> tuple[str, float]:
        """Rule-based classification for a single field.

        D6: recognizes envelope keys (``object``, ``created``, ``role``,
        ``index``, ``logprobs``, …) so the heuristic fallback is at least
        as honest as the ONNX path. Without this, a request that times
        out on the ONNX side (and degrades to the heuristic classifier)
        would still inflate ``schema_mapper_overflow_keys`` with provider
        boilerplate — defeating D3's purpose. Uses the same
        ``_is_envelope_field`` gate as the post-classification rewriter
        so the two paths can't drift.
        """
        # D6: envelope key short-circuit. Comes BEFORE content/token rules
        # because some envelope keys (``role``, ``type``, ``object``) are
        # short strings that would otherwise look like enums.
        if _is_envelope_field(f.key, f.path):
            return ENVELOPE_LABEL, 1.0

        key_lower = f.key.lower()
        path_lower = f.path.lower()

        # Content detection: long natural-language string
        if f.value_type == "string" and isinstance(f.value, str):
            if len(f.value) > 50 and f.value.count(" ") >= 5:
                if any(k in key_lower for k in ("content", "text", "generated", "output")):
                    if "think" in key_lower or "reason" in key_lower or "plan" in key_lower:
                        return "thinking_content", 0.9
                    return "content", 0.9
            if key_lower in ("content", "text", "generated_text", "output_text"):
                return "content", 0.8
            if key_lower in ("reasoning", "reasoning_content", "thinking", "tool_plan"):
                return "thinking_content", 0.85
            if key_lower in ("finish_reason", "stop_reason", "done_reason",
                             "completion_reason", "status"):
                return "finish_reason", 0.85
            if key_lower in ("id",) and "message" not in path_lower:
                return "response_id", 0.7
            if key_lower in ("model", "model_version"):
                return "model", 0.8
            if key_lower in ("system_fingerprint", "model_hash", "version"):
                return "model_hash", 0.7

        # Token count detection: integers in usage-like context
        if f.value_type in ("int", "float") and not isinstance(f.value, bool):
            if any(k in key_lower for k in ("prompt_token", "input_token", "prompt_eval")):
                return "prompt_tokens", 0.9
            if any(k in key_lower for k in ("completion_token", "output_token", "eval_count",
                                             "generated_token")):
                return "completion_tokens", 0.9
            if "total_token" in key_lower:
                return "total_tokens", 0.9
            if "cache" in key_lower and "token" in key_lower:
                if "creation" in key_lower:
                    return "cache_creation_tokens", 0.85
                return "cached_tokens", 0.85
            if "reasoning_token" in key_lower:
                return "reasoning_tokens", 0.85
            if any(k in key_lower for k in ("duration", "time", "latency", "elapsed")):
                return "timing_value", 0.75

        # Tool call detection
        if key_lower in ("name",) and "function" in path_lower:
            return "tool_call_name", 0.8
        if key_lower in ("arguments",) and "function" in path_lower:
            return "tool_call_arguments", 0.8

        return "UNKNOWN", 0.5

    def _assemble(self, fields: list[FlatField], classifications: list[tuple[str, float]],
                  raw: dict) -> CanonicalResponse:
        """Assemble a CanonicalResponse from classified fields.

        D1: ``confidence`` is coverage-weighted —
        ``sum(mapped_confidences) / classified_count`` where
        ``classified_count`` is the number of fields that are not
        ``envelope``-tagged. Envelope fields are removed from the
        denominator because they're structural boilerplate that
        legitimately has no canonical class. ``confidence_on_mapped``
        keeps the legacy "average over mapped fields" semantic.

        D3: ``envelope``-tagged fields are excluded from
        ``unmapped_fields`` (so they don't inflate the operator-visible
        unmapped count) and from ``cr.overflow`` (so ``overflow_keys``
        only carries actionable-unknown fields).

        D4: null-valued UNKNOWN fields are kept out of overflow — they
        carry no information and only inflate the key list.
        """
        from gateway.schema.canonical import ENVELOPE_LABEL

        cr = CanonicalResponse()
        mapped = []
        unmapped = []
        envelope_count = 0

        # Group classifications.
        #
        # D3 accounting rules for ``unmapped_fields`` / ``mapped_fields``:
        #
        # * ``ENVELOPE_LABEL`` → excluded from both counts. Structural
        #   boilerplate (no canonical class to grade against).
        # * Structural-type UNKNOWN (object/array values whose leaf is
        #   not an envelope key) → excluded too. ``choices`` and
        #   ``choices.0.message`` are containers; they're UNKNOWN by
        #   nature because they have no value to classify, only nested
        #   children. Counting them as "unmapped" gives operators a
        #   misleadingly inflated number — the children are the real
        #   classification targets.
        # * Any other UNKNOWN → counted as unmapped (and put in overflow
        #   if non-null per D4).
        field_map: dict[str, list[tuple[FlatField, float]]] = {}
        for f, (label, conf) in zip(fields, classifications):
            if label == ENVELOPE_LABEL:
                envelope_count += 1
                continue
            if label == "UNKNOWN":
                # Structural containers don't count toward operator-visible
                # "unmapped" — they're traversal aids, not classifiable leaves.
                if f.value_type in ("object", "array"):
                    continue
                unmapped.append(f.path)
                continue
            # Structural containers (object/array) that the classifier labeled
            # with a canonical class are still traversal aids — their *leaves*
            # carry the actual value. Adding them to ``field_map`` lets the
            # shortest-path tiebreaker in ``_best`` accidentally pick a parent
            # container over a leaf (e.g. Gemini's ``candidates.0.content``
            # object beating ``candidates.0.content.parts.0.text``). Skip them
            # for canonical-slot assignment but still count them as "mapped"
            # for operator metrics so the coverage number stays honest.
            mapped.append(f.path)
            if f.value_type in ("object", "array"):
                continue
            if label not in field_map:
                field_map[label] = []
            field_map[label].append((f, conf))

        # ── Assign singleton fields ────────────────────────────────────
        #
        # Selection: prefer the field with the SHORTEST path, then the
        # highest confidence. Shortest-path-wins resolves collisions where
        # the same canonical label was assigned to a top-level field AND a
        # nested sub-field (e.g. OpenAI's real ``usage.completion_tokens=4``
        # vs the nested ``usage.completion_tokens_details.audio_tokens=11``
        # that ONNX confidently mislabeled as ``completion_tokens``).
        # Top-level provider fields are authoritative; nested fields under
        # ``*_details`` sub-objects should never overwrite them. Highest-
        # confidence remains the secondary criterion for the common case
        # where two fields legitimately share a canonical class (multiple
        # tool_call entries, multi-choice responses).
        def _best(label: str) -> tuple[FlatField, float] | None:
            entries = field_map.get(label, [])
            if not entries:
                return None
            return min(entries, key=lambda fc: (fc[0].path.count("."), -fc[1]))

        best = _best("content")
        if best:
            cr.content = str(best[0].value) if best[0].value is not None else ""

        best = _best("thinking_content")
        if best:
            cr.thinking_content = str(best[0].value) if best[0].value is not None else None

        best = _best("finish_reason")
        if best:
            cr.finish_reason = self._normalize_finish_reason(str(best[0].value))

        best = _best("response_id")
        if best:
            cr.response_id = str(best[0].value)

        best = _best("model")
        if best:
            cr.model = str(best[0].value)

        best = _best("model_hash")
        if best:
            cr.model_hash = str(best[0].value)

        # ── Assign usage fields ──────────────────────────────────────
        for label in ("prompt_tokens", "completion_tokens", "total_tokens",
                      "reasoning_tokens", "cached_tokens", "cache_creation_tokens", "cost_usd"):
            best = _best(label)
            if best and best[0].value is not None:
                try:
                    val = float(best[0].value) if label == "cost_usd" else int(float(best[0].value))
                    setattr(cr.usage, label, val)
                except (ValueError, TypeError):
                    pass
        cr.usage.compute_total()

        # ── Tool calls ───────────────────────────────────────────────
        tool_names = field_map.get("tool_call_name", [])
        tool_args = field_map.get("tool_call_arguments", [])
        tool_ids = field_map.get("tool_call_id", [])
        tool_types = field_map.get("tool_call_type", [])
        n_tools = max(len(tool_names), len(tool_args))
        for i in range(n_tools):
            tc = CanonicalToolCall()
            if i < len(tool_names):
                tc.name = str(tool_names[i][0].value)
            if i < len(tool_args):
                args = tool_args[i][0].value
                tc.arguments = args if isinstance(args, (dict, str)) else str(args)
            if i < len(tool_ids):
                tc.id = str(tool_ids[i][0].value)
            if i < len(tool_types):
                tc.type = str(tool_types[i][0].value)
            cr.tool_calls.append(tc)

        # ── Citations ────────────────────────────────────────────────
        for f, conf in field_map.get("citation_url", []):
            if isinstance(f.value, list):
                for url in f.value:
                    cr.citations.append(CanonicalCitation(url=str(url)))
            elif isinstance(f.value, str):
                cr.citations.append(CanonicalCitation(url=f.value))

        # ── Timing ───────────────────────────────────────────────────
        timing_fields = field_map.get("timing_value", [])
        if timing_fields:
            cr.timing = CanonicalTiming()
            for f, conf in timing_fields:
                key = f.key.lower()
                try:
                    val = float(f.value)
                    # Convert nanoseconds to milliseconds (Ollama uses ns)
                    if val > 1_000_000:
                        val = val / 1_000_000
                    if "total" in key or "overall" in key:
                        cr.timing.total_ms = val
                    elif "prompt" in key or "eval" in key and "prompt" in f.path.lower():
                        cr.timing.prompt_ms = val
                    elif "queue" in key:
                        cr.timing.queue_ms = val
                    elif cr.timing.completion_ms is None:
                        cr.timing.completion_ms = val
                except (ValueError, TypeError):
                    pass

        # ── Safety ───────────────────────────────────────────────────
        safety_fields = field_map.get("safety_category", [])
        if safety_fields:
            cr.safety = CanonicalSafety()
            for f, conf in safety_fields:
                if isinstance(f.value, list):
                    for item in f.value:
                        if isinstance(item, dict):
                            cat = item.get("category", "")
                            prob = item.get("probability", "")
                            cr.safety.categories[cat] = prob
                            if prob in ("HIGH", "VERY_HIGH"):
                                cr.safety.blocked = True

        # ── Overflow (self-healing) ──────────────────────────────────
        # D3: envelope-tagged fields skipped — they're structural, not
        # actionable. D4: null leaves skipped too — they carry no info
        # and only inflate the key list.
        for f, (label, _) in zip(fields, classifications):
            if label != "UNKNOWN":
                continue
            if f.value_type in ("object", "array", "null"):
                continue
            cr.overflow[f.path] = f.value

        # ── Mapping metadata ─────────────────────────────────────────
        # D1: coverage-weighted confidence. Denominator excludes envelope
        # fields (no canonical class to grade), so it answers "of the
        # fields that COULD have a canonical class, how many did we
        # correctly classify, weighted by confidence". The legacy
        # average-over-mapped semantic remains available as
        # ``confidence_on_mapped`` for any downstream consumer that
        # specifically wants "how sure were we about the calls we made".
        mapped_confidences = [
            conf for _, (lbl, conf) in zip(fields, classifications)
            if lbl != "UNKNOWN" and lbl != ENVELOPE_LABEL
        ]
        classifiable_total = len(mapped_confidences) + len(unmapped)
        coverage_confidence = (
            sum(mapped_confidences) / classifiable_total
            if classifiable_total else 0.0
        )
        on_mapped_confidence = (
            sum(mapped_confidences) / len(mapped_confidences)
            if mapped_confidences else 0.0
        )
        cr.mapping = MappingReport(
            confidence=coverage_confidence,
            confidence_on_mapped=on_mapped_confidence,
            incomplete=not cr.content and not cr.thinking_content,
            mapped_fields=mapped,
            unmapped_fields=unmapped,
        )

        return cr

    def reload(self) -> None:
        """Rebuild the `InferenceSession` from the registry's production path.

        Also refreshes `_input_name` so ORT call sites keep working after a
        retrained model changes the input tensor name. Fail-safe.
        """
        from gateway.intelligence.reload import maybe_reload

        def _build(path: str):
            from onnxruntime import InferenceSession
            return InferenceSession(path, providers=["CPUExecutionProvider"])

        def _adopt(session) -> None:
            self._session = session
            try:
                self._input_name = session.get_inputs()[0].name
            except Exception:
                logger.debug("SchemaMapper.reload: could not refresh input_name", exc_info=True)
            # Refresh the cached ONNX class-label set whenever a new binary
            # is hot-swapped in. Without this, a promotion that adds/removes
            # output classes (e.g. envelope) would leave the per-field
            # verdict gate stuck on the OLD class set, re-introducing
            # exactly the skew PR #50 fixed.
            self._model_class_labels = set()
            self._validate_labels()

        maybe_reload(self._reload_state, _build, _adopt, label="schema_mapper")

    def _maybe_reload(self) -> None:
        """Hot-path hook — poll generation, rebuild session if it moved."""
        if self._reload_state.registry is None:
            return
        self.reload()

    @staticmethod
    def _normalize_finish_reason(raw: str) -> str:
        """Normalize finish_reason across providers."""
        raw_lower = raw.lower().strip()
        mapping = {
            "stop": "stop", "end_turn": "stop", "eos_token": "stop",
            "complete": "stop", "finished": "stop", "succeeded": "stop",
            "length": "length", "max_tokens": "length",
            "tool_calls": "tool_calls", "tool_use": "tool_calls",
            "content_filter": "content_filter", "safety": "content_filter",
            "error": "error", "failed": "error",
        }
        return mapping.get(raw_lower, raw_lower)
