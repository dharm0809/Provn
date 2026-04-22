"""Lineage API route handlers: read-only endpoints for audit trail browsing."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

import gateway.util.json_utils as _json

from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# TTL + single-flight cache for analytics endpoints.
#
# The dashboard polls /v1/lineage/metrics and /token-latency every 3s. At N
# open dashboard tabs this fans out to N concurrent identical SQL aggregations
# every 3s — redundant work, since the underlying data doesn't update that
# fast in practice. We memoize the JSON result per (endpoint, range_key) for
# a short TTL, and an asyncio.Lock coalesces concurrent misses so only ONE
# worker hits SQLite even during a stampede.
#
# TTL is deliberately short (below the dashboard's own poll cadence) so users
# still see ~3s freshness on Overview. Cache is invalidated implicitly by TTL
# expiry; no invalidation hooks required.
# ─────────────────────────────────────────────────────────────────────────────
_ANALYTICS_CACHE_TTL_S = 2.5
_analytics_cache: dict[str, tuple[float, Any]] = {}
_analytics_locks: dict[str, asyncio.Lock] = {}


async def _cached_analytics(key: str, compute: Callable[[], Any]) -> Any:
    """Return cached value for *key* or call *compute* once under a per-key lock.

    *compute* is an async zero-arg callable (typically a thin wrapper around
    `_call(reader.method, range_key)`). Concurrent callers during a miss all
    await the same Lock, so the SQL query runs once and the result is shared.
    """
    now = time.monotonic()
    entry = _analytics_cache.get(key)
    if entry and (now - entry[0]) < _ANALYTICS_CACHE_TTL_S:
        return entry[1]
    lock = _analytics_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _analytics_locks[key] = lock
    async with lock:
        # Re-check after acquiring the lock: another coroutine may have
        # populated the cache while we were waiting.
        entry = _analytics_cache.get(key)
        if entry and (time.monotonic() - entry[0]) < _ANALYTICS_CACHE_TTL_S:
            return entry[1]
        value = await compute()
        _analytics_cache[key] = (time.monotonic(), value)
        return value


async def _call(method, *args, **kwargs) -> Any:
    """Call a reader method, awaiting if async (WalacorLineageReader) or calling directly (SQLite)."""
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result

_DEFAULT_SESSION_LIMIT = 50
_MAX_SESSION_LIMIT = 200
_DEFAULT_ATTEMPT_LIMIT = 100
_MAX_ATTEMPT_LIMIT = 500


def _safe_int(value: str | None, default: int) -> int:
    """Parse an integer from a query parameter, returning *default* on bad input."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _sessions_list_params(request: Request) -> tuple[int, int, str | None, str, str]:
    limit = min(_safe_int(request.query_params.get("limit"), _DEFAULT_SESSION_LIMIT), _MAX_SESSION_LIMIT)
    offset = max(0, _safe_int(request.query_params.get("offset"), 0))
    q = request.query_params.get("q")
    if q is not None:
        q = q.strip() or None
    sort = request.query_params.get("sort") or "last_activity"
    if sort not in ("last_activity", "record_count", "model"):
        sort = "last_activity"
    order_raw = request.query_params.get("order") or "desc"
    order = "asc" if str(order_raw).lower() == "asc" else "desc"
    return limit, offset, q, sort, order


_ATTEMPTS_SORT_ALLOW = frozenset({
    "timestamp",
    "disposition",
    "request_id",
    "user",
    "model_id",
    "path",
    "status_code",
})


def _attempts_list_params(request: Request) -> tuple[int, int, str | None, str, str]:
    limit = min(_safe_int(request.query_params.get("limit"), _DEFAULT_ATTEMPT_LIMIT), _MAX_ATTEMPT_LIMIT)
    offset = max(0, _safe_int(request.query_params.get("offset"), 0))
    q = request.query_params.get("q")
    if q is not None:
        q = q.strip() or None
    sort = request.query_params.get("sort") or "timestamp"
    if sort not in _ATTEMPTS_SORT_ALLOW:
        sort = "timestamp"
    order_raw = request.query_params.get("order") or "desc"
    order = "asc" if str(order_raw).lower() == "asc" else "desc"
    return limit, offset, q, sort, order


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
    limit, offset, q, sort, order = _sessions_list_params(request)
    try:
        sessions = await _call(
            reader.list_sessions, limit=limit, offset=offset, search=q, sort=sort, order=order
        )
        total = await _call(reader.count_sessions, q) if hasattr(reader, "count_sessions") else len(sessions)
        return JSONResponse(
            {
                "sessions": sessions,
                "total": total,
                "limit": limit,
                "offset": offset,
                "q": q or "",
                "sort": sort,
                "order": order,
            }
        )
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
        try:
            limit = int(request.query_params.get("limit", 500))
        except (TypeError, ValueError):
            limit = 500
        limit = max(1, min(limit, 5000))
        records = await _call(reader.get_session_timeline, session_id, limit)
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

    # Promote file_metadata from metadata to top level for dashboard
    if not record.get("file_metadata") and meta.get("file_metadata"):
        record["file_metadata"] = meta.pop("file_metadata")

    return record


