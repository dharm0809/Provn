"""Background async worker that drains the VerdictBuffer to SQLite in batches.

The worker ticks on `flush_interval_s`, drains up to `batch_size` verdicts,
and writes them in a single explicit transaction (BEGIN IMMEDIATE / COMMIT)
per the IntelligenceDB autocommit contract. Write failures are logged at
ERROR and the loop continues — the inference hot path must not be taken
down by a broken flush.

`drain()` removes items from the buffer BEFORE `_write_batch()` runs, so a
failed write would otherwise silently drop the in-flight batch. The
dead-letter file (`{intelligence_db_path}.dlq.jsonl`, capped at
``_DLQ_MAX_BYTES`` = 50 MB) parks failed batches and replays them on the
next successful tick. See ``_park_to_dlq`` / ``_try_replay_dlq`` below.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
from pathlib import Path

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.types import ModelVerdict
from gateway.intelligence.verdict_buffer import VerdictBuffer

logger = logging.getLogger(__name__)

# Cap the dead-letter file at 50 MB. A typical verdict serialises to ~500
# bytes JSONL, so the cap parks ~100k verdicts before refusing further
# appends — generous given verdicts are observational telemetry. We refuse
# to grow past the cap rather than rotating: an operator who hasn't drained
# 100k parked verdicts has a structural problem (SQLite wedged, disk full),
# and silently rotating would mask it.
_DLQ_MAX_BYTES = 50 * 1024 * 1024


class VerdictFlushWorker:
    def __init__(
        self,
        buffer: VerdictBuffer,
        db: IntelligenceDB,
        flush_interval_s: float = 1.0,
        batch_size: int = 500,
    ) -> None:
        self._buf = buffer
        self._db = db
        self._interval = flush_interval_s
        self._batch = batch_size
        self._running = False
        # Cancellable sleep handle so stop_and_drain() can wake the
        # current tick instead of waiting up to flush_interval_s for it
        # to expire on its own.
        self._sleep_task: asyncio.Task | None = None

    @property
    def _dlq_path(self) -> str:
        # Co-locate with the intelligence DB so disk pressure on the DB's
        # volume is the same pressure the DLQ feels — easier to reason about.
        return f"{self._db.path}.dlq.jsonl"

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                self._sleep_task = asyncio.ensure_future(asyncio.sleep(self._interval))
                try:
                    await self._sleep_task
                except asyncio.CancelledError:
                    # stop_and_drain() cancelled our sleep; fall through
                    # to the loop check (`self._running` is now False) so
                    # the worker exits cleanly without re-raising.
                    if not self._running:
                        break
                    raise
                finally:
                    self._sleep_task = None
                # Best-effort DLQ replay BEFORE the in-memory drain. If the
                # DLQ replay write fails, we leave the file alone and try
                # again next tick — never combine with the fresh batch (a
                # poison row in the DLQ must not take down healthy writes).
                await asyncio.to_thread(self._try_replay_dlq)
                batch = self._buf.drain(max_batch=self._batch)
                if batch:
                    try:
                        await asyncio.to_thread(self._write_batch, batch)
                    except Exception:
                        # The drained batch is already off the buffer; if we
                        # don't park it now it is gone forever. Park to DLQ
                        # and let the next tick (or the operator) replay.
                        await asyncio.to_thread(self._park_to_dlq, batch)
                        raise
                    self._update_size_gauge()
            except Exception:
                # Hot path is sacred: log + continue, never re-raise.
                # asyncio.CancelledError inherits from BaseException (Py 3.8+),
                # so `except Exception` does NOT swallow cancellation —
                # `stop()` followed by `await task` still shuts down cleanly.
                logger.exception("verdict flush iteration failed")
                try:
                    from gateway.metrics.prometheus import (
                        intelligence_db_write_failures_total,
                    )
                    intelligence_db_write_failures_total.inc()
                except Exception:
                    logger.debug("intelligence_db_write_failures_total metric failed", exc_info=True)

    def _update_size_gauge(self) -> None:
        try:
            from gateway.metrics.prometheus import verdict_buffer_size
            verdict_buffer_size.set(self._buf.size)
        except Exception:
            logger.debug("verdict_buffer_size metric failed", exc_info=True)

    def stop(self) -> None:
        """Synchronous stop — flips the running flag and cancels the
        in-flight sleep so the run loop exits promptly. Does NOT drain
        the buffer; callers that need a final drain (graceful shutdown)
        must use `stop_and_drain()` instead.
        """
        self._running = False
        sleep_task = self._sleep_task
        if sleep_task is not None and not sleep_task.done():
            sleep_task.cancel()

    async def stop_and_drain(self) -> None:
        """Stop the loop, cancel the in-flight sleep, and flush whatever
        remains in the buffer. Bounded by the buffer's max size — drains
        in `batch_size` chunks until empty.

        Invariant for graceful shutdown: every verdict that was in the
        buffer at the time stop_and_drain() is invoked is written to
        SQLite (or surfaces as a write failure — never silently lost).
        """
        self.stop()
        # Drain in batches until the buffer is empty. Bound the loop so
        # a runaway producer can't keep us in here forever — we cap at
        # the buffer's configured max so worst case is one full
        # buffer-equivalent of writes.
        max_iterations = max(1, (self._buf.size // max(1, self._batch)) + 2)
        for _ in range(max_iterations):
            batch = self._buf.drain(max_batch=self._batch)
            if not batch:
                break
            try:
                await asyncio.to_thread(self._write_batch, batch)
                self._update_size_gauge()
            except Exception:
                # Same hot-path discipline as run(): log + continue so a
                # broken final write doesn't prevent shutdown. Park the
                # drained batch to the DLQ first — without this the
                # graceful-shutdown invariant ("every buffered verdict
                # ends up in SQLite OR surfaces as a write failure")
                # would silently lose the final batch on disk-full /
                # SQLite-lock failure paths.
                logger.exception("verdict flush stop_and_drain iteration failed")
                try:
                    await asyncio.to_thread(self._park_to_dlq, batch)
                except Exception:
                    logger.exception("DLQ park failed during stop_and_drain")
                break

    def _write_batch(self, verdicts: list[ModelVerdict]) -> None:
        # Explicit transaction per IntelligenceDB's autocommit contract —
        # batches must issue their own BEGIN / COMMIT. sqlite3.connect()
        # without `isolation_level=None` gives us implicit BEGIN before the
        # first DML and explicit commit() at the end, which is exactly what
        # we want for a batched insert.
        conn = sqlite3.connect(self._db.path)
        try:
            conn.executemany(
                "INSERT INTO onnx_verdicts "
                "(model_name, input_hash, input_features_json, prediction, "
                "confidence, request_id, timestamp, version, "
                "divergence_signal, divergence_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        v.model_name,
                        v.input_hash,
                        v.input_features_json,
                        v.prediction,
                        v.confidence,
                        v.request_id,
                        v.timestamp,
                        v.version,
                        v.divergence_signal,
                        v.divergence_source,
                    )
                    for v in verdicts
                ],
            )
            conn.commit()
        finally:
            conn.close()

    # ──────────────────────────────────────────────────────────────────
    # Dead-letter queue
    #
    # Pre-DLQ, a failed _write_batch (SQLite lock, disk full, malformed
    # row) silently dropped the in-flight batch — visible only as a log
    # line. The intelligence layer was advertised as "observational
    # telemetry", but the self-learning loop downstream treats verdict
    # gaps as model drift signal, so the loss is not actually benign.
    # JSONL append + truncate-on-success is the simplest crash-safe
    # parking lot; no rotation by design (see _DLQ_MAX_BYTES).
    # ──────────────────────────────────────────────────────────────────
    def _park_to_dlq(self, batch: list[ModelVerdict]) -> None:
        """Append a failed batch to the DLQ JSONL file (one line per verdict).

        Refuses to grow past ``_DLQ_MAX_BYTES`` — beyond the cap the
        batch is dropped and an ERROR is logged. That is the explicit
        operator signal: an unprocessed DLQ this large means SQLite is
        wedged or disk is full, and silently growing would mask both.
        """
        path = self._dlq_path
        try:
            existing = os.path.getsize(path) if os.path.exists(path) else 0
        except OSError:
            existing = 0
        if existing >= _DLQ_MAX_BYTES:
            logger.error(
                "verdict DLQ cap reached (%d bytes >= %d); dropping batch of %d "
                "— drain %s manually or investigate SQLite/disk health",
                existing, _DLQ_MAX_BYTES, len(batch), path,
            )
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                for v in batch:
                    fh.write(json.dumps(dataclasses.asdict(v), default=str))
                    fh.write("\n")
            logger.warning(
                "verdict batch parked to DLQ count=%d path=%s",
                len(batch), path,
            )
        except OSError:
            # If we cannot even park to disk, the loss is unavoidable —
            # log loudly and continue. The hot path must not be blocked.
            logger.exception("verdict DLQ park failed path=%s", path)

    def _try_replay_dlq(self) -> None:
        """Re-insert parked verdicts; truncate the file on success.

        Reads the entire DLQ file (bounded by _DLQ_MAX_BYTES = 50 MB),
        rebuilds ModelVerdict instances, and attempts a single
        _write_batch. On success the file is truncated; on failure it is
        left untouched so the next tick can try again. We never delete
        the file before the write succeeds — losing the parked batch on
        a transient SQLite lock would defeat the whole point of the DLQ.
        """
        path = self._dlq_path
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        except OSError:
            logger.exception("verdict DLQ read failed path=%s", path)
            return
        if not lines:
            # Empty file lying around from a previous run — clean up.
            try:
                Path(path).unlink()
            except OSError:
                pass
            return
        recovered: list[ModelVerdict] = []
        for ln in lines:
            try:
                d = json.loads(ln)
                recovered.append(ModelVerdict(**d))
            except (ValueError, TypeError):
                # Skip poison rows but keep going — one bad line must not
                # block recovery of the rest. The dropped row stays in
                # the file until truncation, so it's still inspectable.
                logger.warning("verdict DLQ poison row skipped: %r", ln[:200])
        if not recovered:
            # Nothing valid to replay; leave file alone for inspection.
            return
        try:
            self._write_batch(recovered)
        except Exception:
            logger.warning(
                "verdict DLQ replay failed (will retry next tick) count=%d path=%s",
                len(recovered), path,
            )
            return
        # Success — drop the file. We deliberately unlink rather than
        # truncate so a concurrent appender (there should be none, but
        # defensive) creates a fresh inode rather than racing on offsets.
        try:
            Path(path).unlink()
            logger.info(
                "verdict DLQ replayed and cleared count=%d path=%s",
                len(recovered), path,
            )
        except OSError:
            logger.exception("verdict DLQ unlink failed path=%s", path)
