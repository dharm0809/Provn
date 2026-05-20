"""Multi-PID aggregation tests for LineageReader (Phase 1.1).

When ``WALACOR_UVICORN_WORKERS>1``, each uvicorn worker writes its own
``wal-<pid>.db`` file (single-writer SQLite is unsafe across processes).
The lineage SQLite reader must therefore union across every WAL file in
the directory — matching the readiness-check aggregator already in
``readiness/checks/integrity.py:_exec_wal_ro_all``.

These tests pin the behaviour at the public API: build several worker
files in one tmp_path, point the reader at any of them, and assert that
results reflect rows from EVERY file (not just the one the path points
to).
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


# All fixture rows live in the same recent minute so they fall inside the
# 1h window of ``get_metrics_history("1h")``. Computed once at import time
# so every worker file shares the same bucket key.
_BUCKET_DT = (
    datetime.now(timezone.utc).replace(second=0, microsecond=0)
    - timedelta(minutes=5)
)
_BUCKET_TS = _BUCKET_DT.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
_BUCKET_TS_30S = (
    _BUCKET_DT.replace(second=30).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
)
_BUCKET_TS_15S = (
    _BUCKET_DT.replace(second=15).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
)

from gateway.lineage.reader import LineageReader


# ---------------------------------------------------------------------------
# Fixture helpers — minimal subset of test_lineage_reader.py's helpers,
# duplicated here so the multi-PID file stays self-contained.
# ---------------------------------------------------------------------------

def _create_wal_db(db_path: str) -> sqlite3.Connection:
    from gateway.wal.writer import _apply_schema
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _apply_schema(conn)
    conn.commit()
    return conn


def _insert_execution(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    execution_id: str,
    sequence: int = 0,
    previous_record_id: str | None = None,
    timestamp: str = "2026-03-03T10:00:00+00:00",
    model_id: str = "test-model",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
    latency_ms: int = 100,
):
    record_id = f"rec-{execution_id}"
    record = {
        "execution_id": execution_id,
        "session_id": session_id,
        "model_attestation_id": "test-model",
        "policy_version": 1,
        "policy_result": "pass",
        "tenant_id": "test-tenant",
        "gateway_id": "gw-test",
        "timestamp": timestamp,
        "sequence_number": sequence,
        "record_id": record_id,
        "previous_record_id": previous_record_id,
        "prompt_text": "prompt",
        "response_content": "response",
    }
    conn.execute(
        """INSERT INTO wal_records
           (execution_id, record_json, created_at, event_type, session_id,
            timestamp, model_id, provider, sequence_number, policy_result,
            prompt_tokens, completion_tokens, total_tokens, latency_ms)
           VALUES (?, ?, ?, 'execution', ?, ?, ?, 'ollama', ?, 'pass',
                   ?, ?, ?, ?)""",
        (execution_id, json.dumps(record), timestamp, session_id, timestamp,
         model_id, sequence, prompt_tokens, completion_tokens, total_tokens, latency_ms),
    )
    conn.commit()
    return record


def _insert_attempt(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    timestamp: str,
    disposition: str = "forwarded",
    status_code: int = 200,
):
    conn.execute(
        """INSERT INTO gateway_attempts
           (request_id, timestamp, tenant_id, provider, model_id, path, disposition, execution_id, status_code)
           VALUES (?, ?, 'test-tenant', 'ollama', 'qwen3:4b', '/v1/chat/completions', ?, ?, ?)""",
        (request_id, timestamp, disposition, None, status_code),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixture: a directory with TWO per-PID WAL files. The reader's
# constructor takes a single path; ``_db_paths`` derives the dir from
# its parent, then ``iter_wal_db_paths`` discovers every ``wal*.db``.
# We point the reader at the first file but expect rows from both.
# ---------------------------------------------------------------------------

@pytest.fixture
def multi_pid_wal_dir():
    """Build two WAL files (wal-1.db, wal-2.db) in one tmp dir.

    Worker 1 holds session-a (2 executions + 2 attempts).
    Worker 2 holds session-b (1 execution + 1 attempt).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        path1 = os.path.join(tmpdir, "wal-1.db")
        path2 = os.path.join(tmpdir, "wal-2.db")
        c1 = _create_wal_db(path1)
        c2 = _create_wal_db(path2)

        # Worker 1: session-a, two executions (in the recent bucket)
        rec_a0 = _insert_execution(
            c1, session_id="session-a", execution_id="exec-a0", sequence=0,
            timestamp=_BUCKET_TS,
            prompt_tokens=10, completion_tokens=5, total_tokens=15, latency_ms=100,
        )
        _insert_execution(
            c1, session_id="session-a", execution_id="exec-a1", sequence=1,
            previous_record_id=rec_a0["record_id"],
            timestamp=_BUCKET_TS_30S,
            prompt_tokens=20, completion_tokens=10, total_tokens=30, latency_ms=200,
        )
        # Worker 1 attempts — both in the same 1-minute bucket
        # so the multi-PID bucket-merge logic is exercised.
        _insert_attempt(c1, request_id="req-a0",
                        timestamp=_BUCKET_TS,
                        disposition="forwarded")
        _insert_attempt(c1, request_id="req-a1",
                        timestamp=_BUCKET_TS_30S,
                        disposition="forwarded")

        # Worker 2: session-b, one execution
        _insert_execution(
            c2, session_id="session-b", execution_id="exec-b0", sequence=0,
            timestamp=_BUCKET_TS_15S,
            prompt_tokens=100, completion_tokens=50, total_tokens=150, latency_ms=500,
        )
        # Worker 2 attempt — SAME bucket as worker 1's req-a0, to verify
        # the bucket merge across files SUMs counts rather than picking one.
        _insert_attempt(c2, request_id="req-b0",
                        timestamp=_BUCKET_TS,
                        disposition="denied_policy", status_code=403)

        c1.close()
        c2.close()
        yield tmpdir, path1, path2


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_list_sessions_unions_across_pid_files(multi_pid_wal_dir):
    tmpdir, path1, _ = multi_pid_wal_dir
    # Constructor path is irrelevant — reader discovers siblings via dir scan.
    reader = LineageReader(path1)
    sessions = reader.list_sessions()
    ids = {s["session_id"] for s in sessions}
    # Both worker-local sessions must surface.
    assert ids == {"session-a", "session-b"}, (
        f"LineageReader did not union across worker files; got {ids}. "
        "Phase 1.1 contract violated."
    )
    reader.close()


