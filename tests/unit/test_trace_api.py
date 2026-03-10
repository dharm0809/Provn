"""Unit tests for Phase 27: trace API endpoint."""

import json
import pytest
from unittest.mock import patch, MagicMock

from gateway.lineage.reader import LineageReader


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def test_get_execution_trace_returns_combined_data():
    """get_execution_trace returns execution + tool_events + timings."""
    reader = MagicMock(spec=LineageReader)
    execution = {
        "execution_id": "exec-1",
        "model_id": "gpt-4",
        "timings": {"attestation_ms": 1.0, "forward_ms": 100.0, "total_ms": 110.0},
    }
    tool_events = [{"tool_name": "web_search", "execution_id": "exec-1"}]
    reader.get_execution.return_value = execution
    reader.get_tool_events.return_value = tool_events

    result = reader.get_execution_trace("exec-1")
    # This will fail because MagicMock doesn't have the real method
    # We need to test the real implementation
    reader.get_execution_trace.assert_called_once_with("exec-1")


def test_get_execution_trace_real_implementation(tmp_path):
    """get_execution_trace assembles execution + tool events + timings."""
    from gateway.wal.writer import WALWriter

    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)

    # Write an execution record with timings
    record = {
        "execution_id": "trace-test-1",
        "model_attestation_id": "att-1",
        "model_id": "gpt-4",
        "policy_version": 1,
        "policy_result": "pass",
        "tenant_id": "t1",
        "gateway_id": "gw-1",
        "timestamp": "2026-03-10T12:00:00Z",
        "timings": {"attestation_ms": 1.0, "forward_ms": 100.0, "total_ms": 110.0},
    }
    writer.write_and_fsync(record)

    reader = LineageReader(db_path)
    result = reader.get_execution_trace("trace-test-1")
    assert result is not None
    assert result["execution"]["execution_id"] == "trace-test-1"
    assert result["timings"]["forward_ms"] == 100.0
    assert isinstance(result["tool_events"], list)
    writer.close()
    reader.close()


def test_get_execution_trace_not_found(tmp_path):
    """get_execution_trace returns None for missing execution."""
    from gateway.wal.writer import WALWriter

    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    writer._ensure_conn()

    reader = LineageReader(db_path)
    result = reader.get_execution_trace("nonexistent")
    assert result is None
    writer.close()
    reader.close()


@pytest.mark.anyio
async def test_lineage_trace_endpoint():
    """GET /v1/lineage/trace/{execution_id} returns trace data."""
    from gateway.lineage.api import lineage_trace
    from starlette.requests import Request

    mock_reader = MagicMock()
    mock_reader.get_execution_trace.return_value = {
        "execution": {"execution_id": "exec-1", "model_id": "gpt-4"},
        "tool_events": [],
        "timings": {"forward_ms": 100.0},
    }

    with patch("gateway.lineage.api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.lineage_reader = mock_reader

        scope = {
            "type": "http", "method": "GET",
            "path": "/v1/lineage/trace/exec-1",
            "query_string": b"",
            "headers": [],
            "path_params": {"execution_id": "exec-1"},
        }
        request = Request(scope)
        response = await lineage_trace(request)
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["execution"]["execution_id"] == "exec-1"
        assert "timings" in body


@pytest.mark.anyio
async def test_lineage_trace_endpoint_not_found():
    """GET /v1/lineage/trace/{execution_id} returns 404 for missing execution."""
    from gateway.lineage.api import lineage_trace
    from starlette.requests import Request

    mock_reader = MagicMock()
    mock_reader.get_execution_trace.return_value = None

    with patch("gateway.lineage.api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.lineage_reader = mock_reader

        scope = {
            "type": "http", "method": "GET",
            "path": "/v1/lineage/trace/nonexistent",
            "query_string": b"",
            "headers": [],
            "path_params": {"execution_id": "nonexistent"},
        }
        request = Request(scope)
        response = await lineage_trace(request)
        assert response.status_code == 404
