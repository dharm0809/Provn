from __future__ import annotations

import asyncio
import sqlite3

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.types import ModelVerdict
from gateway.intelligence.verdict_buffer import VerdictBuffer
from gateway.intelligence.verdict_flush import VerdictFlushWorker

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_flush_writes_to_db(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    buf = VerdictBuffer(max_size=100)
    for i in range(5):
        buf.record(
            ModelVerdict.from_inference(
                model_name="intent", input_text=f"t{i}",
                prediction="normal", confidence=0.9,
            )
        )
    worker = VerdictFlushWorker(buf, db, flush_interval_s=0.01, batch_size=10)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)
    worker.stop()
    await task
    with sqlite3.connect(db.path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM onnx_verdicts").fetchone()[0]
    assert count == 5


async def test_flush_respects_batch_size(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    buf = VerdictBuffer(max_size=1000)
    for i in range(25):
        buf.record(
            ModelVerdict.from_inference(
                model_name="intent", input_text=f"t{i}",
                prediction="normal", confidence=0.9,
            )
        )
    # Tiny batch size forces multiple flush iterations to drain everything.
    worker = VerdictFlushWorker(buf, db, flush_interval_s=0.005, batch_size=5)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.2)
    worker.stop()
    await task
    with sqlite3.connect(db.path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM onnx_verdicts").fetchone()[0]
    assert count == 25


async def test_flush_persists_iso8601_timestamp(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    buf = VerdictBuffer(max_size=10)
    v = ModelVerdict.from_inference(
        model_name="safety", input_text="x", prediction="safe", confidence=0.9,
    )
    buf.record(v)
    worker = VerdictFlushWorker(buf, db, flush_interval_s=0.01, batch_size=10)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)
    worker.stop()
    await task
    with sqlite3.connect(db.path) as conn:
        row = conn.execute("SELECT timestamp FROM onnx_verdicts LIMIT 1").fetchone()
    # Round-trip through fromisoformat confirms ISO-8601 format persisted correctly.
    from datetime import datetime
    parsed = datetime.fromisoformat(row[0])
    assert parsed.tzinfo is not None


async def test_flush_survives_sqlite_error(tmp_path, monkeypatch, caplog):
    """If SQLite writes fail, the worker must log + continue, not crash.

    Inference path is sacred: a broken flusher cannot take down the main
    event loop by raising.
    """
    import logging
    caplog.set_level(logging.ERROR)

    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    buf = VerdictBuffer(max_size=10)
    buf.record(
        ModelVerdict.from_inference(
            model_name="intent", input_text="x", prediction="normal", confidence=0.5,
        )
    )
    worker = VerdictFlushWorker(buf, db, flush_interval_s=0.01, batch_size=10)

    # Simulate write failure on first attempt, success on subsequent.
    calls = {"n": 0}
    original = worker._write_batch

    def flaky(verdicts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("simulated failure")
        return original(verdicts)

    monkeypatch.setattr(worker, "_write_batch", flaky)

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.05)
    worker.stop()
    await task
    # Worker must have logged the error, not propagated it
    assert any("simulated failure" in str(r.message) or "verdict flush" in str(r.message).lower()
               for r in caplog.records)