def test_count_sessions_sums_across_pid_files(multi_pid_wal_dir):
    tmpdir, path1, _ = multi_pid_wal_dir
    reader = LineageReader(path1)
    # 1 session in worker-1 + 1 in worker-2 = 2 globally.
    assert reader.count_sessions() == 2
    reader.close()


def test_metrics_history_sums_buckets_across_pid_files(multi_pid_wal_dir):
    """Critical: when two worker files have rows in the SAME minute bucket,
    the cross-file merge must SUM the counts, not pick one or concatenate.
    """
    tmpdir, path1, _ = multi_pid_wal_dir
    reader = LineageReader(path1)
    hist = reader.get_metrics_history("1h")
    # All 3 attempts in the multi-pid fixture are in the same minute bucket
    # (10:00:00 — both req-a0 and req-b0 — plus req-a1 at 10:00:30 also
    # buckets to 10:00:00 under the 1m granularity). Total should be 3.
    total_attempts = sum(b["total"] for b in hist["buckets"])
    assert total_attempts == 3, (
        f"expected 3 attempts unioned across two worker files; got {total_attempts}"
    )
    # 2 forwarded (worker 1) + 1 denied (worker 2)
    allowed = sum(b["allowed"] for b in hist["buckets"])
    blocked = sum(b["blocked"] for b in hist["buckets"])
    assert allowed == 2
    assert blocked == 1
    reader.close()


