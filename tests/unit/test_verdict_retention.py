from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.retention import RetentionSweeper

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _insert_verdict(db_path: str, timestamp_iso: str, input_hash: str = "x") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, confidence, "
            "request_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("intent", input_hash, "{}", "normal", 0.9, None, timestamp_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_shadow(db_path: str, timestamp_iso: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO shadow_comparisons "
            "(model_name, candidate_version, input_hash, production_prediction, "
            "production_confidence, candidate_prediction, candidate_confidence, "
            "candidate_error, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("intent", "v1", "x", "normal", 0.9, "normal", 0.85, None, timestamp_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _count(db_path: str, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


async def test_sweeper_deletes_old_verdicts(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=45)).isoformat()
    new = (now - timedelta(days=5)).isoformat()
    _insert_verdict(db.path, old, input_hash="old")
    _insert_verdict(db.path, new, input_hash="new")

    sweeper = RetentionSweeper(db, retention_days=30, sweep_interval_s=0.01)
    task = asyncio.create_task(sweeper.run())
    await asyncio.sleep(0.05)
    sweeper.stop()
    await task

    assert _count(db.path, "onnx_verdicts") == 1
    with sqlite3.connect(db.path) as conn:
        remaining = conn.execute("SELECT input_hash FROM onnx_verdicts").fetchone()[0]
    assert remaining == "new"


async def test_sweeper_deletes_old_shadow_comparisons(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=45)).isoformat()
    new = (now - timedelta(days=5)).isoformat()
    _insert_shadow(db.path, old)
    _insert_shadow(db.path, new)

    sweeper = RetentionSweeper(db, retention_days=30, sweep_interval_s=0.01)
    task = asyncio.create_task(sweeper.run())
    await asyncio.sleep(0.05)
    sweeper.stop()
    await task

    assert _count(db.path, "shadow_comparisons") == 1


async def test_sweeper_survives_db_error(tmp_path, monkeypatch, caplog):
    """Broken sweep must not crash the loop."""
    import logging
    caplog.set_level(logging.ERROR)

    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    sweeper = RetentionSweeper(db, retention_days=30, sweep_interval_s=0.01)

    calls = {"n": 0}
    original = sweeper._sweep_once

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("simulated failure")
        return original()

    monkeypatch.setattr(sweeper, "_sweep_once", flaky)

    task = asyncio.create_task(sweeper.run())
    await asyncio.sleep(0.05)
    sweeper.stop()
    await task

    assert any("simulated failure" in str(r.message) or "retention sweep" in str(r.message).lower()
               for r in caplog.records)


async def test_sweeper_with_empty_db(tmp_path):
    """Sweeping an empty DB must not raise."""
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    sweeper = RetentionSweeper(db, retention_days=30, sweep_interval_s=0.01)
    task = asyncio.create_task(sweeper.run())
    await asyncio.sleep(0.05)
    sweeper.stop()
    await task
    assert _count(db.path, "onnx_verdicts") == 0
    assert _count(db.path, "shadow_comparisons") == 0
