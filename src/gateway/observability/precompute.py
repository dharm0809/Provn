"""Background pre-computation worker for /v1/readiness and /v1/connections.

Why
---
Both endpoints already have a request-side TTL cache:

  - Readiness: 15s (``readiness.runner._READINESS_TTL_S``). A request that
    arrives more than 15s after the previous compute pays the full ~37-check
    fan-out (each bounded by 5s) — historically 200-600 ms even on a healthy
    box, occasionally seconds when an HTTP probe (Walacor, Ollama) is slow.
  - Connections: 3s (``connections.api._TTL_S``). 10 tile builders, several
    of which call the lineage reader. Cold compute is ~50-300 ms.

Operators jumping between dashboard tabs hit those cold-load latencies as
soon as their tab has been idle long enough for the cache to expire. This
worker ticks every 30 s and refreshes both caches so dashboard navigations
always land on a warm cache (<5 ms read).

Pattern
-------
Modelled after ``compliance.precompute.CompliancePrecomputeWorker``. Same
lifecycle (``start()`` / ``await stop()``), same fail-open contract (a
single tick failure logs a warning and continues; the run loop never
raises), same idempotency.

Cache writes
------------
Rather than re-implementing the caches, we drive the existing ones:

  - Readiness: call ``run_all(ctx, fresh=True)``. The function writes its
    result into ``readiness.runner._cache`` as a side-effect; subsequent
    request handlers see ``cache_age_s < 15s`` and serve from there.
  - Connections: call ``build_snapshot(ctx)`` and assign the result into
    ``connections.api._CACHE``. The handler's TTL check then short-circuits.

Tick interval is 30 s — comfortably under the 15-s readiness TTL with a
2x safety margin against tick drift. Connections has a 3-s TTL so its
cache will go cold *between* ticks, but the worst-case cold-fetch the user
ever sees is one ~30 s old, which is still better than a true cold compute
because subsequent requests inside the 3-s window are free.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


_TICK_INTERVAL_S = 30.0


class ObservabilityPrecomputeWorker:
    """Periodic warmer for the readiness and connections caches.

    Stateless except for the background ``Task`` and last-tick metadata.
    The warmed values live in the existing module-level caches owned by
    ``readiness.runner`` and ``connections.api``.
    """

    def __init__(self, ctx: Any, *, tick_interval_s: float = _TICK_INTERVAL_S) -> None:
        self._ctx = ctx
        self._tick_interval = max(5.0, float(tick_interval_s))
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._last_tick_ok = False
        self._last_tick_at: datetime | None = None
        self._last_error: str | None = None
        self._tick_count = 0

    def start(self) -> asyncio.Task:
        """Schedule the run loop on the current event loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="observability-precompute")
        return self._task

    async def stop(self) -> None:
        """Signal stop and await task drain. Safe to call multiple times."""
        self._stopping = True
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        # Tick once immediately so the cache is warm before the first
        # dashboard navigation after boot.
        await self._tick_once()
        while not self._stopping:
            try:
                await asyncio.sleep(self._tick_interval)
            except asyncio.CancelledError:
                break
            if self._stopping:
                break
            await self._tick_once()

    async def _tick_once(self) -> None:
        """Refresh the readiness + connections caches concurrently.

        Both refreshes are independent; a failure in one does not skip the
        other. Per-call exceptions are caught here so the run loop never
        dies on a transient subsystem error.
        """
        results = await asyncio.gather(
            self._refresh_readiness(),
            self._refresh_connections(),
            return_exceptions=True,
        )
        any_ok = False
        first_err: str | None = None
        for r in results:
            if isinstance(r, BaseException):
                if first_err is None:
                    first_err = f"{type(r).__name__}: {r}"
            elif r is True:
                any_ok = True
            elif r is False and first_err is None:
                first_err = "refresh returned False"
        self._last_tick_ok = any_ok
        self._last_tick_at = datetime.now(timezone.utc)
        self._last_error = first_err
        self._tick_count += 1

    async def _refresh_readiness(self) -> bool:
        try:
            from gateway.readiness.runner import run_all
            await run_all(self._ctx, fresh=True)
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open per module docstring
            logger.warning("Observability precompute (readiness) failed: %s", exc)
            return False

    async def _refresh_connections(self) -> bool:
        try:
            from gateway.connections import api as conn_api
            from gateway.connections.builder import build_snapshot
            snapshot = await build_snapshot(self._ctx)
            conn_api._CACHE["snapshot"] = snapshot
            conn_api._CACHE["ts"] = time.time()
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open per module docstring
            logger.warning("Observability precompute (connections) failed: %s", exc)
            return False

    # Health surface for ops debugging / connections tile.
    @property
    def health(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "last_tick_ok": self._last_tick_ok,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "last_error": self._last_error,
            "tick_interval_s": self._tick_interval,
            "tick_count": self._tick_count,
        }
