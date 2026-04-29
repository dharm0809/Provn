"""G2 compliance: gateway sends prompt_text and response_content; hashes are computed by Walcor backend."""

import os
import tempfile
import pytest


_PROMPT_TEXT = "Hello world"
_RESPONSE_TEXT = "Hi there"


def test_g2_execution_record_no_hashes_from_gateway():
    """G2: Gateway sends dict records without prompt_hash/response_hash; backend hashes from content."""
    # Gateway build_execution_record returns a dict; no hash keys.
    from gateway.pipeline.hasher import build_execution_record
    from gateway.adapters.base import ModelCall, ModelResponse

    call = ModelCall(
        provider="openai",
        model_id="gpt-4o",
        prompt_text=_PROMPT_TEXT,
        raw_body=b"{}",
        is_streaming=False,
        metadata={},
    )
    model_response = ModelResponse(
        content=_RESPONSE_TEXT,
        usage=None,
        raw_body=b"{}",
    )
    record = build_execution_record(
        call=call,
        model_response=model_response,
        attestation_id="att_001",
        policy_version=1,
        policy_result="pass",
        tenant_id="t1",
        gateway_id="gw-1",
    )
    assert isinstance(record, dict)
    assert record["prompt_text"] == _PROMPT_TEXT
    assert record["response_content"] == _RESPONSE_TEXT
    assert "prompt_hash" not in record
    assert "response_hash" not in record
    assert "execution_id" in record


def test_g2_durability_wal_persists_across_restart():
    """G2 durability: Record written to WAL is still present after close and reopen (crash recovery)."""
    from gateway.wal.writer import WALWriter

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "wal.db")
        record = {
            "execution_id": "eid-durability-001",
            "model_attestation_id": "att_001",
            "policy_version": 1,
            "policy_result": "pass",
            "tenant_id": "t1",
            "gateway_id": "gw-1",
            "timestamp": "2026-02-16T12:00:00Z",
        }
        w1 = WALWriter(db_path)
        w1.write_durable(record)
        w1.close()

        w2 = WALWriter(db_path)
        rows = w2.get_undelivered(limit=10)
        w2.close()
        assert len(rows) == 1
        assert rows[0][0] == "eid-durability-001"


@pytest.mark.asyncio
async def test_g2_idempotent_409_marks_delivered():
    """G2 idempotent: When control plane returns 409 (duplicate), delivery worker marks record delivered."""
    from unittest.mock import AsyncMock, patch
    from gateway.wal.writer import WALWriter
    from gateway.wal.delivery_worker import DeliveryWorker

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "wal.db")
        wal = WALWriter(db_path)
        record = {
            "execution_id": "eid-409-test",
            "model_attestation_id": "att_001",
            "policy_version": 1,
            "policy_result": "pass",
            "tenant_id": "t1",
            "gateway_id": "gw-1",
            "timestamp": "2026-02-16T12:00:00Z",
        }
        wal.write_durable(record)
        assert wal.pending_count() == 1

        mock_resp = AsyncMock()
        mock_resp.status_code = 409
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_resp)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=None)

        with patch("gateway.wal.delivery_worker.get_settings") as gs:
            gs.return_value.control_plane_url = "http://controlplane"
            gs.return_value.control_plane_api_key = ""
            with patch("httpx.AsyncClient", return_value=mock_client_instance):
                worker = DeliveryWorker(wal)
                await worker._deliver_batch()

        assert wal.pending_count() == 0
        wal.close()
