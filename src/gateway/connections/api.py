"""GET /v1/connections — 10-tile live ops snapshot.

Singleflight + 45s TTL cache. Endpoint never returns 5xx: per-tile
fail-open in builder.build_snapshot ensures a failing probe becomes a
grey ``status:"unknown"`` tile rather than a 500.

Caching tiers
-------------
1. **Redis (preferred, multi-worker safe)** — when ``ctx.redis_client``
   is set, the snapshot is stored under
   ``walacor:connections:snapshot`` with a 45s TTL. All uvicorn workers
   read/write the same key, so the ObservabilityPrecomputeWorker
   (which runs only in the fcntl-elected leader) warms the cache for
   every worker, not just itself. Pre-fix: each worker had its own
   in-process dict, so 3/4 requests hit a cold build under prod's 4
   workers behind SO_REUSEPORT.
2. **In-process dict (fallback)** — single-worker deployments and
   tests without Redis keep the previous behaviour.

The 45s TTL outlives the precompute worker's 30s tick so a snapshot it
warmed stays fresh across at least one tick. ``snapshot_at`` is
surfaced in the response so callers can see actual freshness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.connections.builder import build_snapshot

logger = logging.getLogger(__name__)

_TTL_S = 45.0
_REDIS_KEY = "walacor:connections:snapshot"

# In-process fallback (used only when ctx.redis_client is None).
_CACHE: dict = {"snapshot": None, "ts": 0.0}
_LOCK: asyncio.Lock | None = None


async def _redis_get(redis_client) -> dict | None:
    try:
        raw = await redis_client.get(_REDIS_KEY)
    except Exception as exc:  # network blip, fail open to rebuild
        logger.debug("connections: redis GET failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        logger.warning("connections: redis snapshot was unparseable, rebuilding: %s", exc)
        return None


async def _redis_set(redis_client, snapshot: dict) -> None:
    try:
        await redis_client.set(_REDIS_KEY, json.dumps(snapshot), ex=int(_TTL_S))
    except Exception as exc:
        logger.debug("connections: redis SET failed: %s", exc)


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

    ctx = get_pipeline_context()
    redis_client = getattr(ctx, "redis_client", None)

    # ── Path A: Redis-backed shared cache ────────────────────────────
    if redis_client is not None:
        cached = await _redis_get(redis_client)
        if cached is not None:
            return JSONResponse(cached)
        # Per-worker singleflight: prevents the same worker from
        # double-building. Cross-worker dogpile is bounded to N workers
        # × one build, which is the same as without Redis.
        async with _LOCK:
            cached = await _redis_get(redis_client)
            if cached is not None:
                return JSONResponse(cached)
            snapshot = await _safe_build(ctx)
            await _redis_set(redis_client, snapshot)
            return JSONResponse(snapshot)

    # ── Path B: in-process fallback (single worker / tests) ──────────

    now = time.time()
    cached = _CACHE["snapshot"]
    if cached is not None and now - _CACHE["ts"] < _TTL_S:
        return JSONResponse(cached)

    async with _LOCK:
        cached = _CACHE["snapshot"]
        if cached is not None and time.time() - _CACHE["ts"] < _TTL_S:
            return JSONResponse(cached)
        snapshot = await _safe_build(ctx)
        _CACHE["snapshot"] = snapshot
        _CACHE["ts"] = time.time()
        return JSONResponse(snapshot)


async def _safe_build(ctx) -> dict:
    try:
        return await build_snapshot(ctx)
    except Exception as exc:
        # build_snapshot already wraps per-tile; last-line safety.
        logger.warning("connections: snapshot assembly failed: %s", exc)
        from gateway.util.time import iso8601_utc
        return {
            "generated_at": iso8601_utc(time.time()),
            "ttl_seconds": int(_TTL_S),
            "overall_status": "red",
            "tiles": [],
            "events": [],
            "error": str(exc),
        }


def _reset_cache_for_tests() -> None:
    _CACHE["snapshot"] = None
    _CACHE["ts"] = 0.0
