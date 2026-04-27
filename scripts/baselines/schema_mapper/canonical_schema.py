"""Canonical 19-label schema for LLM provider response field classification.

The label space MUST stay aligned with src/gateway/schema/schema_mapper_labels.json
(the runtime contract) and src/gateway/schema/canonical.py:CanonicalResponse.

Adding a new label requires: (1) bumping the manifest schema_version,
(2) coordinating retraining + redeploying, (3) updating downstream
consumers in pipeline/orchestrator.py + content/.
"""
from __future__ import annotations

CANONICAL_LABELS: tuple[str, ...] = (
    "UNKNOWN",
    "cache_creation_tokens",
    "cached_tokens",
    "citation_url",
    "completion_tokens",
    "content",
    "finish_reason",
    "model",
    "model_hash",
    "prompt_tokens",
    "response_id",
    "safety_category",
    "thinking_content",
    "timing_value",
    "tool_call_arguments",
    "tool_call_id",
    "tool_call_name",
    "tool_call_type",
    "total_tokens",
)
LABEL_TO_ID: dict[str, int] = {label: i for i, label in enumerate(CANONICAL_LABELS)}
ID_TO_LABEL: dict[int, str] = {i: label for label, i in LABEL_TO_ID.items()}

# Each value is a one-line semantic definition used by:
#   (a) the teacher-LLM labelling prompt
#   (b) the human-gate review tool
#   (c) the model card audit trail
LABEL_DESCRIPTIONS: dict[str, str] = {
    "UNKNOWN":               "Field that does not map to any canonical concept",
    "content":               "Primary user-visible text from the assistant",
    "thinking_content":      "Reasoning / thinking-block content separate from final answer",
    "finish_reason":         "Why generation stopped (stop, length, tool_calls, ...)",
    "model":                 "Model identifier string (e.g. 'gpt-4o', 'claude-opus-4-7')",
    "model_hash":            "Model fingerprint or version-pin hash",
    "response_id":           "Unique provider-side response/request identifier",
    "prompt_tokens":         "Input/prompt token count",
    "completion_tokens":     "Output/completion token count",
    "total_tokens":          "Sum of prompt + completion tokens",
    "cached_tokens":         "Tokens served from prompt cache (read)",
    "cache_creation_tokens": "Tokens written to prompt cache",
    "citation_url":          "URL of a cited source (web search / RAG)",
    "safety_category":       "Provider-side safety classification label",
    "timing_value":          "Latency / duration value (ms or s)",
    "tool_call_arguments":   "JSON-encoded tool-call argument string",
    "tool_call_id":          "Per-call identifier for tool invocations",
    "tool_call_name":        "Name of the tool being invoked",
    "tool_call_type":        "Tool-call type (function / web_search / code_interpreter)",
}

# CRF-level transition rules: (label_a, label_b) -> forbidden if both
# appear within the same JSON sub-tree depth-1 sibling group.
# The CRF learns transition weights from data; these are extreme priors
# (impossible co-occurrence) used as -inf masking during inference.
CRF_FORBIDDEN_TRANSITIONS: tuple[tuple[str, str], ...] = (
    # No two siblings can both be `content` in a single response dict.
    ("content", "content"),
)

# Mutually-exclusive groups: at most one member of each group can be
# the prediction for any single field's siblings AT THE SAME PATH DEPTH.
# Used by the CRF as a hard constraint when computing per-field marginals.
EXCLUSIVE_GROUPS: dict[str, tuple[str, ...]] = {
    "primary_content": ("content", "thinking_content"),
    "token_count":     ("prompt_tokens", "completion_tokens", "total_tokens",
                        "cached_tokens", "cache_creation_tokens"),
    "tool_call":       ("tool_call_id", "tool_call_name",
                        "tool_call_arguments", "tool_call_type"),
    "id":              ("response_id", "tool_call_id"),
    "model_meta":      ("model", "model_hash"),
}

# Soft constraints: pairs that should USUALLY co-occur in the same dict.
# Used by the CRF as positive prior transition weights.
COOCCUR_BIAS: tuple[tuple[str, str, float], ...] = (
    ("prompt_tokens",     "completion_tokens",   1.0),
    ("prompt_tokens",     "total_tokens",        0.7),
    ("completion_tokens", "total_tokens",        0.7),
    ("tool_call_id",      "tool_call_name",      0.9),
    ("tool_call_name",    "tool_call_arguments", 0.9),
    ("content",           "finish_reason",       0.6),
)
