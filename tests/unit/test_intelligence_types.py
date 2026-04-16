from __future__ import annotations

from datetime import datetime

from gateway.intelligence.types import ModelVerdict


def test_verdict_from_inference():
    v = ModelVerdict.from_inference(
        model_name="intent", input_text="search for python",
        prediction="web_search", confidence=0.87, request_id="req-1",
    )
    assert v.model_name == "intent"
    assert v.prediction == "web_search"
    assert v.confidence == 0.87
    assert v.request_id == "req-1"
    assert len(v.input_hash) == 64  # sha256
    assert v.input_features_json == "{}"  # default when no features supplied
    assert v.divergence_signal is None
    assert v.divergence_source is None


def test_verdict_timestamp_is_iso8601_utc():
    v = ModelVerdict.from_inference(
        model_name="intent", input_text="x", prediction="normal", confidence=0.5,
    )
    assert isinstance(v.timestamp, str)
    parsed = datetime.fromisoformat(v.timestamp)
    assert parsed.tzinfo is not None


def test_input_hash_deterministic():
    v1 = ModelVerdict.from_inference(
        model_name="safety", input_text="hello world",
        prediction="safe", confidence=0.99,
    )
    v2 = ModelVerdict.from_inference(
        model_name="safety", input_text="hello world",
        prediction="safe", confidence=0.99,
    )
    assert v1.input_hash == v2.input_hash


def test_input_hash_differs_on_different_text():
    v1 = ModelVerdict.from_inference(
        model_name="safety", input_text="hello world",
        prediction="safe", confidence=0.99,
    )
    v2 = ModelVerdict.from_inference(
        model_name="safety", input_text="hello worlds",  # one char different
        prediction="safe", confidence=0.99,
    )
    assert v1.input_hash != v2.input_hash


def test_features_json_is_sorted():
    # Sort for determinism — two logically-equal feature dicts should serialize
    # to the same JSON string regardless of insertion order.
    v1 = ModelVerdict.from_inference(
        model_name="schema", input_text="x", prediction="content",
        confidence=0.8, features={"b": 2, "a": 1},
    )
    v2 = ModelVerdict.from_inference(
        model_name="schema", input_text="x", prediction="content",
        confidence=0.8, features={"a": 1, "b": 2},
    )
    assert v1.input_features_json == v2.input_features_json
