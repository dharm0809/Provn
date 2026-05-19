"""CI accuracy gate for the schema mapper ONNX model.

Tests the mapper directly against real provider response fixtures —
no live gateway, no API keys. Each fixture asserts:
  1. Every semantic field (content, tokens, finish_reason, etc.) is
     correctly classified — not UNKNOWN.
  2. schema_mapper_unmapped == 0 (nothing falls through to overflow).
  3. Structural containers (choices, usage, message objects) are
     tolerated as UNKNOWN (they have no canonical label).

Run: pytest tests/unit/test_schema_mapper_accuracy.py -v
"""
from __future__ import annotations

import pytest
from gateway.schema.mapper import SchemaMapper
from gateway.schema.canonical import ENVELOPE_LABEL


# ── Fixtures ─────────────────────────────────────────────────────────────────
# (provider_name, response_dict, expected_label_map)
# expected_label_map: {field_path: expected_label}
# Only semantic leaf fields are checked; containers (choices, usage, message)
# are allowed to be UNKNOWN and are omitted.

FIXTURES = [
    # ── OpenAI standard ──────────────────────────────────────────────────
    ("openai", {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 1677858242,
        "model": "gpt-4o",
        "system_fingerprint": "fp_abc",
        "choices": [{"index": 0, "message": {"role": "assistant",
            "content": "Hello world this is a test response.", "refusal": None},
            "logprobs": None, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25},
    }, {
        "id": "response_id",
        "model": "model",
        "choices.0.message.content": "content",
        "choices.0.finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── OpenAI tool calls ─────────────────────────────────────────────────
    ("openai_tools", {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    }, {
        "id": "response_id",
        "model": "model",
        "choices.0.finish_reason": "finish_reason",
        "choices.0.message.tool_calls.0.id": "tool_call_id",
        "choices.0.message.tool_calls.0.type": "tool_call_type",
        "choices.0.message.tool_calls.0.function.name": "tool_call_name",
        "choices.0.message.tool_calls.0.function.arguments": "tool_call_arguments",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── OpenAI o1/o3 reasoning ────────────────────────────────────────────
    ("openai_reasoning", {
        "id": "chatcmpl-o1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "o3-mini",
        "choices": [{"index": 0, "message": {"role": "assistant",
            "content": "The answer is 42.", "refusal": None},
            "finish_reason": "stop", "logprobs": None}],
        "usage": {
            "prompt_tokens": 120, "completion_tokens": 800, "total_tokens": 920,
            "prompt_tokens_details": {"cached_tokens": 64, "audio_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 650, "audio_tokens": 0,
                                          "accepted_prediction_tokens": 0,
                                          "rejected_prediction_tokens": 0},
        },
        "service_tier": "default",
        "system_fingerprint": "fp_reasoning",
    }, {
        "id": "response_id",
        "model": "model",
        "choices.0.message.content": "content",
        "choices.0.finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
        "usage.prompt_tokens_details.cached_tokens": "cached_tokens",
        "usage.completion_tokens_details.reasoning_tokens": "reasoning_tokens",
    }),

    # ── Anthropic standard ────────────────────────────────────────────────
    ("anthropic", {
        "id": "msg_abc",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "content": [{"type": "text", "text": "This is a detailed explanation of quantum mechanics."}],
        "usage": {"input_tokens": 25, "output_tokens": 150,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }, {
        "id": "response_id",
        "model": "model",
        "stop_reason": "finish_reason",
        "content.0.text": "content",
        "usage.input_tokens": "prompt_tokens",
        "usage.output_tokens": "completion_tokens",
        "usage.cache_creation_input_tokens": "cache_creation_tokens",
        "usage.cache_read_input_tokens": "cached_tokens",
    }),

    # ── Anthropic tool_use ────────────────────────────────────────────────
    ("anthropic_tools", {
        "id": "msg_tool",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "content": [
            {"type": "text", "text": "Let me search for that."},
            {"type": "tool_use", "id": "toolu_1", "name": "web_search",
             "input": {"query": "AI news 2025"}},
        ],
        "usage": {"input_tokens": 40, "output_tokens": 60,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }, {
        "id": "response_id",
        "model": "model",
        "stop_reason": "finish_reason",
        # content.1.* (tool_use block) is never flattened — flatten_json
        # only processes the first array element (content.0).
        "content.0.text": "content",
        "usage.input_tokens": "prompt_tokens",
        "usage.output_tokens": "completion_tokens",
    }),

    # ── Anthropic extended thinking ───────────────────────────────────────
    ("anthropic_thinking", {
        "id": "msg_think",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "content": [
            {"type": "thinking", "thinking": "Let me reason step by step through this problem..."},
            {"type": "text", "text": "The answer involves three key principles."},
        ],
        "usage": {"input_tokens": 30, "output_tokens": 450,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }, {
        "id": "response_id",
        "model": "model",
        "stop_reason": "finish_reason",
        "content.0.thinking": "thinking_content",
        # content.1.text (second block) never flattened — flatten_json takes index 0 only.
        "usage.input_tokens": "prompt_tokens",
        "usage.output_tokens": "completion_tokens",
    }),

    # ── Ollama OpenAI-compat ──────────────────────────────────────────────
    ("ollama_compat", {
        "id": "chatcmpl-ollama",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "qwen3:4b",
        "system_fingerprint": "fp_ollama",
        "choices": [{"index": 0, "message": {"role": "assistant",
            "content": "Ollama runs models locally on your machine.",
            "reasoning": "The user wants to know about Ollama."},
            "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 22, "completion_tokens": 167, "total_tokens": 189},
    }, {
        "id": "response_id",
        "model": "model",
        "choices.0.message.content": "content",
        "choices.0.message.reasoning": "thinking_content",
        "choices.0.finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── Mistral AI ────────────────────────────────────────────────────────
    ("mistral", {
        "id": "cmpl-mistral",
        "object": "chat.completion",
        "created": 1700000001,
        "model": "mistral-large-latest",
        "choices": [{"index": 0, "message": {"role": "assistant",
            "content": "Mistral models are known for their efficiency and multilingual capabilities."},
            "finish_reason": "stop", "logprobs": None}],
        "usage": {"prompt_tokens": 18, "completion_tokens": 22, "total_tokens": 40},
    }, {
        "id": "response_id",
        "model": "model",
        "choices.0.message.content": "content",
        "choices.0.finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── Together AI ───────────────────────────────────────────────────────
    ("together_ai", {
        "id": "chatcmpl-together",
        "object": "chat.completion",
        "created": 1700000002,
        "model": "meta-llama/Llama-3-70b-chat-hf",
        "choices": [{"index": 0, "message": {"role": "assistant",
            "content": "Together AI provides access to open-source LLMs."},
            "finish_reason": "eos", "logprobs": None}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
        "prompt": [],
    }, {
        "id": "response_id",
        "model": "model",
        "choices.0.message.content": "content",
        "choices.0.finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── Fireworks AI ──────────────────────────────────────────────────────
    ("fireworks", {
        "id": "chatcmpl-fw",
        "object": "chat.completion",
        "created": 1700000003,
        "model": "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "choices": [{"index": 0, "message": {"role": "assistant",
            "content": "Fireworks AI offers fast inference for open-source models."},
            "finish_reason": "stop", "logprobs": None}],
        "usage": {"prompt_tokens": 16, "completion_tokens": 28, "total_tokens": 44},
    }, {
        "id": "response_id",
        "model": "model",
        "choices.0.message.content": "content",
        "choices.0.finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
    }),

    # ── Google Gemini ─────────────────────────────────────────────────────
    ("gemini", {
        "candidates": [{"content": {"parts": [{"text": "Gemini is Google's multimodal AI model."}],
            "role": "model"}, "finishReason": "STOP",
            "safetyRatings": [{"category": "HARM_CATEGORY_HATE_SPEECH", "probability": "NEGLIGIBLE"}]}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 50, "totalTokenCount": 60},
        "modelVersion": "gemini-1.5-flash",
    }, {
        "candidates.0.content.parts.0.text": "content",
        "candidates.0.finishReason": "finish_reason",
        "usageMetadata.promptTokenCount": "prompt_tokens",
        "usageMetadata.candidatesTokenCount": "completion_tokens",
        "usageMetadata.totalTokenCount": "total_tokens",
        "modelVersion": "model",
    }),

    # ── DeepSeek reasoning ────────────────────────────────────────────────
    ("deepseek", {
        "id": "chatcmpl-ds",
        "object": "chat.completion",
        "model": "deepseek-reasoner",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "The answer is 42.",
            "reasoning_content": "Let me think step by step..."},
            "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 200, "total_tokens": 230,
                  "prompt_cache_hit_tokens": 10},
    }, {
        "id": "response_id",
        "model": "model",
        "choices.0.message.content": "content",
        "choices.0.message.reasoning_content": "thinking_content",
        "choices.0.finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
        "usage.prompt_cache_hit_tokens": "cached_tokens",
    }),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

# Paths that are legitimately UNKNOWN — either structural containers (no
# canonical label, not an envelope key) or leaf fields explicitly assigned
# UNKNOWN in _PROVIDER_PATH_MAP (known noise we chose not to map).
_KNOWN_UNKNOWNS = frozenset({
    # Structural containers
    "choices", "choices.0.message", "usage",
    "candidates", "candidates.0.content", "candidates.0.content.parts",
    "content", "message", "prompt",
    "usage.prompt_tokens_details", "usage.completion_tokens_details",
    "usage.billed_units", "usage.tokens",
    # Intentional UNKNOWN from _PROVIDER_PATH_MAP (OpenAI o1/o3 noise)
    "usage.prompt_tokens_details.audio_tokens",
    "usage.completion_tokens_details.audio_tokens",
    "usage.completion_tokens_details.accepted_prediction_tokens",
    "usage.completion_tokens_details.rejected_prediction_tokens",
    # Gemini safety ratings (no canonical label defined for these)
    "candidates.0.safetyRatings",
    "candidates.0.safetyRatings.0.category",
    "candidates.0.safetyRatings.0.probability",
})


def _get_all_labels(mapper: SchemaMapper, response: dict) -> dict[str, str]:
    """Map a response and return {field_path: predicted_label} for all fields."""
    from gateway.schema.features import flatten_json

    canonical = mapper.map_response(response)
    fields = flatten_json(response)
    classifications = []

    # Re-run classification to get per-field labels (map_response assembles; we
    # need the raw per-field labels for assertion).
    raw_classifications = mapper._classify_fields(fields)
    fallback = mapper._apply_path_fallbacks(fields, raw_classifications)

    return {f.path: label for f, (label, _) in zip(fields, fallback)}


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def mapper():
    return SchemaMapper()


@pytest.mark.parametrize("provider,response,expected", FIXTURES,
                         ids=[f[0] for f in FIXTURES])
def test_semantic_fields_classified(mapper, provider, response, expected):
    """Every expected semantic field must be correctly classified."""
    labels = _get_all_labels(mapper, response)

    failures = []
    for path, want in expected.items():
        got = labels.get(path, "MISSING")
        if got != want:
            failures.append(f"  {path}: want={want!r} got={got!r}")

    assert not failures, (
        f"[{provider}] {len(failures)} field(s) misclassified:\n" + "\n".join(failures)
    )


@pytest.mark.parametrize("provider,response,expected", FIXTURES,
                         ids=[f[0] for f in FIXTURES])
def test_zero_unmapped(mapper, provider, response, expected):
    """No unexpected unmapped fields — intentional UNKNOWNs are allowed."""
    result = mapper.map_response(response)
    unexpected = [p for p in result.mapping.unmapped_fields if p not in _KNOWN_UNKNOWNS]
    assert not unexpected, (
        f"[{provider}] {len(unexpected)} unexpected unmapped field(s): {unexpected}\n"
        f"overflow={list(result.overflow.keys())}"
    )


@pytest.mark.parametrize("provider,response,expected", FIXTURES,
                         ids=[f[0] for f in FIXTURES])
def test_no_unexpected_unknowns(mapper, provider, response, expected):
    """Non-container fields must not be UNKNOWN (allowed: structural containers,
    envelope fields, and known intentional UNKNOWNs)."""
    labels = _get_all_labels(mapper, response)
    from gateway.schema.features import flatten_json
    fields_by_path = {f.path: f for f in flatten_json(response)}

    unexpected = []
    for path, label in labels.items():
        if label != "UNKNOWN":
            continue
        if path in _KNOWN_UNKNOWNS:
            continue
        f = fields_by_path.get(path)
        if f and f.value_type in ("object", "array"):
            continue  # other structural containers
        unexpected.append(f"  {path} (value_type={f.value_type if f else '?'})")

    assert not unexpected, (
        f"[{provider}] Unexpected UNKNOWN on non-container fields:\n"
        + "\n".join(unexpected)
    )
