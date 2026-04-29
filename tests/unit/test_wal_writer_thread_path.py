"""Tests for the WALWriter dedicated-thread write path.

These cover bugs #5 (request_type missing from _do_write_execution),
#17 (delivery worker DLQ uses delivery_status), and #40 (writer-side
locking when self._conn is shared across threads).
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from gateway.wal.writer import WALWriter


# ---------------------------------------------------------------------------
# Bug #5: _do_write_execution must populate request_type
# ---------------------------------------------------------------------------

def _read_request_type(db_path: str, execution_id: str) -> tuple[str | None, str]:
    """Open the WAL DB read-only and return (request_type, record_json)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT request_type, record_json FROM wal_records WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no row for execution_id={execution_id}"
    return row[0], row[1]


def _make_record(execution_id: str, request_type: str | None = None) -> dict:
    rec: dict = {
        "execution_id": execution_id,
        "session_id": "sess-thread-1",
        "model_id": "test-model",
        "provider": "test",
        "timestamp": "2026-01-01T00:00:00Z",
        "policy_result": "pass",
        "tenant_id": "t1",
        "gateway_id": "gw1",
    }
    if request_type is not None:
        rec["request_type"] = request_type
    return rec


def _wait_for_row(db_path: str, execution_id: str, timeout: float = 5.0) -> None:
    """Poll until the dedicated writer thread has flushed our row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM wal_records WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return
        time.sleep(0.02)
    raise AssertionError(f"row {execution_id!r} never appeared after {timeout}s")


class TestRequestTypePersistedFromThreadPath:
    """Bug #5: enqueue_write_execution must populate request_type."""

    def test_explicit_request_type_top_level(self, tmp_path):
        db_path = str(tmp_path / "wal.db")
        writer = WALWriter(db_path)
        # Make sure the schema is in place before the writer thread races
        # ahead of test-side polling reads.
        writer._ensure_conn()
        writer.start()
        try:
            writer.enqueue_write_execution(
                _make_record("exec-explicit", request_type="batch_eval")
            )
            _wait_for_row(db_path, "exec-explicit")

            request_type, _ = _read_request_type(db_path, "exec-explicit")
            assert request_type == "batch_eval"
        finally:
            writer.close()

    def test_default_when_unset(self, tmp_path):
        db_path = str(tmp_path / "wal.db")
        writer = WALWriter(db_path)
        # Make sure the schema is in place before the writer thread races
        # ahead of test-side polling reads.
        writer._ensure_conn()
        writer.start()
        try:
            writer.enqueue_write_execution(_make_record("exec-default"))
            _wait_for_row(db_path, "exec-default")

            request_type, _ = _read_request_type(db_path, "exec-default")
            # Sync write_durable uses "user_message" as the fallback, the
            # thread path must match.
            assert request_type == "user_message"
        finally:
            writer.close()

    def test_metadata_request_type_is_promoted(self, tmp_path):
        db_path = str(tmp_path / "wal.db")
        writer = WALWriter(db_path)
        # Make sure the schema is in place before the writer thread races
        # ahead of test-side polling reads.
        writer._ensure_conn()
        writer.start()
        try:
            rec = _make_record("exec-meta")
            rec["metadata"] = {"request_type": "system_task"}
            writer.enqueue_write_execution(rec)
            _wait_for_row(db_path, "exec-meta")

            request_type, _ = _read_request_type(db_path, "exec-meta")
            assert request_type == "system_task"
        finally:
            writer.close()


# ---------------------------------------------------------------------------
# Bug #17: dead-letter queue API
# ---------------------------------------------------------------------------

class TestDeadLetterQueue:
    def test_mark_dead_lettered_sets_status_and_delivered(self, tmp_path):
        db_path = str(tmp_path / "wal.db")
        writer = WALWriter(db_path)
        try:
            # Seed one row using the synchronous path.
            writer.write_durable(_make_record("exec-dlq-1"))
            assert writer.pending_count() == 1

            writer.mark_dead_lettered("exec-dlq-1", reason="HTTP 422: bad payload")

            # Should NOT show up in undelivered list anymore.
            assert writer.get_undelivered() == []
            # And the DLQ counter must reflect it.
            assert writer.dead_letter_count() == 1

            # delivery_status column must read 'dead_letter'.
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT delivered, delivery_status, record_json"
                    " FROM wal_records WHERE execution_id = ?",
                    ("exec-dlq-1",),
                ).fetchone()
            finally:
                conn.close()
            assert row[0] == 1
            assert row[1] == "dead_letter"
            # Reason must be preserved inside the JSON blob.
            assert "HTTP 422" in row[2]
        finally:
            writer.close()

    def test_dead_letter_count_zero_by_default(self, tmp_path):
        writer = WALWriter(str(tmp_path / "wal.db"))
        try:
            assert writer.dead_letter_count() == 0
        finally:
            writer.close()


# ---------------------------------------------------------------------------
# Bug #40: write_lock serialises shared-conn access
# ---------------------------------------------------------------------------

class TestWriteLockSerialisation:
    def test_write_lock_exists_and_is_a_lock(self, tmp_path):
        writer = WALWriter(str(tmp_path / "wal.db"))
        try:
            assert hasattr(writer, "_write_lock"), (
                "WALWriter must expose a _write_lock so cross-thread sync "
                "callers do not race on self._conn"
            )
            # Real threading.Lock supports the context-manager protocol and
            # has acquire/release methods.
            assert hasattr(writer._write_lock, "acquire")
            assert hasattr(writer._write_lock, "release")
        finally:
            writer.close()

    def test_concurrent_write_and_mark_delivered_does_not_corrupt(self, tmp_path):
        """Spam write_durable from one thread while mark_delivered runs
        on another — exercises the shared-conn lock path.

        Without the lock this can raise sqlite ProgrammingError or
        OperationalError on cursor reuse; with the lock both operations
        complete cleanly.
        """
        db_path = str(tmp_path / "wal.db")
        writer = WALWriter(db_path)
        errors: list[Exception] = []

        # Pre-seed a row so mark_delivered always has something to update.
        writer.write_durable(_make_record("seed"))

        def _writer_thread():
            try:
                for i in range(200):
                    writer.write_durable(_make_record(f"exec-{i}"))
            except Exception as e:  # pragma: no cover — surfaces only on bug
                errors.append(e)

        def _delivery_thread():
            try:
                for i in range(200):
                    writer.mark_delivered("seed")
                    _ = writer.get_undelivered(limit=10)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        try:
            t1 = threading.Thread(target=_writer_thread)
            t2 = threading.Thread(target=_delivery_thread)
            t1.start()
            t2.start()
            t1.join(timeout=20.0)
            t2.join(timeout=20.0)

            assert not errors, f"concurrent ops produced errors: {errors!r}"
            # Final assertion: all 200 writer-thread rows landed.
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM wal_records WHERE execution_id LIKE 'exec-%'"
                ).fetchone()[0]
            finally:
                conn.close()
            assert count == 200
        finally:
            writer.close()
