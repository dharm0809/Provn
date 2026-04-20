"""record_id + previous_record_id form a valid ID chain per session."""
from __future__ import annotations
import time
import pytest
from gateway.pipeline.hasher import build_execution_record
from gateway.pipeline.session_chain import SessionChainTracker
from gateway.adapters.base import ModelCall, ModelResponse


def _make_record(**kwargs):
    call = ModelCall(
        provider="ollama",
        model_id="test-model",
        prompt_text="hello",
        raw_body=b"{}",
        is_streaming=False,
        metadata={},
    )
    resp = ModelResponse(content="hi", usage=None, raw_body=b"")
    return build_execution_record(
        call=call,
        model_response=resp,
        attestation_id="self-attested:test-model",
        policy_version=1,
        policy_result="pass",
        tenant_id="t1",
        gateway_id="gw1",
        **kwargs,
    )


def test_build_execution_record_sets_record_id() -> None:
    rec = _make_record()
    assert "record_id" in rec
    assert len(rec["record_id"]) == 36  # UUID string form
    # Validate it's a valid UUID
    import uuid
    parsed = uuid.UUID(rec["record_id"])
    assert parsed.version == 7


def test_build_execution_record_record_ids_are_time_sortable() -> None:
    rec1 = _make_record()
    time.sleep(0.002)
    rec2 = _make_record()
    assert rec1["record_id"] < rec2["record_id"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_session_chain_produces_id_pointer() -> None:
    tracker = SessionChainTracker(ttl_seconds=60, max_sessions=100)
    cv1 = await tracker.next_chain_values("s1")
    assert cv1.previous_record_id is None
    await tracker.update("s1", sequence_number=0, record_id="rec-1")
    cv2 = await tracker.next_chain_values("s1")
    assert cv2.previous_record_id == "rec-1"


@pytest.mark.anyio
async def test_session_chain_first_record_has_no_previous_id() -> None:
    tracker = SessionChainTracker(ttl_seconds=60, max_sessions=100)
    cv = await tracker.next_chain_values("new-session")
    assert cv.previous_record_id is None
    assert cv.sequence_number == 0
