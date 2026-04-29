"""Background delivery: read undelivered WAL records and POST to control plane. Exponential backoff."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from gateway.config import get_settings
from gateway.wal.writer import WALWriter
from gateway.metrics.prometheus import delivery_total

logger = logging.getLogger(__name__)


class DeliveryWorker:
    """Async task: poll WAL for undelivered records, POST to /v1/gateway/executions, mark delivered."""

    _DELIVERY_PATH = "/v1/gateway/executions"
    _PURGE_CYCLE = 60
    _INITIAL_BACKOFF = 1.0
    _MAX_BACKOFF = 60.0
    _DEFAULT_BATCH_SIZE = 50

    @staticmethod
    def _resolve_batch_size() -> int:
        try:
            s = get_settings()
            v = s.delivery_batch_size
            return v if isinstance(v, int) else DeliveryWorker._DEFAULT_BATCH_SIZE
        except Exception:
            return DeliveryWorker._DEFAULT_BATCH_SIZE

    def __init__(self, wal: WALWriter) -> None:
        self._wal = wal
        self._running = False
        self._task: asyncio.Task | None = None
        self._interval = 1.0
        self._batch_size = self._resolve_batch_size()
        self._backoff = self._INITIAL_BACKOFF
        self._max_backoff = self._MAX_BACKOFF
        self._cycles = 0
        self._client: httpx.AsyncClient | None = None
        # Running tally of records moved to the dead-letter queue this
        # process lifetime.  Logged at WARNING (not ERROR — ERROR pages
        # on-call) every time a new DLQ entry is written, plus a periodic
        # rollup so dashboards can scrape from logs if they need to.
        self._dlq_count = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        # Do NOT call run_until_complete here — stop() is invoked from on_shutdown()
        # which runs inside the event loop. Let _loop()'s CancelledError handler close
        # the client, or the OS will reclaim connections on process exit.
        self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._deliver_batch()
                self._backoff = self._INITIAL_BACKOFF
                self._cycles += 1
                if self._cycles % self._PURGE_CYCLE == 0:
                    settings = get_settings()
                    deleted = self._wal.purge_delivered(settings.wal_max_age_hours)
                    if deleted:
                        logger.info("WAL purge_delivered: deleted %d records", deleted)
                    attempts_deleted = self._wal.purge_attempts(settings.attempts_retention_hours)
                    if attempts_deleted:
                        logger.info("WAL purge_attempts: deleted %d records", attempts_deleted)
            except asyncio.CancelledError:
                if self._client and not self._client.is_closed:
                    await self._client.aclose()
                    self._client = None
                break
            except Exception as e:
                logger.warning("Delivery batch failed: %s", e)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self._max_backoff)
            await asyncio.sleep(self._interval)

    def _control_plane_headers(self) -> dict[str, str]:
        """Headers for control plane requests (X-API-Key or Authorization: Bearer)."""
        settings = get_settings()
        headers = {"Content-Type": "application/json"}
        key = (settings.control_plane_api_key or "").strip()
        if key:
            headers["X-API-Key"] = key
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def _deliver_batch(self) -> None:
        settings = get_settings()
        base = settings.control_plane_url.rstrip("/")
        headers = self._control_plane_headers()
        batch_size = self._batch_size
        rows = self._wal.get_undelivered(limit=batch_size)
        client = await self._get_client()
        for execution_id, record_json, _ in rows:
            try:
                body = json.loads(record_json)
                r = await client.post(
                    f"{base}{self._DELIVERY_PATH}",
                    json=body,
                    headers=headers,
                )
                if r.status_code in (200, 201):
                    delivery_total.labels(result="success").inc()
                    self._wal.mark_delivered(execution_id)
                elif r.status_code == 409:
                    delivery_total.labels(result="duplicate").inc()
                    self._wal.mark_delivered(execution_id)
                elif 400 <= r.status_code < 500:
                    # 4xx is non-retryable: the control plane explicitly
                    # rejected the body.  Park the record in the DLQ
                    # (delivery_status='dead_letter') instead of silently
                    # marking it delivered — operators can query the DLQ
                    # to see what was lost and why.
                    delivery_total.labels(result="dead_letter").inc()
                    reason = f"HTTP {r.status_code}: {r.text[:200] if r.text else ''}"
                    self._wal.mark_dead_lettered(execution_id, reason)
                    self._dlq_count += 1
                    logger.warning(
                        "Delivery dead-lettered execution_id=%s status=%s dlq_total=%d",
                        execution_id, r.status_code, self._dlq_count,
                    )
                else:
                    delivery_total.labels(result="error").inc()
                    logger.warning("Delivery server error for %s: HTTP %s — will retry next cycle", execution_id, r.status_code)
                    break  # retry entire batch next cycle, don't block remaining records
            except asyncio.CancelledError:
                raise  # let cancellation propagate
            except Exception as e:
                delivery_total.labels(result="error").inc()
                logger.warning("Delivery failed for %s: %s — will retry next cycle", execution_id, e)
                break  # retry next cycle instead of aborting the loop
