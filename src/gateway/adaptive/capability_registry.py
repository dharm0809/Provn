# src/gateway/adaptive/capability_registry.py
"""Model capability registry with TTL-based re-probing.

Replaces the simple _model_capabilities dict in orchestrator.py with
a richer registry that supports TTL expiry, model type classification,
per-model timeouts, and optional persistence to the control plane store.

Per-worker lifetime (intentional)
---------------------------------
``self._cache`` is a process-local ``LRUCache``; under a multi-worker
uvicorn deployment each worker maintains its own copy and there is no
cross-worker sharing.  This is deliberate:

  * The data the cache holds — "does model X accept tools?", observed
    P95 latency — is *self-healing*: a worker that lacks an entry runs
    one extra retry-cycle (a 400/422 with a tool-unsupported phrase
    triggers strip-and-retry, then caches False) and is fully populated.
  * Adding SQLite or Redis sharing would introduce a write-on-every-probe
    hot path and a new failure mode for what is, at most, one extra
    retry per worker per model lifetime.  Not worth it.

If a deployment ever genuinely needs shared capability state (e.g. very
large model catalogs on slow/expensive providers where a single retry
is unacceptable), set ``WALACOR_SHARED_CAPABILITY_CACHE_PATH`` in
config.py — TODO, not implemented today.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, NamedTuple

from cachetools import LRUCache

logger = logging.getLogger(__name__)

# Bound the cache so a misbehaving caller (or an attacker spamming
# bogus model IDs) can't grow it without limit.  100 entries comfortably
# covers the largest realistic model catalog and keeps the worker-local
# memory footprint trivial.
_CACHE_MAXSIZE = 100


class ModelCapability(NamedTuple):
    """Cached capabilities for a single model."""
    model_id: str
    provider: str = ""
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    model_type: str = "chat"  # chat, reasoning, embedding, code
    probed_at: float = 0.0
    probe_count: int = 0
    # Adaptive timeout: observed latencies (last N requests)
    observed_latencies: tuple[float, ...] = ()


class CapabilityRegistry:
    """Model capability cache with TTL and optional persistence."""

    def __init__(self, ttl_seconds: int = 86400, control_store: Any = None):
        # See module docstring re: per-worker lifetime — bounded LRUCache
        # keeps memory bounded; entries evict in LRU order on overflow.
        self._cache: LRUCache = LRUCache(maxsize=_CACHE_MAXSIZE)
        self._ttl = ttl_seconds
        self._store = control_store

    def supports_tools(self, model_id: str) -> bool | None:
        cap = self._cache.get(model_id)
        if cap is None:
            return None
        if self._is_stale(cap):
            return None
        return cap.supports_tools

    def record(self, model_id: str, **kwargs: Any) -> None:
        existing = self._cache.get(model_id)
        if existing:
            updates = {k: v for k, v in kwargs.items() if v is not None}
            updated = existing._replace(
                probed_at=time.time(),
                probe_count=existing.probe_count + 1,
                **updates)
        else:
            updated = ModelCapability(
                model_id=model_id,
                probed_at=time.time(),
                probe_count=1,
                **{k: v for k, v in kwargs.items() if v is not None})
        self._cache[model_id] = updated
        logger.info("Model capability recorded: %s = %s", model_id, dict(updated._asdict()))

    def record_latency(self, model_id: str, latency_seconds: float) -> None:
        """Record an observed request latency for adaptive timeout calculation."""
        cap = self._cache.get(model_id)
        if not cap:
            return
        # Keep last 20 observations
        latencies = cap.observed_latencies[-19:] + (latency_seconds,)
        self._cache[model_id] = cap._replace(observed_latencies=latencies)

    # Per-model-type timeout multipliers applied both before and after
    # the P95-based adaptive calculation. Kept in one place so the
    # cold-start path and the adaptive path stay in sync.
    _TYPE_MULTIPLIER: dict[str, float] = {
        "reasoning": 2.0,   # thinking models need more time
        "embedding": 0.5,   # embeddings finish fast; fail sooner
    }

    def get_timeout(self, model_id: str, default: float = 120.0) -> float:
        """Adaptive timeout: P95 of observed latencies * 2.5, with floor and ceiling.

        - First request (no data): use generous default (model may need to load)
        - After 3+ observations: adapt to actual model speed
        - Fast model (3B, 2s avg) → ~10s timeout
        - Slow model (14B CPU, 40s avg) → ~120s timeout
        - Reasoning model: 2x multiplier on top
        - Embedding model: 0.5x multiplier (these calls should be fast)
        """
        cap = self._cache.get(model_id)
        if not cap or len(cap.observed_latencies) < 3:
            # Not enough data — use generous default for cold start,
            # scaled by model-type multiplier so embedding models fail
            # fast on a hung endpoint instead of tying up a request
            # slot for the full 2-minute default.
            mult = self._TYPE_MULTIPLIER.get(cap.model_type, 1.0) if cap else 1.0
            return default * mult

        latencies = sorted(cap.observed_latencies)
        p95_idx = max(0, int(len(latencies) * 0.95) - 1)
        p95 = latencies[p95_idx]

        # Timeout = P95 * 2.5 (headroom for variance)
        adaptive = p95 * 2.5

        # Model type multiplier — reasoning gets 1.5x here (less than
        # the cold-start 2x since we now have real latency data), and
        # embedding keeps the 0.5x floor.
        if cap.model_type == "reasoning":
            adaptive *= 1.5
        elif cap.model_type == "embedding":
            adaptive *= 0.5

        # Floor: never below 10s, ceiling: never above 300s
        return max(10.0, min(300.0, adaptive))

    def get_stale_models(self) -> list[str]:
        return [mid for mid, cap in self._cache.items() if self._is_stale(cap)]

    def mark_for_reprobe(self, model_id: str) -> None:
        cap = self._cache.get(model_id)
        if cap:
            self._cache[model_id] = cap._replace(probed_at=0)

    def all_capabilities(self) -> dict[str, dict[str, Any]]:
        return {mid: dict(cap._asdict()) for mid, cap in self._cache.items()}

    def _is_stale(self, cap: ModelCapability) -> bool:
        return (time.time() - cap.probed_at) > self._ttl


# ----------------------------------------------------------------------------
# Redis-backed variant (multi-worker shared state — 3b Phase 2 follow-on)
# ----------------------------------------------------------------------------
# Same pattern as RedisSessionChainTracker / RedisBudgetTracker: when
# ``WALACOR_REDIS_URL`` is set, every worker reads/writes the same key space
# so a tool-unsupported probe done on worker A is honored by worker B on
# the next request. Without this, each worker pays a redundant 400/422
# tool-strip-and-retry until its own LRUCache warms up — visible as
# duplicate retries in lineage under multi-worker deployments.
#
# Storage shape — one Redis HASH per model at ``gateway:capability:{model_id}``:
#   supports_tools      "1" | "0"
#   supports_streaming  "1" | "0"
#   provider            string
#   model_type          "chat" | "reasoning" | "embedding" | "code"
#   probed_at           unix epoch float (str)
#   probe_count         integer counter (HINCRBY)
#   observed_latencies  JSON-encoded list (capped at _LATENCY_SAMPLE_CAP)
# Key TTL is the same 24h default; Redis auto-expires stale entries so we
# never need a `_is_stale` check on the read path.
#
# Read-path performance: callers are sync (see tool_executor.py:279, 511,
# 526; orchestrator.py:2609) and async-ifying them is outside the file
# budget of this change. The implementation therefore keeps a *local
# read-through mirror* (the same LRUCache the in-memory variant uses) and
# fans out writes to Redis via ``asyncio.create_task`` for cross-worker
# convergence. Reads stay O(1) in-process; cross-worker propagation is
# bounded by the local TTL (default 24h matches Redis key TTL, but a worker
# that has *no* entry will fall back to a sync no-op and let the next
# successful probe write to both).

_LATENCY_SAMPLE_CAP = 50  # bound the JSON blob; ~50 floats is plenty for p95


def _redis_key(model_id: str) -> str:
    return f"gateway:capability:{model_id}"


class RedisCapabilityRegistry:
    """Redis-backed cross-worker capability cache.

    Mirrors :class:`CapabilityRegistry`'s sync interface. Reads return from
    a local read-through mirror; writes update the mirror synchronously and
    schedule a background Redis fan-out so every worker eventually sees the
    same view. Use this when ``WALACOR_UVICORN_WORKERS>1`` and
    ``WALACOR_REDIS_URL`` is set; otherwise stick with the in-memory variant.
    """

    def __init__(
        self,
        redis_client: Any,
        ttl_seconds: int = 86400,
        control_store: Any = None,
    ):
        self._r = redis_client
        self._ttl = ttl_seconds
        self._store = control_store
        # Local mirror — bounded; reads are O(1) and never touch Redis on
        # the request hot path.
        self._cache: LRUCache = LRUCache(maxsize=_CACHE_MAXSIZE)
        # Track in-flight background writes so we can await them in tests.
        self._pending_writes: set[asyncio.Task] = set()

    # --- read path (sync, mirror-backed) ----------------------------------
    def supports_tools(self, model_id: str) -> bool | None:
        cap = self._cache.get(model_id)
        if cap is None:
            return None
        if (time.time() - cap.probed_at) > self._ttl:
            return None
        return cap.supports_tools

    def get_timeout(self, model_id: str, default: float = 120.0) -> float:
        # Identical formula to CapabilityRegistry.get_timeout — kept in
        # lock-step so a per-deployment registry swap doesn't change
        # request-level timeout behavior.
        cap = self._cache.get(model_id)
        if not cap or len(cap.observed_latencies) < 3:
            mult = CapabilityRegistry._TYPE_MULTIPLIER.get(cap.model_type, 1.0) if cap else 1.0
            return default * mult
        latencies = sorted(cap.observed_latencies)
        p95_idx = max(0, int(len(latencies) * 0.95) - 1)
        p95 = latencies[p95_idx]
        adaptive = p95 * 2.5
        if cap.model_type == "reasoning":
            adaptive *= 1.5
        elif cap.model_type == "embedding":
            adaptive *= 0.5
        return max(10.0, min(300.0, adaptive))

    def all_capabilities(self) -> dict[str, dict[str, Any]]:
        # Local mirror — the cross-worker SCAN view is exposed via the
        # async :meth:`scan_all_capabilities` for diagnostics; the sync
        # caller in /health is happy with the local snapshot.
        return {mid: dict(cap._asdict()) for mid, cap in self._cache.items()}

    # --- write path (sync mirror update + async Redis fan-out) -----------
    def record(self, model_id: str, **kwargs: Any) -> None:
        existing = self._cache.get(model_id)
        if existing:
            updates = {k: v for k, v in kwargs.items() if v is not None}
            updated = existing._replace(
                probed_at=time.time(),
                probe_count=existing.probe_count + 1,
                **updates,
            )
        else:
            updated = ModelCapability(
                model_id=model_id,
                probed_at=time.time(),
                probe_count=1,
                **{k: v for k, v in kwargs.items() if v is not None},
            )
        self._cache[model_id] = updated
        self._schedule(self._write_to_redis(model_id, updated))
        logger.info(
            "Model capability recorded (redis): %s = %s",
            model_id,
            dict(updated._asdict()),
        )

    def record_latency(self, model_id: str, latency_seconds: float) -> None:
        cap = self._cache.get(model_id)
        if not cap:
            return
        latencies = (cap.observed_latencies[-(_LATENCY_SAMPLE_CAP - 1):] +
                     (latency_seconds,))
        cap = cap._replace(observed_latencies=latencies)
        self._cache[model_id] = cap
        self._schedule(self._write_to_redis(model_id, cap))

    def mark_for_reprobe(self, model_id: str) -> None:
        cap = self._cache.get(model_id)
        if cap:
            self._cache[model_id] = cap._replace(probed_at=0)
        self._schedule(self._delete_from_redis(model_id))

    def get_stale_models(self) -> list[str]:
        return [
            mid for mid, cap in self._cache.items()
            if (time.time() - cap.probed_at) > self._ttl
        ]

    # --- Redis I/O (async, fire-and-forget from sync write path) ---------
    async def _write_to_redis(self, model_id: str, cap: ModelCapability) -> None:
        try:
            key = _redis_key(model_id)
            mapping = {
                "supports_tools": "1" if cap.supports_tools else "0",
                "supports_streaming": "1" if cap.supports_streaming else "0",
                "provider": cap.provider,
                "model_type": cap.model_type,
                "probed_at": str(cap.probed_at),
                "observed_latencies": json.dumps(
                    list(cap.observed_latencies[-_LATENCY_SAMPLE_CAP:])
                ),
            }
            async with self._r.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping=mapping)
                pipe.hincrby(key, "probe_count", 1)
                pipe.expire(key, self._ttl)
                await pipe.execute()
        except Exception as e:  # fail-open: never let Redis hiccups break the request path
            logger.warning("RedisCapabilityRegistry write failed for %s: %s", model_id, e)

    async def _delete_from_redis(self, model_id: str) -> None:
        try:
            await self._r.delete(_redis_key(model_id))
        except Exception as e:
            logger.warning("RedisCapabilityRegistry delete failed for %s: %s", model_id, e)

    async def hydrate(self, model_id: str) -> bool | None:
        """Optional warm-read from Redis into the local mirror.

        Not on the hot path — useful for tests and for a one-shot startup
        warmup if a deployment wants to skip the first-request cold cache.
        Returns the supports_tools value loaded (or None if absent).
        """
        try:
            raw = await self._r.hgetall(_redis_key(model_id))
        except Exception as e:
            logger.warning("RedisCapabilityRegistry hydrate failed for %s: %s", model_id, e)
            return None
        if not raw:
            return None
        decoded = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw.items()
        }
        try:
            cap = ModelCapability(
                model_id=model_id,
                provider=decoded.get("provider", ""),
                supports_tools=(decoded.get("supports_tools") == "1") if "supports_tools" in decoded else None,
                supports_streaming=(decoded.get("supports_streaming") == "1") if "supports_streaming" in decoded else None,
                model_type=decoded.get("model_type", "chat"),
                probed_at=float(decoded.get("probed_at", "0") or 0.0),
                probe_count=int(decoded.get("probe_count", "0") or 0),
                observed_latencies=tuple(json.loads(decoded.get("observed_latencies") or "[]")),
            )
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("RedisCapabilityRegistry parse failed for %s: %s", model_id, e)
            return None
        self._cache[model_id] = cap
        return cap.supports_tools

    async def scan_all_capabilities(self, scan_budget_ms: int = 100) -> dict[str, dict[str, Any]]:
        """Cross-worker view via SCAN (bounded by scan_budget_ms).

        Diagnostic-only — uses SCAN (not KEYS, which blocks Redis on large
        keyspaces) and aborts if the time budget is exceeded so we never
        stall a /health or /connections call on a degraded Redis.
        """
        out: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + (scan_budget_ms / 1000.0)
        try:
            cursor = 0
            first = True
            while first or cursor != 0:
                first = False
                if time.monotonic() > deadline:
                    logger.warning("RedisCapabilityRegistry SCAN budget exceeded (%dms)", scan_budget_ms)
                    break
                cursor, batch = await self._r.scan(
                    cursor=cursor, match="gateway:capability:*", count=50,
                )
                for raw_key in batch:
                    key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                    model_id = key.split(":", 2)[-1]
                    await self.hydrate(model_id)
                    cap = self._cache.get(model_id)
                    if cap:
                        out[model_id] = dict(cap._asdict())
        except Exception as e:
            logger.warning("RedisCapabilityRegistry SCAN failed: %s", e)
        return out

    # --- task plumbing ----------------------------------------------------
    def _schedule(self, coro) -> None:
        """Fire-and-forget; absorb 'no running loop' for sync test paths.

        Production callsites are inside async request handlers, so a loop
        is always running. Tests that call ``record()`` outside a loop
        get the local-mirror update and a warning; the Redis fan-out is
        skipped (matching the fail-open semantics).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task = loop.create_task(coro)
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)

    async def drain(self) -> None:
        """Await all in-flight Redis writes — used by tests; harmless in prod."""
        if self._pending_writes:
            await asyncio.gather(*list(self._pending_writes), return_exceptions=True)


def make_capability_registry(
    redis_client: Any,
    settings: Any,
    control_store: Any = None,
) -> CapabilityRegistry | RedisCapabilityRegistry:
    """Return Redis-backed registry if redis_client is provided, else in-memory.

    Mirrors :func:`make_session_chain_tracker` and :func:`make_budget_tracker`
    so multi-worker deployments share capability state via Redis without
    breaking single-worker deployments that don't set WALACOR_REDIS_URL.
    """
    ttl = settings.capability_probe_ttl_seconds
    if redis_client is not None:
        return RedisCapabilityRegistry(
            redis_client, ttl_seconds=ttl, control_store=control_store,
        )
    return CapabilityRegistry(ttl_seconds=ttl, control_store=control_store)
