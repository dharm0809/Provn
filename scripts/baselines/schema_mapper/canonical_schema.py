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
    "response_timestamp",
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
    "response_id":           "Primary correlatable response identifier — the one an external caller correlates traffic by. Provider-internal per-block / per-generation / per-tool-call sub-identifiers (e.g. Groq's `x_groq.id`, Bedrock-Cohere `generations[].id`, Anthropic thinking-block `signature`) are NOT response_id; they map to UNKNOWN. If a sub-id ever needs canonical status, add a distinct label rather than overloading response_id.",
    "response_timestamp":    "Wall-clock time the provider stamped the response (unix int OR ISO-8601 string). Distinct from timing_value (a duration/latency) — used for audit correlation, clock-drift detection, and replay defense.",
    "prompt_tokens":         "Input/prompt token count",
    "completion_tokens":     "Output/completion token count",
    "total_tokens":          "Sum of prompt + completion tokens",
    "cached_tokens":         "Tokens served from prompt cache (read)",
    "cache_creation_tokens": "Tokens written to prompt cache",
    "citation_url":          "URL of a cited source (web search / RAG)",
    "safety_category":       "Provider-side safety classification — category name (e.g. 'HARM_CATEGORY_HATE_SPEECH'), probability bucket (e.g. 'NEGLIGIBLE'/'LOW'/'MEDIUM'/'HIGH'), severity bucket, or any leaf within a safety-rating block. Per-leaf labelling: every facet of a single safety rating carries this label; the model learns the facet from path context, downstream consumers re-aggregate via the path.",
    "timing_value":          "Latency / duration value (ms or s)",
    "tool_call_arguments":   "Tool-call argument data — either the JSON-encoded string (OpenAI/Anthropic) OR a leaf field within a structured argument dict (Ollama). Per-leaf labelling: every leaf under arguments.* carries this label regardless of nesting depth.",
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

# Per-parent-object label-uniqueness groups.
#
# Semantics: within a single direct parent object, AT MOST ONE field
# should carry each label in the group. NOT per-depth, NOT per-dict-tree.
# The "parent object path" is the full path with the trailing leaf
# segment AND any trailing [N] index stripped (see paths.parent_object_path).
#
# Example (`tool_call` group):
#   Within one tool-call dict (parent = choices[0].message.tool_calls[0]),
#   each of {tool_call_id, tool_call_name, tool_call_arguments,
#   tool_call_type} appears AT MOST ONCE — there is one `id` leaf, one
#   `name` leaf, one `arguments` leaf, one `type` leaf. The CRF uses
#   this prior to penalize predicting the same label on two siblings of
#   the same tool-call dict.
#
# Counter-example (NOT exclusive — DO NOT add):
#   `primary_content` = (content, thinking_content) was REMOVED in this
#   commit. DeepSeek-Reasoner (and xAI Grok 4) return both `content` and
#   `reasoning_content` (= thinking_content) under the SAME parent
#   `choices[0].message`. They are CANONICALLY co-occurring on
#   reasoning models, not exclusive. The COOCCUR_BIAS table now encodes
#   the positive prior; see below.
#
# Counter-example for cross-parent: Anthropic content[0].thinking and
# content[1].text have DIFFERENT parents (content[0] vs content[1]),
# so NO group rule applies between them — they sit under separate
# parent dicts.
#
# The CRF (Phase 4) groups fields by parent-object path before applying
# these masks.
EXCLUSIVE_GROUPS: dict[str, tuple[str, ...]] = {
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
    # content + thinking_content co-occur on reasoning-model responses
    # (DeepSeek-Reasoner, xAI Grok 4) and are absent together on
    # non-reasoning responses. Moderate positive prior; data refines.
    ("content",           "thinking_content",    0.5),
)
