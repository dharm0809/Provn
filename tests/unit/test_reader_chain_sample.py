"""LineageReader.get_chain_verification_report: must sample, not census.

The local-SQLite chain-verification report previously verified EVERY
session in the window — for parity with the Walacor reader (which samples
the most-recent 50) it must now also cap.  Tests pin that:

1. With more sessions than the sample_limit, exactly sample_limit results
   come back.
2. The companion ``count_sessions_in_window`` returns the honest census
   so the compliance API can surface "sampled N of M".
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from gateway.lineage.reader import LineageReader


def _make_db_with_sessions(n_sessions: int) -> str:
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute(
        """
        CREATE TABLE wal_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id TEXT,
            session_id TEXT,
            sequence_number INTEGER,
            event_type TEXT DEFAULT 'execution',
            timestamp TEXT,
            record_json TEXT,
            created_at TEXT DEFAULT '2026-01-01T00:00:00'
        )
        """
    )
    for i in range(n_sessions):
        sid = f"sess-{i:04d}"
        rec = {
            "execution_id": f"exec-{i}",
            "session_id": sid,
            "sequence_number": 0,
            "record_id": f"rid-{i}",
            "previous_record_id": None,
            "timestamp": f"2026-05-01T00:{i // 60:02d}:{i % 60:02d}Z",
        }
        conn.execute(
            "INSERT INTO wal_records (execution_id, session_id, sequence_number, event_type, timestamp, record_json) "
            "VALUES (?,?,?,?,?,?)",
            (rec["execution_id"], sid, 0, "execution", rec["timestamp"], json.dumps(rec)),
        )
    conn.commit()
    conn.close()
    return tmp


def test_chain_verification_report_samples_when_over_limit() -> None:
    db = _make_db_with_sessions(100)
    try:
        reader = LineageReader(db)
        report = reader.get_chain_verification_report(
            "2026-01-01", "2026-12-31", sample_limit=10,
        )
        assert len(report) == 10, f"expected sample of 10, got {len(report)}"

        total = reader.count_sessions_in_window("2026-01-01", "2026-12-31")
        assert total == 100
    finally:
        os.unlink(db)


def test_chain_verification_report_returns_all_when_under_limit() -> None:
    db = _make_db_with_sessions(5)
    try:
        reader = LineageReader(db)
        report = reader.get_chain_verification_report(
            "2026-01-01", "2026-12-31", sample_limit=50,
        )
        assert len(report) == 5
        assert reader.count_sessions_in_window("2026-01-01", "2026-12-31") == 5
    finally:
        os.unlink(db)


def test_chain_verification_report_prefers_recent_sessions() -> None:
    # Seed 60 sessions; the 10 most-recent (by timestamp) should be chosen.
    db = _make_db_with_sessions(60)
    try:
        reader = LineageReader(db)
        report = reader.get_chain_verification_report(
            "2026-01-01", "2026-12-31", sample_limit=10,
        )
        session_ids = {r.get("session_id") for r in report}
        # Most-recent timestamps were inserted last; sess-0050..sess-0059.
        expected = {f"sess-{i:04d}" for i in range(50, 60)}
        assert session_ids == expected
    finally:
        os.unlink(db)
