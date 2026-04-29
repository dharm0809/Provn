"""Behavioral tests: write paths must never issue wal_checkpoint.

The fix moved PRAGMA wal_checkpoint(TRUNCATE) out of write_durable /
write_tool_event into purge_delivered (batch cleanup only).  These tests
verify that invariant by intercepting conn.execute() calls.
"""

import sqlite3

import pytest

from gateway.wal.writer import WALWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_execution_record(eid: str = "exec-1") -> dict:
    return {
        "execution_id": eid,
        "model_attestation_id": "test-model",
        "policy_version": 0,
        "policy_result": "pass",
        "tenant_id": "t1",
        "gateway_id": "gw1",
        "timestamp": "2026-01-01T00:00:00Z",
    }


def _make_tool_event(event_id: str = "tool-evt-1") -> dict:
    return {
        "event_id": event_id,
        "execution_id": "exec-1",
        "tool_name": "web_search",
        "input_hash": "abc123",
        "output_hash": "def456",
        "timestamp": "2026-01-01T00:00:00Z",
    }


class _ExecuteTracker:
    """Wraps a sqlite3.Connection to track all execute() SQL statements."""

    def __init__(self, real_conn: sqlite3.Connection):
        self._real = real_conn
        self.statements: list[str] = []

    def execute(self, sql: str, parameters=()):
        self.statements.append(sql)
        return self._real.execute(sql, parameters)

    def commit(self):
        return self._real.commit()

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wal_with_tracker(tmp_path):
    """Return (WALWriter, _ExecuteTracker) with the tracker installed."""
    writer = WALWriter(str(tmp_path / "test.db"))
    # Force connection init so schema DDL is out of the way.
    conn = writer._ensure_conn()
    tracker = _ExecuteTracker(conn)
    writer._conn = tracker  # type: ignore[assignment]
    yield writer, tracker
    writer._conn = tracker._real  # restore real conn for close()
    writer.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_write_durable_no_checkpoint(wal_with_tracker):
    """write_durable must never issue PRAGMA wal_checkpoint."""
    writer, tracker = wal_with_tracker
    tracker.statements.clear()

    for i in range(10):
        writer.write_durable(_make_execution_record(f"exec-{i}"))

    checkpoint_calls = [s for s in tracker.statements if "wal_checkpoint" in s.lower()]
    assert checkpoint_calls == [], (
        f"write_durable issued checkpoint pragma: {checkpoint_calls}"
    )


def test_write_tool_event_no_checkpoint(wal_with_tracker):
    """write_tool_event must never issue PRAGMA wal_checkpoint."""
    writer, tracker = wal_with_tracker
    tracker.statements.clear()

    for i in range(10):
        writer.write_tool_event(_make_tool_event(f"tool-evt-{i}"))

    checkpoint_calls = [s for s in tracker.statements if "wal_checkpoint" in s.lower()]
    assert checkpoint_calls == [], (
        f"write_tool_event issued checkpoint pragma: {checkpoint_calls}"
    )


def test_purge_delivered_does_checkpoint(wal_with_tracker):
    """purge_delivered SHOULD checkpoint after deleting rows (positive control)."""
    writer, tracker = wal_with_tracker

    # Insert a record, mark it delivered, then purge with max_age_hours=0.
    writer.write_durable(_make_execution_record("purge-me"))
    writer.mark_delivered("purge-me")

    tracker.statements.clear()
    deleted = writer.purge_delivered(max_age_hours=0)

    assert deleted == 1
    checkpoint_calls = [s for s in tracker.statements if "wal_checkpoint" in s.lower()]
    assert len(checkpoint_calls) == 1, (
        "purge_delivered should issue exactly one wal_checkpoint after deleting rows"
    )
