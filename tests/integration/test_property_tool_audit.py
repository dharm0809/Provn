"""Phase B2: Property-based tests for tool audit hash invariants (I6)."""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from gateway.core import compute_sha3_512_string
from gateway.adapters.base import ToolInteraction, ModelCall, ModelResponse


# ---------------------------------------------------------------------------
# Helper: compute tool hash the same way the gateway does
# ---------------------------------------------------------------------------

def compute_tool_hash(data: dict | str | None) -> str:
    if data is None:
        return compute_sha3_512_string("")
    if isinstance(data, dict):
        return compute_sha3_512_string(json.dumps(data, sort_keys=True))
    return compute_sha3_512_string(str(data))


# ---------------------------------------------------------------------------
# Basic hash property tests
# ---------------------------------------------------------------------------

def test_hash_determinism():
    """Same input → same hash on repeated calls."""
    text = "hello walacor gateway"
    h1 = compute_sha3_512_string(text)
    h2 = compute_sha3_512_string(text)
    assert h1 == h2


def test_hash_uniqueness():
    """Different inputs → different hashes (with high probability)."""
    inputs = ["alpha", "beta", "gamma delta", "epsilon" * 10]
    hashes = [compute_sha3_512_string(x) for x in inputs]
    assert len(set(hashes)) == len(inputs), "hash collision detected"


def test_hash_length():
    """SHA3-512 output is always 128 hex chars."""
    for text in ["", "a", "hello world", "x" * 10000]:
        h = compute_sha3_512_string(text)
        assert len(h) == 128, f"expected 128 chars, got {len(h)} for input len {len(text)}"
        assert all(c in "0123456789abcdef" for c in h), "non-hex character in hash"


def test_empty_input_hash():
    """compute_sha3_512_string('') returns a valid 128-char hex string."""
    h = compute_sha3_512_string("")
    assert len(h) == 128
    assert all(c in "0123456789abcdef" for c in h)


def test_dict_serialization_stability():
    """Same dict with different key ordering → same hash (sort_keys=True)."""
    d1 = {"b": "2", "a": "1", "c": "3"}
    d2 = {"c": "3", "a": "1", "b": "2"}
    assert compute_tool_hash(d1) == compute_tool_hash(d2)


def test_none_input_hash():
    """None input → hash of empty string."""
    h = compute_tool_hash(None)
    assert h == compute_sha3_512_string("")
    assert len(h) == 128


def test_tool_interaction_input_hash_matches():
    """Build a ToolInteraction with known input_data and verify hash matches."""
    input_data = {"query": "what is the capital of France", "max_results": 5}
    expected_hash = compute_tool_hash(input_data)

    ti = ToolInteraction(
        tool_id="call_abc123",
        tool_type="function",
        tool_name="web_search",
        input_data=input_data,
        output_data={"results": ["Paris is the capital"]},
        sources=[{"url": "https://example.com", "title": "France"}],
        metadata={"iteration": 1, "duration_ms": 250},
    )

    # Compute hash from the ToolInteraction's input_data
    actual_hash = compute_tool_hash(ti.input_data)
    assert actual_hash == expected_hash
    assert len(actual_hash) == 128


def test_tool_interaction_output_hash_matches():
    """Output hash computed from output_data matches expected."""
    output_data = {"results": ["Paris", "Lyon", "Marseille"]}
    expected_hash = compute_tool_hash(output_data)

    ti = ToolInteraction(
        tool_id="call_xyz789",
        tool_type="web_search",
        tool_name=None,
        input_data={"query": "cities in France"},
        output_data=output_data,
        sources=None,
        metadata=None,
    )

    actual_hash = compute_tool_hash(ti.output_data)
    assert actual_hash == expected_hash


def test_tool_interaction_input_data_preserved():
    """ToolInteraction stores actual arguments (not just hash)."""
    input_data = {"city": "Tokyo", "country": "Japan"}
    ti = ToolInteraction(
        tool_id="call_001",
        tool_type="function",
        tool_name="lookup",
        input_data=input_data,
        output_data=None,
        sources=None,
        metadata=None,
    )
    assert ti.input_data == input_data
    assert ti.input_data["city"] == "Tokyo"


# ---------------------------------------------------------------------------
# build_execution_record tests
# ---------------------------------------------------------------------------

def _make_call(model_id="test-model", prompt="hello"):
    return ModelCall(
        provider="ollama",
        model_id=model_id,
        prompt_text=prompt,
        raw_body=b"{}",
        is_streaming=False,
        metadata={"session_id": str(uuid.uuid4())},
    )


