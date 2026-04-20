"""WalacorLineageReader: 5 query paths + type coercion tests."""
from __future__ import annotations
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from gateway.lineage.walacor_reader import WalacorLineageReader


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_reader() -> WalacorLineageReader:
    client = MagicMock()
    client.query_complex = AsyncMock(return_value=[])
    reader = WalacorLineageReader.__new__(WalacorLineageReader)
    reader._client = client
    reader._exec_etid = "9000001"
    reader._tool_etid = "9000003"
    reader._attempt_etid = "9000002"
    reader.logger = MagicMock()
    return reader


# ── 1. get_execution: returns None on empty result ────────────────────────────

@pytest.mark.anyio
async def test_get_execution_returns_none_when_not_found() -> None:
    reader = _make_reader()
    reader._client.query_complex.return_value = []
    result = await reader.get_execution("exec-missing")
    assert result is None


# ── 2. get_execution: normalizes walacor envelope fields ─────────────────────

@pytest.mark.anyio
async def test_get_execution_normalizes_envelope_fields() -> None:
    reader = _make_reader()
    record = {
        "execution_id": "exec-1",
        "EId": "eid-1",
        "env": [{"BlockId": "blk-1", "DH": "dh-abc", "TransId": "txn-1", "BL": 5}],
        "record_id": "rec-1",
    }
    reader._client.query_complex.return_value = [record]
    result = await reader.get_execution("exec-1")
    assert result is not None
    assert result["walacor_block_id"] == "blk-1"
    assert result["walacor_dh"] == "dh-abc"
    assert result["walacor_trans_id"] == "txn-1"


# ── 3. get_tool_events: JSON string fields are deserialized ───────────────────

@pytest.mark.anyio
async def test_get_tool_events_deserializes_json_string_fields() -> None:
    reader = _make_reader()
    sources_data = [{"url": "https://example.com", "title": "Example"}]
    reader._client.query_complex.return_value = [{
        "execution_id": "exec-1",
        "tool_name": "web_search",
        "input_data": json.dumps({"query": "hello"}),
        "sources": json.dumps(sources_data),
    }]
    result = await reader.get_tool_events("exec-1")
    assert isinstance(result[0]["input_data"], dict)
    assert isinstance(result[0]["sources"], list)
    assert result[0]["sources"][0]["url"] == "https://example.com"


# ── 4. get_tool_events: malformed JSON string fields pass through ─────────────

@pytest.mark.anyio
async def test_get_tool_events_passes_through_malformed_json() -> None:
    reader = _make_reader()
    reader._client.query_complex.return_value = [{
        "execution_id": "exec-1",
        "tool_name": "fetch",
        "sources": "{broken json",
    }]
    result = await reader.get_tool_events("exec-1")
    # Malformed string stays as-is rather than crashing
    assert result[0]["sources"] == "{broken json"


# ── 5. get_execution_trace: assembles execution + tool_events + timings ───────

@pytest.mark.anyio
async def test_get_execution_trace_returns_composite() -> None:
    reader = _make_reader()
    execution_record = {"execution_id": "exec-1", "record_id": "rec-1",
                        "timings": json.dumps({"forward_ms": 120})}
    tool_record = {"execution_id": "exec-1", "tool_name": "web_search"}

    call_count = [0]
    async def _query_side_effect(etid, pipeline):
        call_count[0] += 1
        if etid == reader._exec_etid:
            return [execution_record]
        return [tool_record]

    reader._client.query_complex.side_effect = _query_side_effect
    result = await reader.get_execution_trace("exec-1")
    assert result is not None
    assert result["execution"]["execution_id"] == "exec-1"
    assert len(result["tool_events"]) == 1
    assert result["timings"]["forward_ms"] == 120


# ── 6. Type coercion: timings string → dict ───────────────────────────────────

@pytest.mark.anyio
async def test_get_execution_trace_coerces_timings_string_to_dict() -> None:
    reader = _make_reader()
    execution_record = {"execution_id": "exec-2", "record_id": "rec-2",
                        "timings": "{\"forward_ms\": 99}"}

    async def _query(etid, pipeline):
        if etid == reader._exec_etid:
            return [execution_record]
        return []

    reader._client.query_complex.side_effect = _query
    result = await reader.get_execution_trace("exec-2")
    assert isinstance(result["timings"], dict)
    assert result["timings"]["forward_ms"] == 99


# ── 7. Type coercion: malformed timings string → empty dict ──────────────────

@pytest.mark.anyio
async def test_get_execution_trace_handles_malformed_timings() -> None:
    reader = _make_reader()
    execution_record = {"execution_id": "exec-3", "record_id": "rec-3",
                        "timings": "not-valid-json"}

    async def _query(etid, pipeline):
        if etid == reader._exec_etid:
            return [execution_record]
        return []

    reader._client.query_complex.side_effect = _query
    result = await reader.get_execution_trace("exec-3")
    assert result["timings"] == {}
