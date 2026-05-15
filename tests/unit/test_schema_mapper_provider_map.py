"""Pin the deterministic-first provider path map in ``schema/mapper.py``.

Root cause this test guards against:
    The ONNX schema_mapper model was trained on shallow response shapes.
    Nested OpenAI ``*_details`` sub-objects didn't exist in training data.
    When given ``usage.prompt_tokens_details.cached_tokens`` (an int whose
    path contains "prompt") the model emits ``prompt_tokens`` with p=1.0 —
    a confidently-wrong classification that the path-fallback rescue
    (``_apply_path_fallbacks``) cannot override because it only fires on
    ``UNKNOWN``. The field then collides with the real
    ``usage.prompt_tokens`` slot during ``_assemble``, corrupting the
    canonical record's token counts in the gateway audit trail.

The fix: ``_PROVIDER_PATH_MAP`` in ``mapper.py`` runs BEFORE ONNX and assigns
deterministic labels for known provider paths. ONNX is only consulted for
paths NOT in the map. Plus the assembler tiebreaks collisions by shortest
path so a top-level ``usage.completion_tokens`` always wins over a nested
``usage.X.completion_tokens``.

Removing the map or the tiebreaker re-introduces silent audit corruption —
this file fails loudly when that happens.
"""
from __future__ import annotations

from gateway.schema.mapper import SchemaMapper, _PROVIDER_PATH_MAP


def test_provider_path_map_has_openai_nested_token_details() -> None:
    """OpenAI's nested *_details fields must be deterministically mapped."""
    expected = {
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
        "usage.prompt_tokens_details.cached_tokens": "cached_tokens",
        "usage.completion_tokens_details.reasoning_tokens": "reasoning_tokens",
        # No-canonical-class details land in UNKNOWN (overflow)
        "usage.prompt_tokens_details.audio_tokens": "UNKNOWN",
        "usage.completion_tokens_details.audio_tokens": "UNKNOWN",
        "usage.completion_tokens_details.accepted_prediction_tokens": "UNKNOWN",
        "usage.completion_tokens_details.rejected_prediction_tokens": "UNKNOWN",
    }
    for path, label in expected.items():
        assert _PROVIDER_PATH_MAP.get(path) == label, (
            f"_PROVIDER_PATH_MAP[{path!r}] should be {label!r}, "
            f"got {_PROVIDER_PATH_MAP.get(path)!r}. Audit token corruption regresses if removed."
        )


def test_provider_path_map_has_anthropic_cache_buckets() -> None:
    expected = {
        "usage.input_tokens": "prompt_tokens",
        "usage.output_tokens": "completion_tokens",
        "usage.cache_creation_input_tokens": "cache_creation_tokens",
        "usage.cache_read_input_tokens": "cached_tokens",
        # 5m/1h ephemeral buckets from Sonnet 4.5+
        "usage.cache_creation.ephemeral_5m_input_tokens": "cache_creation_tokens",
        "usage.cache_creation.ephemeral_1h_input_tokens": "cache_creation_tokens",
    }
    for path, label in expected.items():
        assert _PROVIDER_PATH_MAP.get(path) == label, (
            f"Anthropic path {path!r} should map to {label!r}"
        )


def test_openai_nested_details_do_not_corrupt_canonical_tokens() -> None:
    """The original audit-corruption reproduction. With distinct nonzero values
    for every token-like field, the assembled canonical record must reflect
    the top-level usage values, not the nested sub-field values."""
    mapper = SchemaMapper()
    openai = {
        "id": "x", "object": "chat.completion", "created": 1,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                     "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 10, "audio_tokens": 3},
            "completion_tokens_details": {
                "reasoning_tokens": 7,
                "audio_tokens": 11,
                "accepted_prediction_tokens": 22,
                "rejected_prediction_tokens": 33,
            },
        },
    }
    r = mapper.map_response(openai)
    assert r.usage.prompt_tokens == 100, (
        f"usage.prompt_tokens corrupted: expected 100, got {r.usage.prompt_tokens}. "
        f"Likely a nested *_details field collapsed into the prompt_tokens slot."
    )
    assert r.usage.completion_tokens == 50, (
        f"usage.completion_tokens corrupted: expected 50, got {r.usage.completion_tokens}. "
        f"Likely a nested *_details field collapsed into the completion_tokens slot."
    )
    assert r.usage.total_tokens == 150
    assert r.usage.cached_tokens == 10, (
        f"usage.cached_tokens lost: expected 10, got {r.usage.cached_tokens}. "
        f"OpenAI's nested prompt_tokens_details.cached_tokens is not reaching the canonical slot."
    )
    assert r.usage.reasoning_tokens == 7, (
        f"usage.reasoning_tokens lost: expected 7, got {r.usage.reasoning_tokens}. "
        f"OpenAI's completion_tokens_details.reasoning_tokens is not reaching the canonical slot."
    )
    # No-canonical-class fields must end up in overflow, NOT silently
    # absorbed into completion_tokens/prompt_tokens.
    overflow_paths = set(r.overflow.keys())
    expected_overflow = {
        "usage.prompt_tokens_details.audio_tokens",
        "usage.completion_tokens_details.audio_tokens",
        "usage.completion_tokens_details.accepted_prediction_tokens",
        "usage.completion_tokens_details.rejected_prediction_tokens",
    }
    missing = expected_overflow - overflow_paths
    assert not missing, (
        f"Fields with no canonical class did NOT land in overflow: {missing}. "
        f"They were silently absorbed into a canonical slot — audit corruption."
    )


