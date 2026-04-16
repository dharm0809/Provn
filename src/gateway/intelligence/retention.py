"""Background TTL sweeper for the verdict log and shadow comparisons.

Wakes on `sweep_interval_s` (default 1h), computes a cutoff
`now - retention_days`, and DELETEs rows older than cutoff from
`onnx_verdicts` and `shadow_comparisons`. Wrapped in try/except so sweep
failures log + continue rather than crashing the main event loop.

Timestamps in both tables are ISO-8601 UTC strings — lexicographic comparison
matches chronological order for this format, so a simple `WHERE timestamp < ?`
clause is correct.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from gateway.intelligence.db import IntelligenceDB

logger = logging.getLogger(__name__)


class RetentionSweeper:
    def __init__(
        self,
        db: IntelligenceDB,
        retention_days: int = 30,
        sweep_interval_s: float = 3600.0,
    ) -> None:
        self._db = db
        # Clamp to >=1 day — prevents an accidental config of 0 (or negative)
        # from wiping every verdict on the next sweep. Matches the VerdictBuffer
        # max_size clamping pattern from Task 5 polish.
        self._retention_days = max(1, int(retention_days))
        self._interval = sweep_interval_s
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                # Sleep FIRST so startup isn't blocked by a sweep cycle.
                await asyncio.sleep(self._interval)
                await asyncio.to_thread(self._sweep_once)
            except Exception:
                # Hot path is sacred: log + continue, never re-raise.
                # asyncio.CancelledError inherits from BaseException (Py 3.8+),
                # so `except Exception` does NOT swallow cancellation —
                # `stop()` followed by `await task` still shuts down cleanly.
                logger.exception("verdict retention sweep failed")

    def stop(self) -> None:
        self._running = False

    def _sweep_once(self) -> None:
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        ).isoformat()
        # Open our own connection (not IntelligenceDB._connect which is
        # autocommit) so both DELETEs share one implicit transaction —
        # matches the VerdictFlushWorker pattern for multi-statement writes.
        conn = sqlite3.connect(self._db.path)
        try:
            cur_v = conn.execute(
                "DELETE FROM onnx_verdicts WHERE timestamp < ?",
                (cutoff_iso,),
            )
            cur_s = conn.execute(
                "DELETE FROM shadow_comparisons WHERE timestamp < ?",
                (cutoff_iso,),
            )
            conn.commit()
            # Log row counts so a catastrophic deletion (e.g. clock-skew
            # pushing `now` far into the future) shows up in dashboards
            # well before the tables are empty — defense in depth against
            # the ISO-8601 lex-order collapse described in the docstring.
            if cur_v.rowcount or cur_s.rowcount:
                logger.info(
                    "verdict retention swept verdicts=%d shadows=%d cutoff=%s",
                    cur_v.rowcount, cur_s.rowcount, cutoff_iso,
                )
        finally:
            conn.close()
