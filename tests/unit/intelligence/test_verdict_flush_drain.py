"""VerdictFlushWorker.stop_and_drain flushes pending verdicts on shutdown.

Without the drain, verdicts that arrived during the worker's
flush_interval_s sleep are lost on restart. The fix:

  1. `stop()` cancels the in-flight sleep so the run loop exits promptly.
  2. `stop_and_drain()` flips the running flag and synchronously drains
     anything still in the buffer to SQLite before returning.

These tests exercise both the cooperative path (run loop running, drain
called from outside) and the standalone path (drain called without ever
having started the loop).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.types import ModelVerdict
from gateway.intelligence.verdict_buffer import VerdictBuffer
from gateway.intelligence.verdict_flush import VerdictFlushWorker


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_verdict(i: int) -> ModelVerdict:
    return ModelVerdict(
        model_name="intent",
        input_hash=f"hash-{i:08d}",
        input_features_json="{}",
        prediction="normal",
        confidence=0.9,
        request_id=f"req-{i}",
        timestamp="2026-04-28T00:00:00Z",
        version="prod-v1",
    )


def _verdict_count(db: IntelligenceDB) -> int:
    import sqlite3
    conn = sqlite3.connect(db.path)
    try:
        return conn.execute("SELECT COUNT(*) FROM onnx_verdicts").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.anyio
async def test_stop_and_drain_flushes_pending_verdicts(tmp_path: Path) -> None:
    """Items enqueued before stop_and_drain MUST end up in SQLite.

    Reproduces the fix for issue #4: previously stop() just flipped a
    bool, the run loop's next sleep would never wake, and up to
    batch_size verdicts sat in memory until the process exited.
    """
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()

    buf = VerdictBuffer(max_size=1000)
    worker = VerdictFlushWorker(buf, db, flush_interval_s=60.0, batch_size=500)

    # Long flush interval to ensure the run loop's sleep does NOT fire
    # during this test — drain must happen via stop_and_drain.
    task = asyncio.create_task(worker.run())

    # Give the loop a tick to enter sleep.
    await asyncio.sleep(0.05)

    # Enqueue more than 0 but well below batch_size.
    for i in range(42):
        buf.record(_make_verdict(i))

    assert buf.size == 42
    assert _verdict_count(db) == 0  # nothing flushed yet

    # Trigger the drain. Must complete without waiting for the 60s sleep.
    await asyncio.wait_for(worker.stop_and_drain(), timeout=2.0)

    # Buffer drained, all 42 verdicts persisted.
    assert buf.size == 0
    assert _verdict_count(db) == 42

    # Run task wakes (sleep cancelled) and exits cleanly.
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.anyio
async def test_stop_and_drain_handles_buffer_larger_than_batch_size(
    tmp_path: Path,
) -> None:
    """Drain loops until empty, even when buffer holds > batch_size items."""
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()

    buf = VerdictBuffer(max_size=2000)
    # Small batch_size to force multiple drain iterations.
    worker = VerdictFlushWorker(buf, db, flush_interval_s=60.0, batch_size=10)

    # 35 items → 4 drain batches of 10 + 1 batch of 5 (well within bound).
    for i in range(35):
        buf.record(_make_verdict(i))

    # Drive drain without ever starting the run loop.
    await worker.stop_and_drain()

    assert buf.size == 0
    assert _verdict_count(db) == 35


@pytest.mark.anyio
async def test_stop_cancels_sleep_so_loop_exits_promptly(tmp_path: Path) -> None:
    """stop() alone (no drain) still wakes the run loop.

    Verifies the cancellation hook even when the caller doesn't want the
    drain — e.g. timeout fallback path in the lifespan shutdown.
    """
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()

    buf = VerdictBuffer(max_size=1000)
    # 30s sleep — without sleep cancellation the test would time out.
    worker = VerdictFlushWorker(buf, db, flush_interval_s=30.0, batch_size=500)

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.05)  # let the loop enter sleep

    worker.stop()

    # Should exit within a fraction of a second, NOT 30s.
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.anyio
async def test_stop_and_drain_idempotent_on_empty_buffer(tmp_path: Path) -> None:
    """Calling stop_and_drain on an empty buffer is a no-op (no error)."""
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()

    buf = VerdictBuffer(max_size=1000)
    worker = VerdictFlushWorker(buf, db, flush_interval_s=60.0, batch_size=500)

    await worker.stop_and_drain()
    await worker.stop_and_drain()  # second call must also succeed

    assert _verdict_count(db) == 0
