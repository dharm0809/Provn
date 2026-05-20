"""Unit tests for compliance export API endpoints."""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _clear_compliance_cache():
    """Reset the module-level singleflight cache between tests.

    Without this, the first test populates _REPORT_CACHE for
    (start, end)=(2026-03-01, 2026-03-10), and every subsequent test
    that uses the same window picks up the stale reader's data instead
    of its own mocked one. The cache exists for real prod traffic
    where multiple frameworks share a window; in tests it's noise.
    """
    from gateway.compliance import api as compliance_api
    compliance_api._REPORT_CACHE.clear()
    compliance_api._REPORT_INFLIGHT.clear()
    yield
    compliance_api._REPORT_CACHE.clear()
    compliance_api._REPORT_INFLIGHT.clear()


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


@pytest.mark.anyio
async def test_concurrent_framework_requests_coalesce_to_single_reader_call():
    """Singleflight cache: 4 simultaneous requests with the same window
    should trigger the reader ONCE, not four times.

    The dashboard fires four parallel /v1/compliance/export calls (one
    per framework) on every page load. Pre-fix, each request independently
    awaited the four reader queries serially → 16 sequential Walacor
    queries per page load → 60s+ timeouts on prod. The cache + coalescing
    in api._load_shared_report fixes this; this test pins it.
    """
    import asyncio
    from gateway.compliance.api import compliance_export
    from starlette.requests import Request

    reader = _mock_reader()
    # Counter-wrap each reader method so we can assert it ran exactly
    # once across all four concurrent requests.
    call_counts = {"summary": 0, "executions": 0, "attestations": 0, "chain": 0}

    def _counted(name, payload):
        def _f(*args, **kw):
            call_counts[name] += 1
            return payload
        return _f

    reader.get_compliance_summary = _counted("summary", reader.get_compliance_summary.return_value)
    reader.get_execution_export = _counted("executions", reader.get_execution_export.return_value)
    reader.get_attestation_summary = _counted("attestations", reader.get_attestation_summary.return_value)
    reader.get_chain_verification_report = _counted("chain", reader.get_chain_verification_report.return_value)

    async def _one(framework: str):
        with patch("gateway.compliance.api.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value.lineage_reader = reader
            mock_ctx.return_value.content_analyzers = []
            mock_ctx.return_value.session_chain = None
            mock_ctx.return_value.wal_writer = None
            mock_ctx.return_value.walacor_client = None
            scope = {
                "type": "http", "method": "GET",
                "path": "/v1/compliance/export",
                "query_string": f"format=json&start=2026-03-01&end=2026-03-10&framework={framework}".encode(),
                "headers": [],
            }
            return await compliance_export(Request(scope))

    # Four frameworks in parallel — what the dashboard does.
    responses = await asyncio.gather(
        _one("eu_ai_act"),
        _one("nist"),
        _one("soc2"),
        _one("iso42001"),
    )
    assert all(r.status_code == 200 for r in responses)
    # Each underlying reader query should have run EXACTLY ONCE across
    # all four requests — that's the whole point of the singleflight.
    assert call_counts == {"summary": 1, "executions": 1, "attestations": 1, "chain": 1}, (
        f"expected each reader query to fire once total, got {call_counts}"
    )
