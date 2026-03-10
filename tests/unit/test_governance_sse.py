# tests/unit/test_governance_sse.py
import pytest
import json


def test_governance_sse_event_format():
    """Governance SSE event has correct format."""
    from gateway.pipeline.forwarder import build_governance_sse_event

    event = build_governance_sse_event(
        execution_id="exec-123",
        attestation_id="self-attested:qwen3:4b",
        chain_seq=5,
        policy_result="allowed",
    )

    assert event.startswith(b"event: governance\n")
    assert b"data: " in event
    assert event.endswith(b"\n\n")

    # Extract JSON from data line
    lines = event.decode().strip().split("\n")
    data_line = [l for l in lines if l.startswith("data: ")][0]
    payload = json.loads(data_line[6:])
    assert payload["execution_id"] == "exec-123"
    assert payload["attestation_id"] == "self-attested:qwen3:4b"
    assert payload["chain_seq"] == 5
    assert payload["policy_result"] == "allowed"
