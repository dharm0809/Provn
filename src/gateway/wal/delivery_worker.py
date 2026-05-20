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
    _DEFAULT_MAX_RETRIES = 10
    _DEFAULT_BATCH_ERROR_BUDGET = 5

    @staticmethod
    def _resolve_batch_size() -> int:
        try:
            s = get_settings()
            v = s.delivery_batch_size
            return v if isinstance(v, int) else DeliveryWorker._DEFAULT_BATCH_SIZE
        except Exception:
            return DeliveryWorker._DEFAULT_BATCH_SIZE

    @staticmethod
    def _resolve_max_retries() -> int:
        try:
            s = get_settings()
            v = getattr(s, "wal_delivery_max_retries", None)
            return v if isinstance(v, int) and v >= 0 else DeliveryWorker._DEFAULT_MAX_RETRIES
        except Exception:
            return DeliveryWorker._DEFAULT_MAX_RETRIES

    @staticmethod
    def _resolve_batch_error_budget() -> int:
        try:
            s = get_settings()
            v = getattr(s, "wal_delivery_batch_error_budget", None)
            return v if isinstance(v, int) and v >= 1 else DeliveryWorker._DEFAULT_BATCH_ERROR_BUDGET
        except Exception:
            return DeliveryWorker._DEFAULT_BATCH_ERROR_BUDGET

    def __init__(self, wal: WALWriter, sink: object | None = None) -> None:
        # ``sink`` (optional): a StorageBackend-like object exposing
        # ``async write_execution(dict)->bool`` / ``write_tool_event``.
        # When set, the worker drains undelivered WAL rows to that sink
        # (the Walacor backend) instead of POSTing to the control-plane
        # aggregator. This is the durability path for Walacor-backed
        # deployments: the request path no longer writes to Walacor
        # inline (see StorageRouter._inline_backends), so this bounded
        # background drainer — same backoff / batch error budget / DLQ —
        # is what delivers records and marks them delivered, immune to
        # request concurrency and to a slow/down Walacor backend.
        self._wal = wal
        self._sink = sink
        self._running = False
        self._task: asyncio.Task | None = None
        self._interval = 1.0
        self._batch_size = self._resolve_batch_size()
        self._max_retries = self._resolve_max_retries()
        self._batch_error_budget = self._resolve_batch_error_budget()
        self._backoff = self._INITIAL_BACKOFF
        self._max_backoff = self._MAX_BACKOFF
        self._cycles = 0
        self._client: httpx.AsyncClient | None = None
        # Running tally of records moved to the dead-letter queue this
        # process lifetime.  Logged at WARNING (not ERROR — ERROR pages
        # on-call) every time a new DLQ entry is written, plus a periodic
        # rollup so dashboards can scrape from logs if they need to.
        self._dlq_count = 0
        # Per-record retry counter (in-memory, process-local). Once a
        # record exceeds ``_max_retries`` it is parked in the WAL DLQ so
        # later records aren't starved by one poisoned envelope. The map
        # is pruned by removing the key on delivery / DLQ promotion.
        # Bounded by ``batch_size`` × pending count — never grows beyond
        # the live undelivered set.
        # Alternative considered (and rejected): persist the attempt count
        # in a new WAL column so retries survive process restarts. The
        # current behaviour (reset on restart) is acceptable because a
        # restart already implies operator intervention, and adding a
        # column would force a schema migration on every existing
        # deployment for a marginal benefit.
        self._attempt_counts: dict[str, int] = {}

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
                if self._sink is not None:
                    batch_unhealthy = await self._deliver_batch_walacor()
                else:
                    batch_unhealthy = await self._deliver_batch()
                if batch_unhealthy:
                    # Aggregator returned failures up to the batch error
                    # budget. Sleep with exponential backoff before the
                    # next cycle instead of pinning the CPU on a clearly
                    # broken endpoint.
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, self._max_backoff)
                else:
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

    def _record_transient_failure(
        self, execution_id: str, reason: str
    ) -> bool:
        """Bump the in-memory retry counter; promote to DLQ if exhausted.

        Returns True iff the record was promoted to the DLQ.  When True
        the caller should continue to the next record in the batch — the
        WAL row will no longer appear in subsequent ``get_undelivered``
        results.
        """
        attempts = self._attempt_counts.get(execution_id, 0) + 1
        self._attempt_counts[execution_id] = attempts
        if attempts < self._max_retries:
            return False
        # Exhausted — park in DLQ so the batch isn't starved indefinitely.
        try:
            self._wal.mark_dead_lettered(
                execution_id,
                f"retries exhausted ({attempts}/{self._max_retries}): {reason}"[:512],
            )
        except Exception:
            logger.warning(
                "DLQ promotion failed for execution_id=%s after %d retries",
                execution_id,
                attempts,
                exc_info=True,
            )
            return False
        self._attempt_counts.pop(execution_id, None)
        self._dlq_count += 1
        delivery_total.labels(result="dead_letter").inc()
        logger.warning(
            "Delivery dead-lettered after %d retries execution_id=%s reason=%s dlq_total=%d",
            attempts,
            execution_id,
            reason,
            self._dlq_count,
        )
        return True

    async def _deliver_batch(self) -> bool:
        """Drain a batch of undelivered records.

        Returns True iff the batch ended unhealthy (the per-batch error
        budget was exhausted), so ``_loop`` knows to back off. Returning
        False — including when there are no rows — keeps the loop at its
        initial polling interval.
        """
        settings = get_settings()
        base = settings.control_plane_url.rstrip("/")
        headers = self._control_plane_headers()
        batch_size = self._batch_size
        rows = self._wal.get_undelivered(limit=batch_size)
        client = await self._get_client()
        # Cap the number of failures we will tolerate within a single
        # batch before backing off the whole loop. A poisoned envelope
        # no longer starves later records (`continue` per-record), but a
        # genuinely down aggregator should still trigger the exponential
        # backoff in `_loop` rather than hammer the same dead URL once
        # per record at full speed.
        batch_errors = 0
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
                    self._attempt_counts.pop(execution_id, None)
                elif r.status_code == 409:
                    delivery_total.labels(result="duplicate").inc()
                    self._wal.mark_delivered(execution_id)
                    self._attempt_counts.pop(execution_id, None)
                elif 400 <= r.status_code < 500:
                    # 4xx is non-retryable: the control plane explicitly
                    # rejected the body.  Park the record in the DLQ
                    # (delivery_status='dead_letter') instead of silently
                    # marking it delivered — operators can query the DLQ
                    # to see what was lost and why.
                    delivery_total.labels(result="dead_letter").inc()
                    reason = f"HTTP {r.status_code}: {r.text[:200] if r.text else ''}"
                    self._wal.mark_dead_lettered(execution_id, reason)
                    self._attempt_counts.pop(execution_id, None)
                    self._dlq_count += 1
                    logger.warning(
                        "Delivery dead-lettered execution_id=%s status=%s dlq_total=%d",
                        execution_id, r.status_code, self._dlq_count,
                    )
                else:
                    # 5xx — retryable. Don't `break`: a single stuck
                    # record would starve the entire queue forever. Bump
                    # the per-record retry counter, promote to DLQ when
                    # exhausted, then `continue` to the next record.
                    delivery_total.labels(result="error").inc()
                    promoted = self._record_transient_failure(
                        execution_id, f"HTTP {r.status_code}"
                    )
                    if not promoted:
                        logger.warning(
                            "Delivery server error for %s: HTTP %s — retry %d/%d",
                            execution_id,
                            r.status_code,
                            self._attempt_counts.get(execution_id, 0),
                            self._max_retries,
                        )
                    batch_errors += 1
                    if batch_errors >= self._batch_error_budget:
                        # Aggregator clearly impaired; let _loop back off
                        # before churning through the rest of the batch.
                        logger.warning(
                            "Delivery batch error budget exhausted (%d errors) — backing off",
                            batch_errors,
                        )
                        break
                    continue
            except asyncio.CancelledError:
                raise  # let cancellation propagate
            except Exception as e:
                # Transport-level failure (connect refused, timeout, …).
                # Same per-record retry policy as 5xx — `continue`, not
                # `break`, so one bad record can't starve the queue.
                delivery_total.labels(result="error").inc()
                promoted = self._record_transient_failure(execution_id, str(e))
                if not promoted:
                    logger.warning(
                        "Delivery failed for %s: %s — retry %d/%d",
                        execution_id,
                        e,
                        self._attempt_counts.get(execution_id, 0),
                        self._max_retries,
                    )
                batch_errors += 1
                if batch_errors >= self._batch_error_budget:
                    logger.warning(
                        "Delivery batch error budget exhausted (%d transport errors) — backing off",
                        batch_errors,
                    )
                    break
                continue
        return batch_errors >= self._batch_error_budget

    def _walacor_client_for_probe(self):
        """Return the underlying WalacorClient for existence probes, or None.

        The sink is a duck-typed StorageBackend (see ``__init__``). For the
        production ``WalacorBackend`` wrapper, the real ``WalacorClient`` is
        exposed as ``sink._client``. Tests can either inject a sink that
        already exposes ``execution_exists`` / ``tool_event_exists`` directly,
        or set ``sink._client`` to a mock. We return whichever object exposes
        the probe methods; the caller checks for None and falls through to
        retry if no probe surface is available.
        """
        sink = self._sink
        if sink is None:
            return None
        # Direct (test) injection: sink itself implements the probes.
        if hasattr(sink, "execution_exists") and hasattr(sink, "tool_event_exists"):
            return sink
        # Production: WalacorBackend wraps a WalacorClient as ``_client``.
        inner = getattr(sink, "_client", None)
        if inner is not None and hasattr(inner, "execution_exists"):
            return inner
        return None

    async def _deliver_batch_walacor(self) -> bool:
        """Drain a batch of undelivered WAL rows to the Walacor sink.

        Same robustness contract as ``_deliver_batch`` (the control-plane
        path): bounded batch, per-record retry → DLQ via
        ``_record_transient_failure``, and a batch error budget that
        signals ``_loop`` to back off exponentially. Because only this
        single worker (one client, ``batch_size`` rows/cycle) ever talks
        to Walacor, request concurrency can never exhaust the Walacor
        connection pool, and a slow/down backend just leaves rows
        ``delivered=0`` until it recovers — never blocking requests.

        Returns True iff the per-batch error budget was exhausted.
        """
        rows = self._wal.get_undelivered(limit=self._batch_size)
        if not rows:
            return False
        batch_errors = 0
        for execution_id, record_json, _ in rows:
            try:
                body = json.loads(record_json)
            except (TypeError, ValueError):
                # Poisoned row — never parseable, so retrying is futile.
                # Park in DLQ so it can't starve the queue forever.
                delivery_total.labels(result="dead_letter").inc()
                self._wal.mark_dead_lettered(execution_id, "invalid record_json")
                self._attempt_counts.pop(execution_id, None)
                self._dlq_count += 1
                continue
            is_tool_event = body.get("event_type") == "tool_call"
            try:
                if is_tool_event:
                    ok = await self._sink.write_tool_event(body)  # type: ignore[union-attr]
                else:
                    ok = await self._sink.write_execution(body)  # type: ignore[union-attr]
                ok = ok is not False  # legacy None == success
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — sink must not kill the loop
                ok = False
                logger.debug("Walacor sink raised for %s: %s", execution_id, e)
            if ok:
                delivery_total.labels(result="success").inc()
                self._wal.mark_delivered(execution_id)
                self._attempt_counts.pop(execution_id, None)
                continue
            # Write-time idempotency probe — structural fix for the dup-write
            # class addressed read-side by PR #61. The sink reported failure,
            # but the failure mode that creates duplicates is "write landed,
            # ack lost on the return path": the record is *already* in
            # Walacor and a retry would produce a duplicate record_id.
            # Probe existence BEFORE the retry path. If the row is present,
            # mark delivered locally and skip retry entirely. If absent (or
            # the probe itself errored), fall through to the existing retry
            # logic — the read-side dedup in WalacorLineageReader remains as
            # belt-and-braces for any legacy/edge dups.
            #
            # Cost: one extra Walacor round-trip per *failed* write (not per
            # write). Successful writes never reach this branch.
            client = self._walacor_client_for_probe()
            probe_key = (
                body.get("event_id") if is_tool_event else body.get("record_id")
            )
            if client is not None and probe_key:
                try:
                    if is_tool_event:
                        already = await client.tool_event_exists(probe_key)
                    else:
                        already = await client.execution_exists(probe_key)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "Walacor existence probe raised for %s: %s",
                        execution_id, e,
                    )
                    already = False
                if already:
                    delivery_total.labels(result="duplicate").inc()
                    self._wal.mark_delivered(execution_id)
                    self._attempt_counts.pop(execution_id, None)
                    logger.info(
                        "Walacor write idempotency hit — record already present "
                        "(no retry scheduled) execution_id=%s probe_key=%s",
                        execution_id, probe_key,
                    )
                    continue
            # Transient failure: retry with per-record counter → DLQ when
            # exhausted, and trip the batch budget so _loop backs off
            # instead of hammering a degraded Walacor backend.
            delivery_total.labels(result="error").inc()
            promoted = self._record_transient_failure(
                execution_id, "walacor sink write failed"
            )
            if not promoted:
                logger.warning(
                    "Walacor delivery failed for %s — retry %d/%d",
                    execution_id,
                    self._attempt_counts.get(execution_id, 0),
                    self._max_retries,
                )
            batch_errors += 1
            if batch_errors >= self._batch_error_budget:
                logger.warning(
                    "Walacor delivery batch error budget exhausted "
                    "(%d errors) — backing off",
                    batch_errors,
                )
                break
        return batch_errors >= self._batch_error_budget

    async def drain(self, timeout: float = 5.0) -> None:
        """Best-effort drain of pending writes before shutdown.

        Issues a single ``_deliver_batch`` so freshly-written records get
        one chance to flush before the writer thread is torn down. Bounded
        by ``timeout`` so shutdown never hangs on a stuck aggregator.

        Mirrors ``completeness_middleware._drain_pending_attempt_writes``
        — both shutdown drains have the same shape (asyncio.wait_for with
        a tight ceiling, swallow on timeout) so on_shutdown can call them
        symmetrically.
        """
        try:
            batch = (
                self._deliver_batch_walacor()
                if self._sink is not None
                else self._deliver_batch()
            )
            await asyncio.wait_for(batch, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "DeliveryWorker.drain timed out after %.1fs — pending records will retry on next start",
                timeout,
            )
        except Exception:
            logger.warning("DeliveryWorker.drain failed", exc_info=True)
