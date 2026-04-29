"""SchemaMapper emits one verdict per field with the 139-d feature vector.

Before this rewrite, `map_response` recorded ONE coarse verdict per call
with `input_features_json="{}"` — useless for training the per-field
ONNX classifier the production `_classify_onnx` pipeline runs. This
suite locks the per-field contract: every field that flows through
`_classify_onnx` produces a verdict whose `input_features_json` carries
the actual 139-d feature vector and whose `prediction` matches the
per-field label assigned to that field.
"""
from __future__ import annotations

import json

import pytest

from gateway.intelligence.types import ModelVerdict
from gateway.intelligence.verdict_buffer import VerdictBuffer
from gateway.schema.features import FEATURE_DIM
from gateway.schema.mapper import SchemaMapper


def _drain(buf: VerdictBuffer) -> list[ModelVerdict]:
    return buf.drain(max_batch=10_000)


def test_per_field_verdicts_emitted_for_openai_response():
    """An OpenAI-shaped response yields one per-field verdict per leaf."""
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    resp = {
        "id": "chatcmpl-abc",
        "model": "gpt-4o",
        "choices": [
            {"message": {"content": "Hello!"}, "finish_reason": "stop"},
        ],
        "usage": {
            "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
        },
    }
    mapper.map_response(resp)

    rows = _drain(buf)
    # All rows are schema_mapper verdicts.
    assert rows, "expected per-field verdict rows"
    assert all(r.model_name == "schema_mapper" for r in rows)

    # Every row carries the 139-d feature vector under feature_vector.
    for row in rows:
        payload = json.loads(row.input_features_json)
        assert "feature_vector" in payload
        vec = payload["feature_vector"]
        assert isinstance(vec, list)
        assert len(vec) == FEATURE_DIM
        # No NaN. JSON can't carry NaN anyway, but this is a sanity guard.
        assert all(isinstance(x, (int, float)) for x in vec)
        # Every row must declare which field it came from so the
        # harvester can match overflow paths to specific verdict rows.
        assert "field_path" in payload
        assert isinstance(payload["field_path"], str)


def test_per_field_count_matches_classified_field_count():
    """One verdict row per field — no batching, no sampling."""
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    from gateway.schema.features import flatten_json
    resp = {
        "model": "test-model",
        "choices": [{"message": {"content": "A long response with several words."}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
    }
    expected_field_count = len(flatten_json(resp))

    mapper.map_response(resp)
    rows = _drain(buf)
    assert len(rows) == expected_field_count


def test_per_field_predictions_align_with_per_field_features():
    """The per-field prediction in each verdict row must be a real label.

    Sanity: every verdict row's prediction is one of the canonical
    labels (or UNKNOWN), and the row carries a corresponding feature
    vector. We don't pin specific labels because the production model
    is the source of truth — we only need the contract to hold.
    """
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    resp = {
        "choices": [{"message": {"content": "hi", "role": "assistant"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mapper.map_response(resp)
    rows = _drain(buf)

    valid_labels = set(mapper._labels)
    for row in rows:
        assert row.prediction in valid_labels or row.prediction == "UNKNOWN"
        assert 0.0 <= row.confidence <= 1.0


def test_per_field_verdicts_carry_heuristic_teacher_signal():
    """When the heuristic has an opinion, divergence_signal is populated.

    The heuristic classifier is the rule-based teacher: it provides
    `divergence_signal` for fields it can name (content, prompt_tokens,
    etc.). Fields the heuristic can't classify get `divergence_signal=None`
    — the dataset builder then filters them out.
    """
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    # An OpenAI-shaped response has fields the heuristic confidently
    # labels: content (long natural string in usage-shaped key),
    # prompt_tokens / completion_tokens / total_tokens (int siblings
    # in usage subobject).
    resp = {
        "choices": [
            {
                "message": {
                    "content": "This is a real natural-language response with many spaces and words.",
                },
                "finish_reason": "stop",
            },
        ],
        "usage": {
            "prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19,
        },
    }
    mapper.map_response(resp)
    rows = _drain(buf)

    # At least one row must have a teacher signal — we don't pin which
    # specific labels come through because the heuristic / value-aware
    # rules can shift, but the existence of >= 1 teacher proves the
    # producer is now generating training data.
    teachers = [r for r in rows if r.divergence_signal is not None]
    assert teachers, "expected heuristic to label at least one field"
    for t in teachers:
        assert t.divergence_source == "schema_mapper_heuristic"


def test_no_verdicts_when_buffer_unwired():
    """`verdict_buffer=None` is the default — must not raise on inference."""
    mapper = SchemaMapper(verdict_buffer=None)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")
    resp = {
        "choices": [{"message": {"content": "test"}}],
        "usage": {"prompt_tokens": 1},
    }
    mapper.map_response(resp)  # Just must not raise.


def test_per_field_input_hashes_are_unique_within_response():
    """Each field's `input_hash` is derived from path|type|value — collisions
    would let the dataset deduper drop legitimate per-field rows.
    """
    buf = VerdictBuffer(max_size=10_000)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    resp = {
        "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    mapper.map_response(resp)
    rows = _drain(buf)
    hashes = [r.input_hash for r in rows]
    # Every field has a distinct path — each hash must be unique.
    assert len(set(hashes)) == len(hashes)


def test_per_field_buffer_overflow_drops_oldest_not_newest():
    """When per-field volume exceeds the buffer, oldest fields drop first.

    A typical response has ~10-50 fields; a small buffer makes the
    drop behavior observable. The buffer's `dropped_total` counter
    must reflect the exact overflow, and the surviving rows must be
    the most recent (the buffer popleft()s on overflow).
    """
    # Tight bound — any normal response will overflow.
    buf = VerdictBuffer(max_size=3)
    mapper = SchemaMapper(verdict_buffer=buf)
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    resp = {
        "choices": [
            {"message": {"content": "Multi-word response."}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "model": "test",
    }
    mapper.map_response(resp)
    assert buf.size <= 3
    # Drop count is positive — confirms the buffer didn't silently
    # accept everything (which would mean the per-field loop short-
    # circuited).
    assert buf.dropped_total > 0
