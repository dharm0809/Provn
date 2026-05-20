"""Background pre-computation worker for compliance reports.

The Compliance dashboard fires `/v1/compliance/export?format=json` for the
shared (framework-agnostic) report on every page load. Even with the
sampling and parallelization in api.py, the first cold load on prod is
~5 s — fine but not "instant." Operators jumping between dashboard tabs
shouldn't pay that latency repeatedly.

This worker pre-warms the request-side singleflight cache (
``api._REPORT_CACHE``) for the windows the dashboard's RangePicker
defaults to: today, 7d, 30d, 90d. Each tick of the worker computes those
windows in parallel via the same ``_load_shared_report`` path the request
handler uses, so the cache stays warm and dashboard navigations land on
~50 ms cache reads instead of the ~5 s cold compute.

Lifecycle
---------
Started from ``main._init_compliance_precompute`` after the lineage
reader is wired. Stopped during shutdown via the same idempotent
``stop()``/``await task`` pattern the other workers use.

Fail-open
---------
A reader error inside one tick logs a warning and skips that window —
the next tick retries. The worker NEVER raises out of its ``run()``
loop. A dead worker would silently regress the dashboard to cold-load
latency; a noisy log fail-open keeps the system observable instead.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Windows the dashboard's RangePicker offers by default. Each tuple is
# (preset_name, days_back). The worker pre-warms exactly these windows
# every tick — operators using "Custom" still get the singleflight cache
# from the first request, just not the pre-warm.
_PREWARM_WINDOWS: list[tuple[str, int]] = [
    ("today", 0),
    ("7d", 7),
    ("30d", 30),
    ("90d", 90),
]

# Tick interval. Must be < api._REPORT_TTL_S so the cache never expires
# between refreshes. With TTL=120s and tick=60s, every cache entry sees
# at least one refresh before it would expire — even if one tick takes
# a few seconds to complete its work.
_TICK_INTERVAL_S = 60.0


def _today_window(days_back: int) -> tuple[str, str]:
    """Return (start, end) in YYYY-MM-DD form for a 'today-Nd .. today' window.

    Matches what the dashboard's ``windowForPreset`` (Compliance.jsx)
    produces, so the cache keys align. UTC-anchored — the dashboard runs
    in browser-local time but its ``Date#toISOString().slice(0,10)`` is
    UTC-day, same as here.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)
    return start.isoformat(), end.isoformat()


class CompliancePrecomputeWorker:
    """Periodic warmer for the compliance report cache.

    Stateless aside from the background ``Task`` handle — the warmed
    cache lives in ``compliance.api._REPORT_CACHE``.
    """

    def __init__(self, reader, *, tick_interval_s: float = _TICK_INTERVAL_S) -> None:
        self._reader = reader
        self._tick_interval = max(5.0, float(tick_interval_s))
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._last_tick_ok = False
        self._last_tick_at: datetime | None = None
        self._last_error: str | None = None

    def start(self) -> asyncio.Task:
        """Schedule the run loop on the current event loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="compliance-precompute")
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
        # Warm immediately on start — operators hitting the dashboard
        # within the first 60s after a deploy still want a warm cache.
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
        """Pre-warm every default window concurrently."""
        from gateway.compliance.api import _load_shared_report

        windows = [_today_window(d) for _, d in _PREWARM_WINDOWS]

        async def _one(start: str, end: str) -> tuple[str, str, bool, str | None]:
            try:
                await _load_shared_report(self._reader, start, end)
                return start, end, True, None
            except Exception as exc:  # noqa: BLE001 - fail-open per docstring
                logger.warning(
                    "Compliance precompute failed for window %s..%s: %s",
                    start, end, exc,
                )
                return start, end, False, str(exc)

        results = await asyncio.gather(*[_one(s, e) for s, e in windows])
        any_ok = any(ok for _, _, ok, _ in results)
        first_err = next((err for _, _, ok, err in results if not ok), None)
        self._last_tick_ok = any_ok
        self._last_tick_at = datetime.now(timezone.utc)
        self._last_error = first_err

    # Health surface for connections tile / readiness check / debugging.
    @property
    def health(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "last_tick_ok": self._last_tick_ok,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "last_error": self._last_error,
            "windows": [{"preset": name, "days_back": days} for name, days in _PREWARM_WINDOWS],
            "tick_interval_s": self._tick_interval,
        }
