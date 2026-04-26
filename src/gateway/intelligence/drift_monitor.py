"""Rolling-accuracy drift monitor — triggers retrain on regression.

The distillation worker's existing trigger (`_should_trigger`) only
fires when *enough* divergence rows have accumulated. It does not look
at *whether the production model has gotten worse*. A regressed model
that emits enough divergences eventually retrains; a regressed model
whose users simply give up and stop sending traffic does not.

This module runs a periodic asyncio task that compares recent accuracy
against a baseline window and emits a `DriftSignal` when the delta
exceeds a configurable threshold. The signal is published to listeners
(the distillation worker subscribes to schedule a forced cycle).

Accuracy is computed via `IntelligenceDB.accuracy_in_window` against
the harvester `divergence_signal`. Snapshots with coverage below a
floor are skipped — comparing a 5%-coverage window against another
5%-coverage window is statistical noise, not drift.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.registry import ALLOWED_MODEL_NAMES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriftSignal:
    """One-shot notification that a model's accuracy dropped past threshold.

    Listeners (the distillation worker) read this and decide what to do
    — typically schedule a forced cycle. The signal carries enough
    context for an alerting layer to render a human-readable message.
    """
    model: str
    window_hours: int
    baseline_accuracy: float
    current_accuracy: float
    delta: float
    sample_count: int
    detected_at: datetime


class DriftMonitor:
    """Periodic accuracy regression detector.

    Two windows: `recent` (the last `window_hours`) and `baseline` (the
    `baseline_window_count` windows ending where `recent` starts). When
    `baseline.accuracy - recent.accuracy >= threshold`, a `DriftSignal`
    fires. Both windows must clear `min_samples` and `min_coverage`
    before being used; otherwise the cycle is skipped (no false alarm
    from thin data).

    Listeners are sync callables — listener exceptions are swallowed so
    one bad subscriber can't take down the monitor.
    """

    def __init__(
        self,
        db: IntelligenceDB,
        *,
        window_hours: int = 1,
        baseline_window_count: int = 6,
        threshold: float = 0.05,
        check_interval_s: int = 600,
        min_samples: int = 50,
        min_coverage: float = 0.30,
        models: list[str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._window = timedelta(hours=int(window_hours))
        self._baseline_window_count = max(1, int(baseline_window_count))
        self._threshold = float(threshold)
        self._interval = max(1, int(check_interval_s))
        self._min_samples = max(1, int(min_samples))
        self._min_coverage = float(min_coverage)
        self._models = list(models) if models is not None else list(sorted(ALLOWED_MODEL_NAMES))
        self._listeners: list[Callable[[DriftSignal], None]] = []
        self._task: asyncio.Task | None = None
        self._running = False
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_check_at: datetime | None = None
        self._last_signal_at: dict[str, datetime] = {}

    # ── listener wiring ────────────────────────────────────────────────

    def on_drift(self, callback: Callable[[DriftSignal], None]) -> None:
        self._listeners.append(callback)

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="drift-monitor")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @property
    def last_check_at(self) -> datetime | None:
        return self._last_check_at

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.check_once()
            except Exception:
                logger.exception("drift monitor cycle failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

    # ── core check ─────────────────────────────────────────────────────

    async def check_once(self) -> list[DriftSignal]:
        """Run one accuracy comparison per model. Returns the signals fired."""
        now = self._clock()
        self._last_check_at = now
        # Fetching from SQLite is sync; offload so the loop stays free.
        signals = await asyncio.to_thread(self._compute_signals, now)
        for sig in signals:
            self._last_signal_at[sig.model] = sig.detected_at
            for cb in self._listeners:
                try:
                    cb(sig)
                except Exception:
                    logger.exception("drift listener failed")
        return signals

    def _compute_signals(self, now: datetime) -> list[DriftSignal]:
        out: list[DriftSignal] = []
        recent_start = now - self._window
        baseline_end = recent_start
        baseline_start = baseline_end - (self._window * self._baseline_window_count)
        for model in self._models:
            recent = self._db.accuracy_in_window(
                model, start=recent_start, end=now,
            )
            baseline = self._db.accuracy_in_window(
                model, start=baseline_start, end=baseline_end,
            )
            # Surface the recent-window signal coverage as a gauge —
            # operators need to see when teachers / harvesters stop
            # contributing labels, even before any drift threshold trips.
            try:
                from gateway.metrics.prometheus import intelligence_signal_coverage_ratio
                intelligence_signal_coverage_ratio.labels(model=model).set(recent.coverage)
            except Exception:
                logger.debug("coverage gauge update failed", exc_info=True)
            if recent.sample_count < self._min_samples:
                logger.debug(
                    "drift skip %s: recent samples %d < %d",
                    model, recent.sample_count, self._min_samples,
                )
                continue
            if baseline.sample_count < self._min_samples:
                logger.debug(
                    "drift skip %s: baseline samples %d < %d",
                    model, baseline.sample_count, self._min_samples,
                )
                continue
            if recent.coverage < self._min_coverage or baseline.coverage < self._min_coverage:
                logger.debug(
                    "drift skip %s: coverage too low (recent=%.2f baseline=%.2f)",
                    model, recent.coverage, baseline.coverage,
                )
                continue
            delta = baseline.accuracy - recent.accuracy
            if delta < self._threshold:
                continue
            out.append(DriftSignal(
                model=model,
                window_hours=int(self._window.total_seconds() / 3600) or 1,
                baseline_accuracy=baseline.accuracy,
                current_accuracy=recent.accuracy,
                delta=delta,
                sample_count=recent.sample_count,
                detected_at=now,
            ))
        return out
