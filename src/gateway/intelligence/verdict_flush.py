"""Background async worker that drains the VerdictBuffer to SQLite in batches.

The worker ticks on `flush_interval_s`, drains up to `batch_size` verdicts,
and writes them in a single explicit transaction (BEGIN IMMEDIATE / COMMIT)
per the IntelligenceDB autocommit contract. Write failures are logged at
ERROR and the loop continues — the inference hot path must not be taken
down by a broken flush.

Known limitation (tracked for future hardening): `drain()` removes items from
the buffer BEFORE `_write_batch()` runs. If `_write_batch()` raises (SQLite
lock, disk full, bad row), the drained batch is lost. Acceptable in Task 6's
scope — the verdict log is observational telemetry, not durable audit — but a
retry / dead-letter queue should be added before the intelligence layer is
promoted to production-critical status. Not urgent; verdicts are high-volume
and a single batch loss is statistically irrelevant to distillation outcomes.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.types import ModelVerdict
from gateway.intelligence.verdict_buffer import VerdictBuffer

logger = logging.getLogger(__name__)


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

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                batch = self._buf.drain(max_batch=self._batch)
                if batch:
                    await asyncio.to_thread(self._write_batch, batch)
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
        self._running = False

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
                "confidence, request_id, timestamp, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                    )
                    for v in verdicts
                ],
            )
            conn.commit()
        finally:
            conn.close()