def _make_response(content="world"):
    return ModelResponse(
        content=content,
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        raw_body=b"{}",
        model_hash=None,
        thinking_content=None,
        provider_request_id=None,
    )


def test_build_execution_record_required_keys():
    """build_execution_record produces a dict with all required keys."""
    from gateway.pipeline.hasher import build_execution_record

    call = _make_call()
    resp = _make_response()
    record = build_execution_record(
        call=call,
        model_response=resp,
        attestation_id="self-attested:test-model",
        policy_version=1,
        policy_result="pass",
        tenant_id="tenant-test",
        gateway_id="gw-test",
    )

    required_keys = [
        "execution_id", "model_id", "policy_version", "policy_result",
        "tenant_id", "gateway_id", "timestamp", "prompt_tokens",
        "completion_tokens", "total_tokens",
    ]
    for key in required_keys:
        assert key in record, f"missing key: {key}"


def test_build_execution_record_execution_id_is_uuid():
    """execution_id is a valid UUID."""
    from gateway.pipeline.hasher import build_execution_record

    call = _make_call()
    resp = _make_response()
    record = build_execution_record(
        call=call, model_response=resp,
        attestation_id="self-attested:test-model",
        policy_version=1, policy_result="pass",
        tenant_id="t", gateway_id="g",
    )
    # Should not raise
    parsed = uuid.UUID(record["execution_id"])
    assert str(parsed) == record["execution_id"]


def test_build_execution_record_timestamp_iso():
    """timestamp is an ISO format string."""
    from gateway.pipeline.hasher import build_execution_record

    call = _make_call()
    resp = _make_response()
    record = build_execution_record(
        call=call, model_response=resp,
        attestation_id="self-attested:test-model",
        policy_version=1, policy_result="pass",
        tenant_id="t", gateway_id="g",
    )
    ts = record["timestamp"]
    # Should parse without error
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed is not None


def test_build_execution_record_tokens_nonnegative():
    """Token counts are non-negative integers."""
    from gateway.pipeline.hasher import build_execution_record

    call = _make_call()
    resp = _make_response()
    record = build_execution_record(
        call=call, model_response=resp,
        attestation_id="self-attested:test-model",
        policy_version=1, policy_result="pass",
        tenant_id="t", gateway_id="g",
    )
    assert record["prompt_tokens"] >= 0
    assert record["completion_tokens"] >= 0
    assert record["total_tokens"] >= 0


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

@given(
    text=st.text(min_size=0, max_size=1000),
)
@h_settings(max_examples=50)
def test_hypothesis_hash_length_always_128(text):
    """Hash is always 128 chars for any input."""
    h = compute_sha3_512_string(text)
    assert len(h) == 128
    assert all(c in "0123456789abcdef" for c in h)


@given(
    keys=st.lists(st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=["L", "N"])), min_size=1, max_size=10, unique=True),
    values=st.lists(st.text(min_size=0, max_size=50), min_size=1, max_size=10),
)
@h_settings(max_examples=50)
def test_hypothesis_dict_hash_determinism(keys, values):
    """Same dict always hashes to same value."""
    # Truncate lists to same length
    n = min(len(keys), len(values))
    if n == 0:
        return
    d = dict(zip(keys[:n], values[:n]))
    h1 = compute_tool_hash(d)
    h2 = compute_tool_hash(d)
    assert h1 == h2
    assert len(h1) == 128


@given(
    model_id=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=["L", "N", "P"])),
    prompt=st.text(min_size=1, max_size=200),
    content=st.text(min_size=0, max_size=200),
)
@h_settings(max_examples=20)
def test_hypothesis_build_execution_record(model_id, prompt, content):
    """build_execution_record produces valid records for arbitrary inputs."""
    from gateway.pipeline.hasher import build_execution_record

    call = _make_call(model_id=model_id, prompt=prompt)
    resp = _make_response(content=content)
    record = build_execution_record(
        call=call, model_response=resp,
        attestation_id=f"self-attested:{model_id}",
        policy_version=1, policy_result="pass",
        tenant_id="t", gateway_id="g",
    )

    assert len(record["execution_id"]) == 36  # UUID format
    assert record["prompt_tokens"] >= 0
    assert record["completion_tokens"] >= 0
    assert record["total_tokens"] >= 0
    assert record["model_id"] == model_id
