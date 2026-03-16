"""Tests for WAL writer batch commit behavior."""
import sqlite3
import time
from gateway.wal.writer import WALWriter


def test_batch_commit_groups_writes(tmp_path):
    """Multiple enqueued writes should all be persisted."""
    db_path = str(tmp_path / "test.db")
    writer = WALWriter(db_path)
    writer.start()

    for i in range(20):
        writer.enqueue_write_execution({
            "execution_id": f"exec-{i}",
            "session_id": "s1",
            "model_attestation_id": "m1",
            "timestamp": "2026-01-01T00:00:00Z",
        })

    time.sleep(0.3)
    writer.stop()

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM wal_records").fetchone()[0]
    conn.close()
    assert count == 20


def test_batch_commit_single_write(tmp_path):
    """A single enqueued write should still be committed."""
    db_path = str(tmp_path / "test.db")
    writer = WALWriter(db_path)
    writer.start()

    writer.enqueue_write_execution({
        "execution_id": "exec-solo",
        "session_id": "s1",
        "model_attestation_id": "m1",
        "timestamp": "2026-01-01T00:00:00Z",
    })

    time.sleep(0.3)
    writer.stop()

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM wal_records").fetchone()[0]
    conn.close()
    assert count == 1


def test_batch_commit_attempt_writes(tmp_path):
    """Attempt writes should also be batched."""
    db_path = str(tmp_path / "test.db")
    writer = WALWriter(db_path)
    writer.start()

    for i in range(10):
        writer.enqueue_write_attempt(
            request_id=f"req-{i}",
            tenant_id="t1",
            path="/v1/chat/completions",
            disposition="allowed",
            status_code=200,
        )

    time.sleep(0.3)
    writer.stop()

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM gateway_attempts").fetchone()[0]
    conn.close()
    assert count == 10


def test_batch_commit_tool_event_writes(tmp_path):
    """Tool event writes should also be batched."""
    db_path = str(tmp_path / "test.db")
    writer = WALWriter(db_path)
    writer.start()

    for i in range(5):
        writer.enqueue_write_tool_event({
            "event_id": f"tool-{i}",
            "execution_id": f"exec-{i}",
            "tool_name": "web_search",
        })

    time.sleep(0.3)
    writer.stop()

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM wal_records").fetchone()[0]
    conn.close()
    assert count == 5


def test_sentinel_mid_batch_flushes(tmp_path):
    """Writes enqueued just before stop() should still be persisted."""
    db_path = str(tmp_path / "test.db")
    writer = WALWriter(db_path)
    writer.start()

    for i in range(5):
        writer.enqueue_write_execution({
            "execution_id": f"exec-{i}",
            "session_id": "s1",
            "model_attestation_id": "m1",
            "timestamp": "2026-01-01T00:00:00Z",
        })
    writer.stop()  # sentinel arrives while batch may still be collecting

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM wal_records").fetchone()[0]
    conn.close()
    assert count == 5
