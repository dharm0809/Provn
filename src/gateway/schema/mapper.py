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


# Path-name patterns that strongly indicate a canonical field.
# Used as safety net when ONNX says UNKNOWN but the path is obvious,
# and reused by the Phase 25 SchemaMapper harvester to label overflow
# keys for training signal capture. Format:
#   (path must contain ALL of these tokens, leaf key must match exactly, → label)
# Module-level so the harvester can import without touching class internals.
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
)


def classify_overflow_path(path: str) -> str | None:
    """Return the canonical label a path would receive via fallback rules, or None.

    Splits the leaf (final dotted segment, stripping trailing indices like
    `[0]`) and runs the same `leaf + path-token` match used by
    `SchemaMapper._apply_path_fallbacks`. The Phase 25 harvester uses this
    against the overflow-keys list captured at audit time to produce
    training signal for the distillation pipeline.
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
        if all(tok in path_lower for tok in path_tokens):
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
    ) -> None:
        self._session = None
        self._input_name = ""
        self._labels: list[str] = []
        self._label_to_idx: dict[str, int] = {}
        self._verdict_buffer = verdict_buffer

        # optional `ModelRegistry` wiring — see `intelligence/reload.py`.
        from gateway.intelligence.reload import ReloadState
        self._reload_state = ReloadState(registry=registry, model_name=model_name)

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

        # record verdict for self-learning (observational only).
        # Never allowed to break inference — wrap the whole stanza defensively.
        if self._verdict_buffer is not None:
            try:
                from gateway.util.request_context import request_id_var
                from gateway.intelligence.types import ModelVerdict
                # Serialize raw dict as input_text for input_hash. Use sort_keys
                # so logically-equal dicts produce a stable hash. Fallback to
                # repr() if the dict contains non-JSON-serializable values.
                try:
                    input_text = json.dumps(raw, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    input_text = repr(raw)
                prediction = "incomplete" if result.mapping.incomplete else "complete"
                rid = request_id_var.get() or None
                self._verdict_buffer.record(
                    ModelVerdict.from_inference(
                        model_name="schema_mapper",
                        input_text=input_text,
                        prediction=prediction,
                        confidence=float(result.mapping.confidence or 0.0),
                        request_id=rid,
                    )
                )
            except Exception:
                logger.debug("verdict recording failed", exc_info=True)

        return result

    def _apply_path_fallbacks(self, fields: list[FlatField],
                               classifications: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """Safety net: reclassify UNKNOWN fields when path name is obvious.

        Shares the rule table with `classify_overflow_path` (module-level
        helper reused by the Phase 25 SchemaMapper harvester) so both code
        paths stay in lockstep.
        """
        result = list(classifications)
        for i, (f, (label, conf)) in enumerate(zip(fields, classifications)):
            if label != "UNKNOWN":
                continue
            # Skip structural types — they're correctly UNKNOWN
            if f.value_type in ("object", "array"):
                continue
            key_lower = f.key.lower()
            path_lower = f.path.lower()
            for path_tokens, leaf_match, target_label in _PATH_FALLBACK_RULES:
                if key_lower == leaf_match.lower() or f.key == leaf_match:
                    if all(tok in path_lower for tok in path_tokens):
                        result[i] = (target_label, 0.75)  # Lower confidence than ONNX
                        break
        return result

    def _classify_fields(self, fields: list[FlatField]) -> list[tuple[str, float]]:
        """Classify each field using ONNX model or heuristic fallback.

        Returns list of (label, confidence) tuples.
        """
        if self._session:
            from gateway.intelligence._inference_timeout import InferenceTimeout
            try:
                return self._classify_onnx(fields)
            except InferenceTimeout as e:
                logger.warning("schema-mapper ONNX timed out, using heuristic: %s", e)
                return self._classify_heuristic(fields)
        return self._classify_heuristic(fields)

    def _classify_onnx(self, fields: list[FlatField]) -> list[tuple[str, float]]:
        """Batch ONNX inference on all fields."""
        from gateway.intelligence._inference_timeout import run_with_timeout

        feature_matrix = np.array(
            [extract_features(f) for f in fields], dtype=np.float32
        )
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
            return results
        else:
            return [(self._labels[idx] if idx < len(self._labels) else "UNKNOWN", 0.8)
                    for idx in predicted_indices]

    def _classify_heuristic(self, fields: list[FlatField]) -> list[tuple[str, float]]:
        """Fallback heuristic classification when ONNX is unavailable."""
        results = []
        for f in fields:
            label, conf = self._heuristic_classify_one(f)
            results.append((label, conf))
        return results

    def _heuristic_classify_one(self, f: FlatField) -> tuple[str, float]:
        """Rule-based classification for a single field."""
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
        """Assemble a CanonicalResponse from classified fields."""
        cr = CanonicalResponse()
        mapped = []
        unmapped = []

        # Group classifications
        field_map: dict[str, list[tuple[FlatField, float]]] = {}
        for f, (label, conf) in zip(fields, classifications):
            if label == "UNKNOWN":
                unmapped.append(f.path)
                continue
            mapped.append(f.path)
            if label not in field_map:
                field_map[label] = []
            field_map[label].append((f, conf))

        # ── Assign singleton fields (pick highest confidence) ────────
        def _best(label: str) -> tuple[FlatField, float] | None:
            entries = field_map.get(label, [])
            if not entries:
                return None
            return max(entries, key=lambda x: x[1])

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
        for f, (label, _) in zip(fields, classifications):
            if label == "UNKNOWN" and f.value_type not in ("object", "array"):
                cr.overflow[f.path] = f.value

        # ── Mapping metadata ─────────────────────────────────────────
        confidences = [conf for _, (_, conf) in zip(fields, classifications) if _ != "UNKNOWN"]
        cr.mapping = MappingReport(
            confidence=sum(confidences) / len(confidences) if confidences else 0.0,
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
