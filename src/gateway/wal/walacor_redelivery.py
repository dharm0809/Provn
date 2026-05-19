"""Background WAL→Walacor redelivery sweep.

Closes a durability gap proven by a live-backend stress test: in
Walacor-backend mode the standalone control-plane ``DeliveryWorker`` is
deliberately not started (it ships to ``/v1/gateway/executions`` on a
control-plane aggregator, a different destination than the Walacor API).
The only path that delivers an execution to Walacor in that mode is the
single inline POST in ``StorageRouter.write_execution``; on success it
calls ``WALWriter.mark_delivered``. When that inline POST fails under a
load spike (live Walacor 5xx / timeout under concurrency) the WAL row
stays ``delivered=0`` and **nothing ever retries it** — the record is
durable locally but permanently absent from Walacor. A 60s/500-concurrency
flood left 102 such orphaned execution rows that did not drain even with
zero load for 160s.

This sweep is the missing retry. It is intentionally additive and
behaviour-preserving:

  * It only ever touches rows the inline path already FAILED to deliver
    (``get_undelivered`` returns ``delivered=0`` only, and skips
    dead-lettered rows). Successfully delivered rows are ``delivered=1``
    and invisible to it — the happy path is completely untouched.
  * It re-uses the existing ``WalacorClient.write_execution`` +
    ``WALWriter.mark_delivered`` exactly as the inline path does, so a
    redelivered record is byte-identical to an inline one. Walacor
    de-dupes on execution identity, so the rare "delivered but not
    marked" race re-POSTs harmlessly (same idempotency assumption the
    control-plane DeliveryWorker already relies on).
  * Fail-open: any per-row error is logged and skipped; a down backend
    triggers exponential backoff instead of hammering. It can never
    raise into startup or the request path. Worst case it delivers
    nothing — i.e. exactly today's behaviour — never worse.
"""

from __future__ import annotations

import asyncio
import json
import logging

from gateway.config import get_settings

logger = logging.getLogger(__name__)


class WalacorRedeliveryWorker:
    """Polls the WAL for execution rows the inline write failed to deliver
    and re-POSTs them to Walacor until acknowledged."""

    # Conservative cadence: this is a recovery sweep for the failure tail,
    # not the primary delivery path, so it should be cheap at idle and
    # only work hard when there is an actual backlog.
    _IDLE_INTERVAL_S = 5.0
    _MAX_BACKOFF_S = 60.0
    _BATCH = 50

    def __init__(self, wal, walacor_client) -> None:
        self._wal = wal
        self._client = walacor_client
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "WAL→Walacor redelivery sweep started "
                "(retries execution rows the inline write failed to deliver)"
            )

    async def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        backoff = self._IDLE_INTERVAL_S
        while not self._stop.is_set():
            try:
                delivered, had_rows, unhealthy = await self._sweep_once()
                if unhealthy:
                    backoff = min(self._MAX_BACKOFF_S, backoff * 2)
                else:
                    backoff = self._IDLE_INTERVAL_S
                    if delivered:
                        logger.info(
                            "WAL→Walacor redelivery: recovered %d orphaned "
                            "execution record(s)", delivered,
                        )
                # When a backlog is draining cleanly, poll again promptly.
                wait = 0.0 if (had_rows and not unhealthy) else backoff
            except Exception:
                logger.warning(
                    "WAL→Walacor redelivery sweep iteration failed", exc_info=True
                )
                backoff = min(self._MAX_BACKOFF_S, backoff * 2)
                wait = backoff
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.01, wait))
            except asyncio.TimeoutError:
                pass

    async def _sweep_once(self) -> tuple[int, bool, bool]:
        """Return (delivered_count, had_rows, batch_unhealthy)."""
        rows = await asyncio.to_thread(self._wal.get_undelivered, self._BATCH)
        if not rows:
            return (0, False, False)
        settings = get_settings()
        max_errors = max(1, getattr(settings, "wal_delivery_batch_error_budget", 5))
        delivered = 0
        errors = 0
        for execution_id, record_json, _created in rows:
            if self._stop.is_set():
                break
            try:
                record = json.loads(record_json)
            except (json.JSONDecodeError, TypeError):
                # Poisoned row: never deliverable. Park it so it stops
                # blocking the sweep (same disposition the control-plane
                # worker uses for un-parseable bodies).
                logger.warning(
                    "WAL→Walacor redelivery: unparseable record_json "
                    "execution_id=%s — dead-lettering", execution_id,
                )
                try:
                    self._wal.mark_dead_lettered(execution_id, reason="unparseable record_json")
                except Exception:
                    logger.debug("mark_dead_lettered failed", exc_info=True)
                continue
            try:
                await self._client.write_execution(record)
                # write_execution raises on failure; reaching here means
                # Walacor accepted it. Mark exactly as the inline path does.
                self._wal.mark_delivered(execution_id)
                delivered += 1
            except Exception as e:
                errors += 1
                logger.debug(
                    "WAL→Walacor redelivery: execution_id=%s not yet "
                    "deliverable (%s) — will retry", execution_id, e,
                )
                if errors >= max_errors:
                    # Backend is unhealthy enough that continuing this
                    # batch just hammers it; back off the whole loop.
                    return (delivered, True, True)
        return (delivered, True, False)