def test_anthropic_cache_buckets_preserved() -> None:
    mapper = SchemaMapper()
    ant = {
        "id": "msg_x", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
        "model": "claude-haiku-4-5", "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 200, "output_tokens": 40,
            "cache_creation_input_tokens": 15, "cache_read_input_tokens": 25,
        },
    }
    r = mapper.map_response(ant)
    assert r.usage.prompt_tokens == 200
    assert r.usage.completion_tokens == 40
    assert r.usage.cache_creation_tokens == 15
    assert r.usage.cached_tokens == 25


def test_ollama_top_level_token_counts_preserved() -> None:
    mapper = SchemaMapper()
    ollama = {
        "model": "llama3.1",
        "created_at": "2026-01-01T00:00:00Z",
        "message": {"role": "assistant", "content": "hi"},
        "done": True, "done_reason": "stop",
        "total_duration": 1234567, "load_duration": 100,
        "prompt_eval_count": 50, "prompt_eval_duration": 300,
        "eval_count": 12, "eval_duration": 500,
    }
    r = mapper.map_response(ollama)
    assert r.usage.prompt_tokens == 50
    assert r.usage.completion_tokens == 12
    assert r.content == "hi"
    assert r.finish_reason == "stop"


def test_classify_fields_skips_onnx_for_deterministic_paths() -> None:
    """Pin the architectural choice: provider-map matches do NOT consult the
    ONNX session. Records the fact that nested details paths get
    deterministic-confidence=1.0, not whatever ONNX would emit."""
    from gateway.schema.features import flatten_json
    mapper = SchemaMapper()
    payload = {"usage": {"prompt_tokens_details": {"cached_tokens": 10}}}
    fields = flatten_json(payload)
    results = mapper._classify_fields(fields)
    # find the cached_tokens row
    for f, (label, conf) in zip(fields, results):
        if f.path == "usage.prompt_tokens_details.cached_tokens":
            assert label == "cached_tokens", (
                f"Expected deterministic provider-map classification, got {label!r}"
            )
            assert conf == 1.0, (
                f"Provider-map results carry confidence=1.0; got {conf}"
            )
            break
    else:
        raise AssertionError("cached_tokens path not present in flattened fields")


