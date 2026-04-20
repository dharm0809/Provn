"""Unit tests for compliance export API endpoints."""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _mock_reader():
    """Create a mock LineageReader with compliance methods."""
    reader = MagicMock()
    reader.get_compliance_summary.return_value = {
        "total_requests": 100,
        "allowed": 90,
        "denied": 10,
        "models_used": ["qwen3:4b", "gpt-4o"],
    }
    reader.get_execution_export.return_value = [
        {
            "execution_id": "exec-1",
            "timestamp": "2026-03-05T10:00:00+00:00",
            "session_id": "sess-1",
            "model_id": "qwen3:4b",
            "provider": "ollama",
            "model_attestation_id": "self-attested:qwen3:4b",
            "policy_result": "pass",
            "latency_ms": 200.0,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "sequence_number": 0,
            "record_id": "0191a1b2-c3d4-7ef5-a6b7-c8d9e0f1a2b3",
            "previous_record_id": None,
        },
    ]
    reader.get_attestation_summary.return_value = [
        {"model_id": "qwen3:4b", "provider": "ollama", "attestation_id": "self-attested:qwen3:4b",
         "request_count": 90, "total_tokens": 13500},
    ]
    reader.get_chain_verification_report.return_value = [
        {"session_id": "sess-1", "valid": True, "record_count": 3, "errors": []},
    ]
    return reader


@pytest.mark.anyio
async def test_json_export_returns_valid_structure():
    """JSON export returns the expected structure."""
    from gateway.compliance.api import compliance_export
    from starlette.requests import Request

    mock_reader = _mock_reader()

    with patch("gateway.compliance.api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.lineage_reader = mock_reader

        scope = {
            "type": "http", "method": "GET",
            "path": "/v1/compliance/export",
            "query_string": b"format=json&start=2026-03-01&end=2026-03-10",
            "headers": [],
        }
        request = Request(scope)
        response = await compliance_export(request)

    body = json.loads(response.body)
    assert "report" in body
    assert body["report"]["period"]["start"] == "2026-03-01"
    assert body["report"]["period"]["end"] == "2026-03-10"
    assert "summary" in body
    assert body["summary"]["total_requests"] == 100
    assert "attestations" in body
    assert "executions" in body


@pytest.mark.anyio
async def test_csv_export_has_correct_headers():
    """CSV export produces correct column headers."""
    from gateway.compliance.api import compliance_export
    from starlette.requests import Request

    mock_reader = _mock_reader()

    with patch("gateway.compliance.api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.lineage_reader = mock_reader

        scope = {
            "type": "http", "method": "GET",
            "path": "/v1/compliance/export",
            "query_string": b"format=csv&start=2026-03-01&end=2026-03-10",
            "headers": [],
        }
        request = Request(scope)
        response = await compliance_export(request)

    # StreamingResponse — collect body
    body_parts = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            body_parts.append(chunk.decode())
        else:
            body_parts.append(chunk)
    body = "".join(body_parts)
    lines = body.strip().split("\n")
    assert len(lines) >= 2  # header + at least 1 data row
    header = lines[0]
    assert "execution_id" in header
    assert "timestamp" in header
    assert "model_id" in header
    assert "policy_result" in header


@pytest.mark.anyio
async def test_export_requires_start_and_end_params():
    """Export returns 400 when start/end params are missing."""
    from gateway.compliance.api import compliance_export
    from starlette.requests import Request

    with patch("gateway.compliance.api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.lineage_reader = MagicMock()

        scope = {
            "type": "http", "method": "GET",
            "path": "/v1/compliance/export",
            "query_string": b"format=json",
            "headers": [],
        }
        request = Request(scope)
        response = await compliance_export(request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert "error" in body


@pytest.mark.anyio
async def test_export_no_lineage_reader_returns_503():
    """Returns 503 when lineage reader is not available."""
    from gateway.compliance.api import compliance_export
    from starlette.requests import Request

    with patch("gateway.compliance.api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.lineage_reader = None

        scope = {
            "type": "http", "method": "GET",
            "path": "/v1/compliance/export",
            "query_string": b"format=json&start=2026-03-01&end=2026-03-10",
            "headers": [],
        }
        request = Request(scope)
        response = await compliance_export(request)

    assert response.status_code == 503
