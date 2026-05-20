"""GET /v1/connections — 10-tile live ops snapshot.

Singleflight + 3s TTL cache (mirrors /v1/readiness). Endpoint never
returns 5xx: per-tile fail-open in builder.build_snapshot ensures a
failing probe becomes a grey ``status:"unknown"`` tile rather than a
500.
"""

from __future__ import annotations

import asyncio
import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.connections.builder import build_snapshot

logger = logging.getLogger(__name__)

# 45s TTL outlives the ObservabilityPrecomputeWorker's 30s tick, so a
# snapshot warmed by the worker stays fresh for at least one request
# cycle. Pre-fix the TTL was 3s — the cache expired between every
# precompute tick, so requests routinely paid a full cold-build (~8s
# pre-PR for connection tiles built serially; ~1s after that fix). On
# the dashboard's 3s poll cadence this meant every poll triggered a
# fresh build, defeating the worker entirely.
#
# Bounded-staleness rationale: connections tiles are operational health
# signals — provider error rates, WAL backlog, attestation count. A
# 45s-stale snapshot is fine for an operator dashboard. The
# `snapshot_at` field is surfaced in the response so callers can see
# the actual freshness.
_TTL_S = 45.0
_CACHE: dict = {"snapshot": None, "ts": 0.0}
_LOCK: asyncio.Lock | None = None


async def connections_handler(request: Request) -> JSONResponse:
    from gateway.config import get_settings
    from gateway.pipeline.context import get_pipeline_context

    settings = get_settings()
    if not getattr(settings, "connections_enabled", True):
        return JSONResponse(
            {"error": "connections endpoint disabled"},
            status_code=503,
        )

    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()

    now = time.time()
    cached = _CACHE["snapshot"]
    if cached is not None and now - _CACHE["ts"] < _TTL_S:
        return JSONResponse(cached)

    async with _LOCK:
        cached = _CACHE["snapshot"]
        if cached is not None and time.time() - _CACHE["ts"] < _TTL_S:
            return JSONResponse(cached)
        ctx = get_pipeline_context()
        try:
            snapshot = await build_snapshot(ctx)
        except Exception as exc:
            # build_snapshot already wraps per-tile; this is a last-line safety
            logger.warning("connections: snapshot assembly failed: %s", exc)
            from gateway.util.time import iso8601_utc
            snapshot = {
                "generated_at": iso8601_utc(time.time()),
                "ttl_seconds": int(_TTL_S),
                "overall_status": "red",
                "tiles": [],
                "events": [],
                "error": str(exc),
            }
        _CACHE["snapshot"] = snapshot
        _CACHE["ts"] = time.time()
        return JSONResponse(snapshot)


def _reset_cache_for_tests() -> None:
    _CACHE["snapshot"] = None
    _CACHE["ts"] = 0.0