def test_shortest_path_wins_on_canonical_collision() -> None:
    """Even if ONNX (or a future map entry) labels two fields the same, the
    assembler must prefer the one with the SHORTEST path. Top-level
    ``usage.completion_tokens`` is authoritative; a nested counterpart never
    overrides it."""
    # We can't easily inject "two fields labeled the same" via map_response
    # without the model agreeing; instead, assert directly on the assembler
    # by feeding crafted classifications.
    from gateway.schema.features import FlatField as RealFlatField
    mapper = SchemaMapper()
    f1 = RealFlatField(
        path="usage.completion_tokens", key="completion_tokens",
        value=50, value_type="int", depth=1, parent_key="usage",
        sibling_keys=[], sibling_types=[], int_siblings=[],
    )
    f2 = RealFlatField(
        path="usage.completion_tokens_details.audio_tokens",
        key="audio_tokens", value=11, value_type="int", depth=2,
        parent_key="completion_tokens_details",
        sibling_keys=[], sibling_types=[], int_siblings=[],
    )
    classifications = [("completion_tokens", 1.0), ("completion_tokens", 1.0)]
    cr = mapper._assemble([f1, f2], classifications, {})
    assert cr.usage.completion_tokens == 50, (
        f"shortest-path tiebreaker failed: got {cr.usage.completion_tokens}, "
        f"expected 50 (the top-level usage.completion_tokens value)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic-first expansion: canonical-content paths
# ─────────────────────────────────────────────────────────────────────────────

def test_provider_map_covers_canonical_content_paths() -> None:
    """The ~13 labels that previously fell through to the ONNX residual
    are now deterministic. Pinning these prevents a silent regression
    back to ONNX guessing for content/tool/finish/identity fields."""
    expected = {
        "id": "response_id",
        "model": "model",
        "choices.0.message.content": "content",
        "choices.0.message.reasoning_content": "thinking_content",
        "choices.0.finish_reason": "finish_reason",
        "choices.0.message.tool_calls.0.id": "tool_call_id",
        "choices.0.message.tool_calls.0.type": "tool_call_type",
        "choices.0.message.tool_calls.0.function.name": "tool_call_name",
        "choices.0.message.tool_calls.0.function.arguments": "tool_call_arguments",
        "content.0.text": "content",
        "content.0.thinking": "thinking_content",
        "content.0.id": "tool_call_id",
        "content.0.name": "tool_call_name",
        "content.0.input": "tool_call_arguments",
        "content.0.citations.0.url": "citation_url",
        "stop_reason": "finish_reason",
        "message.content": "content",
        "message.thinking": "thinking_content",
        "message.reasoning_content": "thinking_content",
        "done_reason": "finish_reason",
        "message.tool_calls.0.function.name": "tool_call_name",
        "message.tool_calls.0.function.arguments": "tool_call_arguments",
    }
    for path, label in expected.items():
        assert _PROVIDER_PATH_MAP.get(path) == label, (
            f"_PROVIDER_PATH_MAP[{path!r}] should be {label!r}, "
            f"got {_PROVIDER_PATH_MAP.get(path)!r}"
        )


def test_envelope_keys_deliberately_excluded_from_provider_map() -> None:
    """No-behavior-change guarantee: envelope boilerplate must NOT be in
    the provider map. These are tagged `envelope` by the fallback layer
    and excluded from overflow; remapping them would change canonical
    output for known providers (the chosen scope explicitly forbids that)."""
    for key in ("created", "object", "role", "index",
                "system_fingerprint", "service_tier", "logprobs"):
        assert _PROVIDER_PATH_MAP.get(key) is None, (
            f"{key!r} must stay out of _PROVIDER_PATH_MAP — it is envelope "
            f"boilerplate handled by _apply_path_fallbacks. Mapping it is a "
            f"behavior change for known providers."
        )


def test_openai_content_and_tool_calls_are_deterministic() -> None:
    """OpenAI content/finish/tool-call paths classify via the provider
    map at confidence 1.0 — ONNX is not consulted for them."""
    from gateway.schema.features import flatten_json
    mapper = SchemaMapper()
    payload = {
        "id": "chatcmpl-x", "object": "chat.completion", "model": "gpt-4o-mini",
        "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": "hello",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }],
            },
        }],
    }
    fields = flatten_json(payload)
    results = mapper._classify_fields(fields)
    by_path = {f.path: (label, conf) for f, (label, conf) in zip(fields, results)}
    for path, expected_label in (
        ("choices.0.message.content", "content"),
        ("choices.0.finish_reason", "finish_reason"),
        ("choices.0.message.tool_calls.0.id", "tool_call_id"),
        ("choices.0.message.tool_calls.0.function.name", "tool_call_name"),
        ("id", "response_id"),
        ("model", "model"),
    ):
        assert by_path[path] == (expected_label, 1.0), (
            f"{path!r} → {by_path.get(path)!r}, expected ({expected_label!r}, 1.0)"
        )
    r = mapper.map_response(payload)
    assert r.content == "hello"
    assert r.finish_reason == "tool_calls"


def test_anthropic_text_block_is_deterministic_content() -> None:
    from gateway.schema.features import flatten_json
    mapper = SchemaMapper()
    payload = {
        "id": "msg_x", "type": "message", "role": "assistant",
        "model": "claude-haiku-4-5", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "the answer"}],
    }
    fields = flatten_json(payload)
    results = mapper._classify_fields(fields)
    by_path = {f.path: (label, conf) for f, (label, conf) in zip(fields, results)}
    assert by_path["content.0.text"] == ("content", 1.0)
    assert by_path["stop_reason"] == ("finish_reason", 1.0)
    r = mapper.map_response(payload)
    assert r.content == "the answer"


# ─────────────────────────────────────────────────────────────────────────────
# Schema-shape drift tracker
# ─────────────────────────────────────────────────────────────────────────────

def test_novel_shape_with_overflow_ticks_once_and_dedupes() -> None:
    """A never-seen skeleton that produces an overflow field records one
    drift event; an identical repeat does NOT re-tick (fingerprint
    dedupe)."""
    mapper = SchemaMapper()
    # `usage.prompt_tokens_details.audio_tokens` is deterministically
    # mapped to UNKNOWN → guaranteed overflow regardless of ONNX.
    payload = {
        "id": "x", "model": "gpt-4o-mini",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": {
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
            "prompt_tokens_details": {"audio_tokens": 5},
        },
    }
    assert mapper.novel_shapes_60s() == 0
    mapper.map_response(payload)
    assert mapper.novel_shapes_60s() == 1, "first novel-shape+overflow should tick"
    mapper.map_response(payload)
    assert mapper.novel_shapes_60s() == 1, "identical shape must dedupe (no re-tick)"


def test_clean_shape_does_not_tick_drift() -> None:
    """A fully-mapped response (no overflow) is not a drift signal even
    if its skeleton is novel."""
    mapper = SchemaMapper()
    payload = {
        "id": "x", "model": "gpt-4o-mini",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mapper.map_response(payload)
    assert mapper.novel_shapes_60s() == 0, (
        "a clean (no-overflow) response must not register as schema drift"
    )
