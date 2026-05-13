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
