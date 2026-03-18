"""Lineage API route handlers: read-only endpoints for audit trail browsing."""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)

_DEFAULT_SESSION_LIMIT = "50"
_MAX_SESSION_LIMIT = 200
_DEFAULT_ATTEMPT_LIMIT = "100"
_MAX_ATTEMPT_LIMIT = 500


def _reader_or_503():
    """Return lineage reader or raise 503 JSONResponse."""
    ctx = get_pipeline_context()
    if ctx.lineage_reader is None:
        return None
    return ctx.lineage_reader


async def lineage_sessions(request: Request) -> JSONResponse:
    """GET /v1/lineage/sessions — list sessions with record count and last activity."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    limit = min(int(request.query_params.get("limit", _DEFAULT_SESSION_LIMIT)), _MAX_SESSION_LIMIT)
    offset = int(request.query_params.get("offset", "0"))
    try:
        sessions = reader.list_sessions(limit=limit, offset=offset)
        return JSONResponse({"sessions": sessions, "limit": limit, "offset": offset})
    except Exception as e:
        logger.error("lineage_sessions error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_session_timeline(request: Request) -> JSONResponse:
    """GET /v1/lineage/sessions/{session_id} — timeline of executions for one session."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    session_id = request.path_params["session_id"]
    try:
        records = reader.get_session_timeline(session_id)
        if not records:
            return JSONResponse({"error": "Session not found", "session_id": session_id}, status_code=404)
        records = [_enrich_execution_record(r) for r in records]
        return JSONResponse({"session_id": session_id, "records": records, "count": len(records)})
    except Exception as e:
        logger.error("lineage_session_timeline error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


def _enrich_execution_record(record: dict) -> dict:
    """Enrich execution record with derived fields for API consumers.

    - model_id: fallback from model_attestation_id if missing
    - content_analysis: promote from metadata.analyzer_decisions to top level
    """
    # model_id fallback: extract from "self-attested:model_name" format
    if not record.get("model_id"):
        att_id = record.get("model_attestation_id", "")
        if isinstance(att_id, str) and att_id.startswith("self-attested:"):
            record["model_id"] = att_id[len("self-attested:"):]

    # Promote content analysis from metadata to top level for easy access
    meta = record.get("metadata") or {}
    if not record.get("content_analysis") and meta.get("analyzer_decisions"):
        record["content_analysis"] = meta["analyzer_decisions"]

    return record


async def lineage_execution(request: Request) -> JSONResponse:
    """GET /v1/lineage/executions/{execution_id} — full execution record + tool events."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    execution_id = request.path_params["execution_id"]
    try:
        record = reader.get_execution(execution_id)
        if record is None:
            return JSONResponse({"error": "Execution not found", "execution_id": execution_id}, status_code=404)
        record = _enrich_execution_record(record)
        tool_events = reader.get_tool_events(execution_id)
        # Top-level convenience fields for API consumers
        return JSONResponse({
            **record,
            "tool_events": tool_events,
        })
    except Exception as e:
        logger.error("lineage_execution error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_attempts(request: Request) -> JSONResponse:
    """GET /v1/lineage/attempts — recent attempt records + disposition stats."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    limit = min(int(request.query_params.get("limit", _DEFAULT_ATTEMPT_LIMIT)), _MAX_ATTEMPT_LIMIT)
    offset = int(request.query_params.get("offset", "0"))
    try:
        data = reader.get_attempts(limit=limit, offset=offset)
        return JSONResponse(data)
    except Exception as e:
        logger.error("lineage_attempts error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_metrics_history(request: Request) -> JSONResponse:
    """GET /v1/lineage/metrics?range=1h|24h|7d|30d — time-bucketed attempt metrics for charting."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    range_key = request.query_params.get("range", "1h")
    try:
        data = reader.get_metrics_history(range_key)
        return JSONResponse(data)
    except Exception as e:
        logger.error("lineage_metrics_history error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_token_latency_history(request: Request) -> JSONResponse:
    """GET /v1/lineage/token-latency?range=1h|24h|7d|30d — time-bucketed token + latency metrics."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    range_key = request.query_params.get("range", "1h")
    try:
        data = reader.get_token_latency_history(range_key)
        return JSONResponse(data)
    except Exception as e:
        logger.error("lineage_token_latency_history error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_trace(request: Request) -> JSONResponse:
    """GET /v1/lineage/trace/{execution_id} — execution trace with timings for waterfall view."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    execution_id = request.path_params["execution_id"]
    try:
        trace = reader.get_execution_trace(execution_id)
        if trace is None:
            return JSONResponse({"error": "Execution not found", "execution_id": execution_id}, status_code=404)
        return JSONResponse(trace)
    except Exception as e:
        logger.error("lineage_trace error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_verify(request: Request) -> JSONResponse:
    """GET /v1/lineage/verify/{session_id} — server-side chain verification."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    session_id = request.path_params["session_id"]
    try:
        result = reader.verify_chain(session_id)
        return JSONResponse(result)
    except Exception as e:
        logger.error("lineage_verify error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_attachments(request: Request) -> JSONResponse:
    """GET /v1/lineage/attachments?session_id=X — file metadata for a session."""
    session_id = request.query_params.get("session_id", "")
    if not session_id:
        return JSONResponse({"error": "session_id query parameter required"}, status_code=400)
    ctx = get_pipeline_context()
    if not ctx.lineage_reader:
        return JSONResponse({"error": "Lineage not enabled"}, status_code=503)
    attachments = ctx.lineage_reader.get_attachments(session_id)
    return JSONResponse({"session_id": session_id, "attachments": attachments})


async def lineage_ab_test_results(request: Request) -> JSONResponse:
    """GET /v1/lineage/ab-tests/{test_name}/results — compare A/B variant stats.

    Returns per-variant request counts, average latency (ms), and total/average
    token usage for all executions tagged with the given A/B test name.
    """
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    test_name = request.path_params.get("test_name", "")
    if not test_name:
        return JSONResponse({"error": "test_name path parameter required"}, status_code=400)
    try:
        results = reader.get_ab_test_results(test_name)
        return JSONResponse(results)
    except Exception as exc:
        logger.error("lineage_ab_test_results error: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)
