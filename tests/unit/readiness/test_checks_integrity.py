"""Integrity batch: INT-03…INT-07 green+red path tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


def _make_wal(tmp_path, records):
    """Create a WAL SQLite with a populated wal_records table. records: list of dicts."""
    db = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE wal_records (execution_id TEXT, record_json TEXT, "
        "event_type TEXT, session_id TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE gateway_attempts (request_id TEXT, execution_id TEXT, timestamp TEXT)"
    )
    for rec in records:
        conn.execute(
            "INSERT INTO wal_records VALUES (?, ?, 'execution', ?, ?)",
            (
                rec.get("execution_id", ""),
                json.dumps(rec),
                rec.get("session_id"),
                rec.get("_created_at", "2020-01-01T00:00:00"),
            ),
        )
    conn.commit()
    conn.close()
    return types.SimpleNamespace(_path=str(db)), db


def _ctx(**kw):
    d = dict(wal_writer=None, walacor_client=None, redis_client=None, tool_registry=None)
    d.update(kw)
    return types.SimpleNamespace(**d)


# ─── INT-03 ───────────────────────────────────────────────────────────────────

def test_int03_green(monkeypatch, tmp_path):
    """Signatures all verify."""
    monkeypatch.setattr("gateway.readiness.checks.integrity.signing_key_available", lambda: True)
    monkeypatch.setattr(
        "gateway.readiness.checks.integrity.verify_canonical",
        lambda **kw: True,
    )
    writer, _ = _make_wal(tmp_path, [
        {"execution_id": f"e{i}", "record_signature": "sig", "sequence_number": i,
         "record_id": f"r{i}", "previous_record_id": None, "timestamp": "2020"}
        for i in range(5)
    ])
    from gateway.readiness.checks.integrity import _Int03SignaturesVerify
    result = _run(_Int03SignaturesVerify().run(_ctx(wal_writer=writer)))
    assert result.status == "green", result.detail


def test_int03_red(monkeypatch, tmp_path):
    """Signatures fail to verify."""
    monkeypatch.setattr("gateway.readiness.checks.integrity.signing_key_available", lambda: True)
    monkeypatch.setattr(
        "gateway.readiness.checks.integrity.verify_canonical",
        lambda **kw: False,
    )
    writer, _ = _make_wal(tmp_path, [
        {"execution_id": f"e{i}", "record_signature": "sig", "sequence_number": i,
         "record_id": f"r{i}", "previous_record_id": None, "timestamp": "2020"}
        for i in range(3)
    ])
    from gateway.readiness.checks.integrity import _Int03SignaturesVerify
    result = _run(_Int03SignaturesVerify().run(_ctx(wal_writer=writer)))
    assert result.status == "red"


# ─── INT-04 ───────────────────────────────────────────────────────────────────

def test_int04_green(monkeypatch, tmp_path):
    class _S:
        walacor_storage_enabled = True
        walacor_executions_etid = 123
    monkeypatch.setattr("gateway.readiness.checks.integrity.get_settings", lambda: _S())
    writer, _ = _make_wal(tmp_path, [])

    async def _query(etid, pipeline):
        return [
            {"execution_id": f"e{i}", "BlockId": "b", "TransId": "t", "DH": "d"}
            for i in range(20)
        ]
    client = types.SimpleNamespace(query_complex=_query)
    from gateway.readiness.checks.integrity import _Int04WalacorAnchoringActive
    result = _run(_Int04WalacorAnchoringActive().run(_ctx(wal_writer=writer, walacor_client=client)))
    assert result.status == "green"


def test_int04_red(monkeypatch, tmp_path):
    class _S:
        walacor_storage_enabled = True
        walacor_executions_etid = 123
    monkeypatch.setattr("gateway.readiness.checks.integrity.get_settings", lambda: _S())
    writer, _ = _make_wal(tmp_path, [])

    async def _query(etid, pipeline):
        # Records written but not yet anchored — no BlockId/TransId/DH
        return [{"execution_id": f"e{i}"} for i in range(20)]
    client = types.SimpleNamespace(query_complex=_query)
    from gateway.readiness.checks.integrity import _Int04WalacorAnchoringActive
    result = _run(_Int04WalacorAnchoringActive().run(_ctx(wal_writer=writer, walacor_client=client)))
    assert result.status == "red"


# ─── INT-05 ───────────────────────────────────────────────────────────────────

def test_int05_green(monkeypatch, tmp_path):
    class _S:
        walacor_executions_etid = 123
    monkeypatch.setattr("gateway.readiness.checks.integrity.get_settings", lambda: _S())
    pick = {"execution_id": "e1", "walacor_block_id": "b", "walacor_trans_id": "t", "walacor_dh": "d"}
    writer, _ = _make_wal(tmp_path, [pick])

    async def _query(etid, pipeline):
        return [pick]

    client = types.SimpleNamespace(query_complex=_query)
    from gateway.readiness.checks.integrity import _Int05AnchorRoundTrip
    result = _run(_Int05AnchorRoundTrip().run(_ctx(wal_writer=writer, walacor_client=client)))
    assert result.status == "green"


def test_int05_red_mismatch(monkeypatch, tmp_path):
    class _S:
        walacor_executions_etid = 123
    monkeypatch.setattr("gateway.readiness.checks.integrity.get_settings", lambda: _S())
    local = {"execution_id": "e1", "walacor_block_id": "b1", "walacor_trans_id": "t1", "walacor_dh": "d1"}
    remote = {"execution_id": "e1", "walacor_block_id": "b2", "walacor_trans_id": "t1", "walacor_dh": "d1"}
    writer, _ = _make_wal(tmp_path, [local])

    async def _query(etid, pipeline):
        return [remote]
    client = types.SimpleNamespace(query_complex=_query)
    from gateway.readiness.checks.integrity import _Int05AnchorRoundTrip
    result = _run(_Int05AnchorRoundTrip().run(_ctx(wal_writer=writer, walacor_client=client)))
    assert result.status == "red"


# ─── INT-06 ───────────────────────────────────────────────────────────────────

def test_int06_green(monkeypatch, tmp_path):
    writer, _ = _make_wal(tmp_path, [
        {"execution_id": "e1", "session_id": "s1"},
        {"execution_id": "e2", "session_id": "s1"},
    ])

    def _fake_verify(self, session_id):
        return {"errors": [], "records_checked": 2}

    monkeypatch.setattr(
        "gateway.lineage.reader.LineageReader.verify_chain",
        _fake_verify,
        raising=False,
    )
    from gateway.readiness.checks.integrity import _Int06ChainContinuity
    result = _run(_Int06ChainContinuity().run(_ctx(wal_writer=writer)))
    assert result.status == "green"


def test_int06_red(monkeypatch, tmp_path):
    writer, _ = _make_wal(tmp_path, [
        {"execution_id": "e1", "session_id": "s1"},
        {"execution_id": "e2", "session_id": "s1"},
    ])

    def _fake_verify(self, session_id):
        return {"errors": ["sequence gap at record 1"], "records_checked": 2}

    monkeypatch.setattr(
        "gateway.lineage.reader.LineageReader.verify_chain",
        _fake_verify,
        raising=False,
    )
    from gateway.readiness.checks.integrity import _Int06ChainContinuity
    result = _run(_Int06ChainContinuity().run(_ctx(wal_writer=writer)))
    assert result.status == "red"


# ─── INT-07 ───────────────────────────────────────────────────────────────────

def test_int07_green(tmp_path):
    """All executions have matching attempt rows.

    Uses ISO-8601 timestamps (with 'T' separator) — the real wal writer format.
    This test catches the regression where INT-07's SQL tried to compare
    ISO-format strings directly to datetime('now') output, which has a space
    separator that sorts differently under lexicographic comparison.
    """
    import datetime as _dt
    old_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=60)).isoformat()
    db = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE wal_records (execution_id TEXT, record_json TEXT, event_type TEXT, created_at TEXT, session_id TEXT, request_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE gateway_attempts (request_id TEXT, execution_id TEXT, timestamp TEXT)"
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO wal_records VALUES (?, '{}', 'execution', ?, NULL, NULL)",
            (f"e{i}", old_iso),
        )
        conn.execute(
            "INSERT INTO gateway_attempts VALUES (?, ?, '2020')",
            (f"r{i}", f"e{i}"),
        )
    conn.commit()
    conn.close()
    writer = types.SimpleNamespace(_path=str(db))
    from gateway.readiness.checks.integrity import _Int07AttemptCompleteness
    result = _run(_Int07AttemptCompleteness().run(_ctx(wal_writer=writer)))
    assert result.status == "green"


def test_int07_red_missing(tmp_path):
    """Executions older than 30s with NO matching attempt rows → red."""
    import datetime as _dt
    old_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=60)).isoformat()
    db = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE wal_records (execution_id TEXT, record_json TEXT, event_type TEXT, created_at TEXT, session_id TEXT, request_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE gateway_attempts (request_id TEXT, execution_id TEXT, timestamp TEXT)"
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO wal_records VALUES (?, '{}', 'execution', ?, NULL, NULL)",
            (f"e{i}", old_iso),
        )
    # No gateway_attempts rows
    conn.commit()
    conn.close()
    writer = types.SimpleNamespace(_path=str(db))
    from gateway.readiness.checks.integrity import _Int07AttemptCompleteness
    result = _run(_Int07AttemptCompleteness().run(_ctx(wal_writer=writer)))
    assert result.status == "red"


def test_int08_green_when_all_models_present(tmp_path):
    from gateway.intelligence.registry import ModelRegistry
    from gateway.readiness.checks.integrity import _Int08ProductionModelsPresent

    reg = ModelRegistry(base_path=str(tmp_path / "models"))
    reg.ensure_structure()
    (reg.base / "production" / "intent.onnx").write_bytes(b"x")
    (reg.base / "production" / "safety.onnx").write_bytes(b"y")

    result = _run(_Int08ProductionModelsPresent().run(_ctx(model_registry=reg)))
    assert result.status == "green"


def test_int08_red_when_file_missing(tmp_path, monkeypatch):
    from gateway.intelligence.registry import ModelRegistry
    from gateway.readiness.checks.integrity import _Int08ProductionModelsPresent

    reg = ModelRegistry(base_path=str(tmp_path / "models"))
    reg.ensure_structure()
    monkeypatch.setattr(reg, "list_production_models", lambda: ["intent"])

    result = _run(_Int08ProductionModelsPresent().run(_ctx(model_registry=reg)))
    assert result.status == "red"
    assert "unhealthy" in result.detail
    assert any(u["status"] == "missing" for u in result.evidence["unhealthy"])


def test_int08_amber_when_no_registry():
    from gateway.readiness.checks.integrity import _Int08ProductionModelsPresent
    result = _run(_Int08ProductionModelsPresent().run(_ctx()))
    assert result.status == "amber"


def test_int07_amber_race_window(tmp_path):
    """Executions newer than 30s are excluded (racing with attempt write)."""
    import datetime as _dt
    # Only young records — the 30s filter should exclude them all → amber
    young_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    db = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE wal_records (execution_id TEXT, record_json TEXT, event_type TEXT, created_at TEXT, session_id TEXT, request_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE gateway_attempts (request_id TEXT, execution_id TEXT, timestamp TEXT)"
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO wal_records VALUES (?, '{}', 'execution', ?, NULL, NULL)",
            (f"e{i}", young_iso),
        )
    conn.commit()
    conn.close()
    writer = types.SimpleNamespace(_path=str(db))
    from gateway.readiness.checks.integrity import _Int07AttemptCompleteness
    result = _run(_Int07AttemptCompleteness().run(_ctx(wal_writer=writer)))
    assert result.status == "amber"
    assert "older than 30s" in result.detail