def test_token_latency_history_aggregates_across_pid_files(multi_pid_wal_dir):
    tmpdir, path1, _ = multi_pid_wal_dir
    reader = LineageReader(path1)
    hist = reader.get_token_latency_history("1h")
    total_tokens = sum(b["total_tokens"] for b in hist["buckets"])
    # 15 + 30 (worker 1) + 150 (worker 2) = 195
    assert total_tokens == 195
    request_count = sum(b["request_count"] for b in hist["buckets"])
    assert request_count == 3
    # avg_latency_ms must be reconstructed from SUM/COUNT, not averaged
    # naively across files. Sum of latencies = 100+200+500 = 800; count = 3;
    # expected avg = 266.7. (All three fall in the same bucket.)
    non_zero = [b for b in hist["buckets"] if b["request_count"] > 0]
    assert len(non_zero) == 1
    assert non_zero[0]["avg_latency_ms"] == pytest.approx(266.7, abs=0.1)
    assert non_zero[0]["max_latency_ms"] == 500
    reader.close()


def test_get_execution_finds_row_in_any_pid_file(multi_pid_wal_dir):
    tmpdir, path1, _ = multi_pid_wal_dir
    reader = LineageReader(path1)
    # exec-b0 lives in wal-2.db; the reader must find it.
    rec = reader.get_execution("exec-b0")
    assert rec is not None
    assert rec["session_id"] == "session-b"
    reader.close()


def test_get_attempts_unions_and_sums_stats(multi_pid_wal_dir):
    tmpdir, path1, _ = multi_pid_wal_dir
    reader = LineageReader(path1)
    result = reader.get_attempts(limit=100)
    assert result["total"] == 3
    # Per-disposition stats must sum across files.
    assert result["stats"].get("forwarded", 0) == 2
    assert result["stats"].get("denied_policy", 0) == 1
    # Items list contains rows from both files.
    rids = {it["request_id"] for it in result["items"]}
    assert rids == {"req-a0", "req-a1", "req-b0"}
    reader.close()


def test_attestation_summary_merges_groups_across_pid_files(multi_pid_wal_dir):
    tmpdir, path1, _ = multi_pid_wal_dir
    reader = LineageReader(path1)
    rows = reader.get_attestation_summary(
        (_BUCKET_DT - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00",
        (_BUCKET_DT + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00",
    )
    # All three executions share (model_id=test-model, provider=ollama),
    # so cross-file merge collapses to one row with request_count=3.
    assert len(rows) == 1
    assert rows[0]["request_count"] == 3
    assert rows[0]["total_tokens"] == 195
    reader.close()


def test_single_wal_db_still_works(tmp_path):
    """Regression guard: single-worker mode (only wal.db present) must
    be byte-identical with pre-Phase-1.1. The reader's path-based
    fallback handles this through ``iter_wal_db_paths``.
    """
    db_path = str(tmp_path / "wal.db")
    conn = _create_wal_db(db_path)
    _insert_execution(conn, session_id="solo", execution_id="exec-solo")
    _insert_attempt(conn, request_id="req-solo",
                    timestamp="2026-03-03T10:00:00+00:00")
    conn.close()

    reader = LineageReader(db_path)
    sessions = reader.list_sessions()
    assert [s["session_id"] for s in sessions] == ["solo"]
    assert reader.count_sessions() == 1
    assert reader.get_execution("exec-solo") is not None
    attempts = reader.get_attempts(limit=10)
    assert attempts["total"] == 1
    assert attempts["stats"].get("forwarded") == 1
    reader.close()


def test_count_sessions_in_window_sums_across_pid_files(multi_pid_wal_dir):
    tmpdir, path1, _ = multi_pid_wal_dir
    reader = LineageReader(path1)
    n = reader.count_sessions_in_window(
        (_BUCKET_DT - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00",
        (_BUCKET_DT + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00",
    )
    assert n == 2  # session-a + session-b
    reader.close()
