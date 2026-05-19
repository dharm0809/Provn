"""Phase-1 regression test: readiness integrity checks aggregate across
per-worker WAL files (`wal-<pid>.db`), not just the current worker's file.

This is the guard for the core 3b Phase 1 invariant — at uvicorn_workers>1
each worker has its own SQLite WAL, and the readiness checks MUST see all
of them or they will report false-positives (e.g. INT-07 marking an
execution "missing attempt row" when the matching row is simply in a
different worker's file).
"""

from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

import pytest


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _create_wal(path: Path, executions: list[dict], attempts: list[dict]) -> None:
    """Create a WAL DB with the prod schema fields used by integrity checks."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE wal_records ("
        "execution_id TEXT, record_json TEXT, event_type TEXT, "
        "request_type TEXT, created_at TEXT, session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE gateway_attempts (request_id TEXT, execution_id TEXT, created_at TEXT)"
    )
    for e in executions:
        conn.execute(
            "INSERT INTO wal_records (execution_id, record_json, event_type, request_type, created_at, session_id) "
            "VALUES (?, ?, 'execution', NULL, ?, NULL)",
            (e["id"], json.dumps(e.get("record", {"record_signature": "x"})), e["created_at"]),
        )
    for a in attempts:
        conn.execute(
            "INSERT INTO gateway_attempts (request_id, execution_id, created_at) VALUES (?, ?, ?)",
            (a["request_id"], a["execution_id"], a["created_at"]),
        )
    conn.commit()
    conn.close()


def _make_ctx(wal_path: Path):
    """A minimal ctx whose wal_writer._path falls back to one file, but the
    multi-file union helper reads ALL wal*.db in wal_path via settings."""
    # _wal_paths falls back to ctx.wal_writer._path only when iter is empty;
    # to exercise the union path, point _path at one of the files (it must
    # exist for the fallback to be valid).
    legacy = wal_path / "wal-100.db"
    return types.SimpleNamespace(
        wal_writer=types.SimpleNamespace(_path=str(legacy)),
        walacor_client=None,
    )


@pytest.fixture
def two_worker_wals(tmp_path, monkeypatch):
    """Two per-worker WALs in tmp_path. Worker A has a fully-correlated
    execution; worker B has an execution missing its attempt row."""
    from gateway.config import get_settings as _g
    # Old creation time so the >30s grace in INT-07 admits all rows.
    old = "2026-05-19T00:00:00+00:00"

    _create_wal(
        tmp_path / "wal-100.db",
        executions=[{"id": "exec-A", "created_at": old}],
        attempts=[{"request_id": "rA", "execution_id": "exec-A", "created_at": old}],
    )
    _create_wal(
        tmp_path / "wal-200.db",
        executions=[{"id": "exec-B", "created_at": old}],
        attempts=[],  # missing attempt row — should surface in INT-07
    )

    # Clear cache so _g() rebuilds, then patch the now-cached instance so
    # subsequent get_settings() calls inside the check return our wal_path
    # (per CLAUDE.md: get_settings is lru_cache; patch the cached object,
    # don't clear after — that would throw away the patch).
    _g.cache_clear()
    s = _g()
    monkeypatch.setattr(s, "wal_path", str(tmp_path))
    yield tmp_path
    _g.cache_clear()  # tear down to avoid leaking the patched wal_path


def test_int07_aggregates_across_per_worker_wals(two_worker_wals):
    """INT-07 must report exactly 1/2 missing (the worker-B execution),
    proving the check walks both worker WAL files. Without the union the
    check would only see worker-A and report 1/1 green — a false pass."""
    from gateway.readiness.checks.integrity import _Int07AttemptCompleteness
    ctx = _make_ctx(two_worker_wals)
    result = _run(_Int07AttemptCompleteness().run(ctx))

    # Both executions are visible (cross-file union).
    assert "2" in result.detail, (
        f"INT-07 did not aggregate across worker WALs: {result.detail}"
    )
    # Exactly one is missing (exec-B in worker-200).
    assert "1/2" in result.detail or result.status == "red", (
        f"expected 1/2 missing, got: {result.detail}"
    )
    assert result.status == "red"


def test_int02_aggregates_signing_across_per_worker_wals(two_worker_wals):
    """INT-02 samples 'newest 50 execution records'. With per-worker WALs
    the sample must come from both files merged by created_at, not just
    one worker's file."""
    from gateway.readiness.checks.integrity import _Int02SigningActive
    ctx = _make_ctx(two_worker_wals)
    result = _run(_Int02SigningActive().run(ctx))
    # Both worker files contribute one signed execution each → 2 sampled.
    assert result.evidence.get("sampled") == 2, (
        f"INT-02 did not aggregate per-worker WALs: sampled={result.evidence}"
    )
    assert result.evidence.get("signed") == 2
    assert result.status == "green"
