"""Tests for drift-to-audit hook (§4.6)."""

from __future__ import annotations

import json
import sqlite3
import types

import pytest


def _make_writer(tmp_path):
    """Build a real WALWriter against a fresh DB so we can inspect the row it writes."""
    from gateway.wal.writer import WALWriter
    writer = WALWriter(str(tmp_path / "wal.db"))
    # Force schema creation
    writer._ensure_conn()
    return writer


def _ctx(writer=None):
    return types.SimpleNamespace(wal_writer=writer)


def _result(status, detail="broken"):
    from gateway.readiness.protocol import CheckResult
    return CheckResult(status=status, detail=detail)


def _reset():
    from gateway.readiness.drift_audit import reset_rate_limit
    reset_rate_limit()


def test_drift_writes_attempt_row(tmp_path):
    """A sec/int check flipping green→red produces a gateway_attempts row with disposition=readiness_degraded."""
    _reset()
    writer = _make_writer(tmp_path)
    from gateway.readiness.drift_audit import maybe_write_drift_record

    wrote = maybe_write_drift_record(
        "INT-02", _result("red", "0/50 signed"), previous_status="green", ctx=_ctx(writer),
    )
    assert wrote is True

    # Inspect the DB directly
    conn = sqlite3.connect(str(tmp_path / "wal.db"))
    rows = conn.execute(
        "SELECT disposition, path, reason FROM gateway_attempts"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    disp, path, reason = rows[0]
    assert disp == "readiness_degraded"
    assert path == "/v1/readiness"

    # Metadata is JSON-encoded in reason (§4.6)
    meta = json.loads(reason)
    assert meta["check_id"] == "INT-02"
    assert meta["detail"] == "0/50 signed"
    assert meta["previous_status"] == "green"


def test_drift_skips_when_not_red(tmp_path):
    _reset()
    writer = _make_writer(tmp_path)
    from gateway.readiness.drift_audit import maybe_write_drift_record

    assert maybe_write_drift_record("INT-02", _result("amber"), "green", _ctx(writer)) is False
    assert maybe_write_drift_record("INT-02", _result("green"), "red", _ctx(writer)) is False


def test_drift_skips_when_already_red(tmp_path):
    """Consecutive red states don't spam the WAL (only the transition matters)."""
    _reset()
    writer = _make_writer(tmp_path)
    from gateway.readiness.drift_audit import maybe_write_drift_record

    assert maybe_write_drift_record("INT-02", _result("red"), "red", _ctx(writer)) is False


def test_drift_rate_limit_5min(tmp_path, monkeypatch):
    """Second write within 5 minutes is rate-limited."""
    _reset()
    writer = _make_writer(tmp_path)
    from gateway.readiness.drift_audit import maybe_write_drift_record

    # First transition
    assert maybe_write_drift_record("INT-02", _result("red"), "green", _ctx(writer)) is True
    # Simulated second transition 4 minutes later (still under rate limit)
    import gateway.readiness.drift_audit as mod
    # Back-date the recorded time by 4 minutes
    orig = mod._last_written["INT-02"]
    mod._last_written["INT-02"] = orig - 240  # 4 min ago
    assert maybe_write_drift_record("INT-02", _result("red"), "green", _ctx(writer)) is False

    # Simulated 6 minutes later → should fire
    mod._last_written["INT-02"] = orig - 360
    assert maybe_write_drift_record("INT-02", _result("red"), "green", _ctx(writer)) is True


def test_drift_no_writer_is_noop(tmp_path):
    _reset()
    from gateway.readiness.drift_audit import maybe_write_drift_record
    assert maybe_write_drift_record("INT-02", _result("red"), "green", _ctx(None)) is False


def test_drift_writer_exception_swallowed(tmp_path):
    """If the writer raises, we log and return False — never propagate."""
    _reset()

    class _BadWriter:
        def write_attempt(self, **kw):
            raise RuntimeError("disk full")
    from gateway.readiness.drift_audit import maybe_write_drift_record
    # Should not raise
    assert maybe_write_drift_record("INT-02", _result("red"), "green", _ctx(_BadWriter())) is False


def test_drift_only_fires_for_sec_int_via_runner(tmp_path, monkeypatch):
    """Full-path: runner invokes drift_audit only when severity is sec/int."""
    _reset()
    writer = _make_writer(tmp_path)

    from gateway.readiness.protocol import Category, CheckResult, Severity

    class _WarnRedCheck:
        id = "WARN-X"
        name = "warn-sev red check"
        category = Category.hygiene
        severity = Severity.warn
        async def run(self, ctx):
            return CheckResult(status="red", detail="warn-red")

    import gateway.readiness.registry as reg_mod
    import gateway.readiness.runner as runner_mod
    original = reg_mod._REGISTERED[:]
    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.append(_WarnRedCheck())
    runner_mod._cache = None
    runner_mod._cache_lock_obj = None
    runner_mod._previous_statuses.clear()

    import asyncio
    try:
        asyncio.run(runner_mod.run_all(_ctx(writer), fresh=True))
    finally:
        reg_mod._REGISTERED.clear()
        reg_mod._REGISTERED.extend(original)
        runner_mod._cache = None

    # No drift row should have been written for a warn-severity red.
    conn = sqlite3.connect(str(tmp_path / "wal.db"))
    count = conn.execute("SELECT COUNT(*) FROM gateway_attempts").fetchone()[0]
    conn.close()
    assert count == 0, "Warn-severity red must NOT trigger drift audit"