async def lineage_execution(request: Request) -> JSONResponse:
    """GET /v1/lineage/executions/{execution_id} — full execution record + tool events."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    execution_id = request.path_params["execution_id"]
    try:
        record = await _call(reader.get_execution, execution_id)
        if record is None:
            return JSONResponse({"error": "Execution not found", "execution_id": execution_id}, status_code=404)
        record = _enrich_execution_record(record)
        tool_events = await _call(reader.get_tool_events, execution_id)
        # Keep "record" wrapper for dashboard compat + spread top-level for API consumers
        return JSONResponse({
            "record": record,
            "tool_events": tool_events,
            # Top-level convenience fields (same data, easier access)
            "model_id": record.get("model_id"),
            "content_analysis": record.get("content_analysis"),
            "execution_id": record.get("execution_id"),
        })
    except Exception as e:
        logger.error("lineage_execution error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_attempts(request: Request) -> JSONResponse:
    """GET /v1/lineage/attempts — recent attempt records + disposition stats."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    limit, offset, q, sort, order = _attempts_list_params(request)
    try:
        data = await _call(reader.get_attempts, limit=limit, offset=offset, search=q, sort=sort, order=order)
        # Normalize keys: Walacor reader returns "items" with "model_id",
        # SQLite reader returns "attempts" with "model". Normalize both.
        if "items" in data and "attempts" not in data:
            data["attempts"] = data.pop("items")
        for att in data.get("attempts", []):
            if "model_id" in att and "model" not in att:
                att["model"] = att["model_id"]
        data = {
            **data,
            "limit": limit,
            "offset": offset,
            "q": q or "",
            "sort": sort,
            "order": order,
        }
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
        data = await _cached_analytics(
            f"metrics:{range_key}",
            lambda: _call(reader.get_metrics_history, range_key),
        )
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
        data = await _cached_analytics(
            f"token_latency:{range_key}",
            lambda: _call(reader.get_token_latency_history, range_key),
        )
        return JSONResponse(data)
    except Exception as e:
        logger.error("lineage_token_latency_history error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_metrics_stream(request: Request) -> StreamingResponse:
    """GET /v1/lineage/metrics/stream?range=1h — server-sent events live metrics.

    Additive alternative to polling /v1/lineage/metrics every 3s. Emits the
    same JSON payload as the REST endpoint on a fixed cadence so the dashboard
    can replace N setIntervals with one open EventSource. Unlike polling, all
    connected browsers share a single computation per tick via the same
    _cached_analytics single-flight cache, so fan-out is O(1) regardless of
    viewer count.

    Fail-open: if the reader is unavailable OR the client disconnects, the
    generator ends cleanly and the HTTP response closes. Clients that don't
    support SSE (or for which this endpoint returns 503) fall back to polling
    the REST endpoint — nothing in the existing flow changes.
    """
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    range_key = request.query_params.get("range", "1h")
    # Tick just above the cache TTL so each emission usually hits a fresh
    # value without forcing more DB work than the cached REST endpoint.
    tick_seconds = 3.0

    async def event_source():
        # Retry hint for browser reconnect: if the pipe drops, EventSource
        # will reconnect after this many milliseconds. Matches our tick so
        # the dashboard doesn't hammer on transient network blips.
        yield b"retry: 3000\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    data = await _cached_analytics(
                        f"metrics:{range_key}",
                        lambda: _call(reader.get_metrics_history, range_key),
                    )
                    payload = _json.dumps_bytes(data)
                    yield b"data: " + payload + b"\n\n"
                except Exception:
                    logger.warning("metrics_stream tick failed", exc_info=True)
                    # Emit a comment line to keep the connection alive even if
                    # one tick hits an error — keeps the browser from
                    # reconnecting unnecessarily.
                    yield b": error\n\n"
                await asyncio.sleep(tick_seconds)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Disable buffering in any reverse proxy sitting in front of us
            # (nginx honors this header); without it, chunks are held until
            # the proxy flushes and SSE loses its real-time property.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def lineage_trace(request: Request) -> JSONResponse:
    """GET /v1/lineage/trace/{execution_id} — execution trace with timings for waterfall view."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    execution_id = request.path_params["execution_id"]
    try:
        trace = await _call(reader.get_execution_trace, execution_id)
        if trace is None:
            return JSONResponse({"error": "Execution not found", "execution_id": execution_id}, status_code=404)
        return JSONResponse(trace)
    except Exception as e:
        logger.error("lineage_trace error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def lineage_envelope(request: Request) -> JSONResponse:
    """GET /v1/lineage/envelope/{execution_id} — raw Walacor envelope for a sealed record.

    Returns the unredacted envelope fields (UID, ORGId, SV, EId, …) by calling
    walacor_client.query_complex directly. Also returns a `match` block
    comparing the locally stored anchor fields against what Walacor returned,
    so the dashboard can prove the seal is still valid right now — not just at
    write time.

    Shape:
        {
          "execution_id": "...",
          "envelope": { ...raw getcomplex row, NO deserialization stripping... } | null,
          "local": { "walacor_block_id", "walacor_trans_id", "walacor_dh", "record_hash" },
          "match": {
              "dh":       true|false,
              "block_id": true|false,
              "trans_id": true|false,
              "all_ok":   true|false
          }
        }

    Errors:
        404 — execution_id not in local WAL
        502 — Walacor unreachable or query failed; body includes {fallback: {local...}}
        503 — Walacor storage not configured (walacor_client is None)
    """
    execution_id = request.path_params["execution_id"]
    ctx = get_pipeline_context()

    # Need both the local reader (for local anchor fields) AND the walacor client
    # (for the live envelope). Missing either → graceful degradation.
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)

    # Local view — always available (this is what the session timeline shows).
    try:
        local_exec = await _call(reader.get_execution, execution_id)
    except Exception as e:
        logger.error("lineage_envelope local lookup error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
    if not local_exec:
        return JSONResponse(
            {"error": "Execution not found", "execution_id": execution_id},
            status_code=404,
        )

    local_anchor = {
        "walacor_block_id": local_exec.get("walacor_block_id"),
        "walacor_trans_id": local_exec.get("walacor_trans_id"),
        "walacor_dh": local_exec.get("walacor_dh"),
        "record_hash": local_exec.get("record_hash"),
    }

    # No Walacor client → local-only response.
    if ctx.walacor_client is None:
        return JSONResponse(
            {
                "execution_id": execution_id,
                "envelope": None,
                "local": local_anchor,
                "match": None,
                "warning": "Walacor storage not configured — showing local anchor only",
            },
            status_code=503,
        )

    # Live round-trip. Do NOT run _deserialize_record — we want the envelope
    # identity fields (_id, UID, ORGId, SV) that deserialization strips.
    from gateway.config import get_settings
    settings = get_settings()
    try:
        rows = await ctx.walacor_client.query_complex(
            settings.walacor_executions_etid,
            [{"$match": {"execution_id": execution_id}}, {"$limit": 1}],
        )
    except Exception as e:
        logger.warning("lineage_envelope live fetch failed: %s", e, exc_info=True)
        return JSONResponse(
            {
                "execution_id": execution_id,
                "envelope": None,
                "local": local_anchor,
                "match": None,
                "error": f"Walacor unreachable: {e}",
            },
            status_code=502,
        )

    envelope = rows[0] if rows else None
    if envelope is None:
        return JSONResponse(
            {
                "execution_id": execution_id,
                "envelope": None,
                "local": local_anchor,
                "match": None,
                "error": "Execution not found on Walacor — sealed locally but not yet delivered",
            },
            status_code=200,  # Not an error; the UI renders "seal pending".
        )

    # Compare local anchor fields against remote envelope. Walacor column names
    # use PascalCase (BlockId, TransId, DH) in the envelope but snake_case in
    # the record body — check both just in case the backend surfaces either.
    def _first(d: dict, *keys: str):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return None

    remote_block = _first(envelope, "BlockId", "walacor_block_id", "block_id")
    remote_trans = _first(envelope, "TransId", "walacor_trans_id", "trans_id")
    remote_dh = _first(envelope, "DH", "walacor_dh", "dh")

    match = {
        "block_id": local_anchor["walacor_block_id"] == remote_block
                     if local_anchor["walacor_block_id"] and remote_block else None,
        "trans_id": local_anchor["walacor_trans_id"] == remote_trans
                     if local_anchor["walacor_trans_id"] and remote_trans else None,
        "dh": local_anchor["walacor_dh"] == remote_dh
               if local_anchor["walacor_dh"] and remote_dh else None,
    }
    match["all_ok"] = all(v is True for v in (match["block_id"], match["trans_id"], match["dh"]))

    return JSONResponse({
        "execution_id": execution_id,
        "envelope": envelope,
        "local": local_anchor,
        "match": match,
    })


async def lineage_verify(request: Request) -> JSONResponse:
    """GET /v1/lineage/verify/{session_id} — server-side chain verification."""
    reader = _reader_or_503()
    if reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)
    session_id = request.path_params["session_id"]
    try:
        result = await _call(reader.verify_chain, session_id)
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
    attachments = await _call(ctx.lineage_reader.get_attachments, session_id)
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
        results = await _call(reader.get_ab_test_results, test_name)
        return JSONResponse(results)
    except Exception as exc:
        logger.error("lineage_ab_test_results error: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)
