"""Unit tests for BatchWriter group commit."""

import asyncio
import os
import tempfile

import pytest
from unittest.mock import MagicMock

from gateway.wal.batch_writer import BatchWriter


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_mock_writer():
    writer = MagicMock()
    writer.write_batch = MagicMock()
    writer.write_durable = MagicMock()
    return writer


@pytest.mark.anyio
async def test_enqueue_and_flush():
    """Records enqueued are flushed via write_batch."""
    writer = _make_mock_writer()
    bw = BatchWriter(writer, flush_interval_ms=50, max_size=10)
    await bw.start()
    try:
        await bw.enqueue({"execution_id": "e1", "data": "test1"})
        await bw.enqueue({"execution_id": "e2", "data": "test2"})
        await asyncio.sleep(0.2)  # Wait for flush
    finally:
        await bw.stop()
    writer.write_batch.assert_called()
    # Verify records were passed
    all_records = []
    for call in writer.write_batch.call_args_list:
        all_records.extend(call[0][0])
    assert len(all_records) == 2
    ids = {r["execution_id"] for r in all_records}
    assert ids == {"e1", "e2"}


@pytest.mark.anyio
async def test_max_size_triggers_flush():
    """Batch flushes when max_size is reached."""
    writer = _make_mock_writer()
    bw = BatchWriter(writer, flush_interval_ms=5000, max_size=3)
    await bw.start()
    try:
        for i in range(3):
            await bw.enqueue({"execution_id": f"e{i}"})
        await asyncio.sleep(0.1)
    finally:
        await bw.stop()
    # Should have flushed at least once
    assert writer.write_batch.call_count >= 1


@pytest.mark.anyio
async def test_stop_flushes_remaining():
    """Stopping the batch writer flushes remaining records."""
    writer = _make_mock_writer()
    bw = BatchWriter(writer, flush_interval_ms=5000, max_size=100)
    await bw.start()
    await bw.enqueue({"execution_id": "e1"})
    await bw.stop()
    # write_batch should have been called during stop's flush
    total_records = sum(len(call[0][0]) for call in writer.write_batch.call_args_list)
    assert total_records >= 1


@pytest.mark.anyio
async def test_fallback_on_batch_error():
    """If write_batch fails, falls back to individual writes."""
    writer = _make_mock_writer()
    writer.write_batch.side_effect = Exception("batch fail")
    bw = BatchWriter(writer, flush_interval_ms=50, max_size=10)
    await bw.start()
    try:
        await bw.enqueue({"execution_id": "e1"})
        await asyncio.sleep(0.2)
    finally:
        await bw.stop()
    writer.write_durable.assert_called()


def test_write_batch_method():
    """WALWriter.write_batch writes multiple records in one transaction."""
    from gateway.wal.writer import WALWriter

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        w = WALWriter(db_path)
        records = [
            {"execution_id": f"e{i}", "data": f"value{i}"} for i in range(5)
        ]
        w.write_batch(records)
        assert w.pending_count() == 5
        w.close()


def test_write_batch_empty():
    """write_batch with empty list is a no-op."""
    from gateway.wal.writer import WALWriter

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        w = WALWriter(db_path)
        w.write_batch([])
        assert w.pending_count() == 0
        w.close()


@pytest.mark.anyio
async def test_pending_count():
    """pending_count reflects queue size."""
    writer = _make_mock_writer()
    bw = BatchWriter(writer, flush_interval_ms=5000, max_size=100)
    assert bw.pending_count == 0
    # Don't start — just enqueue to check count
    await bw._queue.put({"execution_id": "e1"})
    assert bw.pending_count == 1
