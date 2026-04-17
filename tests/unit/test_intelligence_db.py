from __future__ import annotations

import sqlite3

import pytest

from gateway.intelligence.db import IntelligenceDB


def test_db_creates_tables(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    tables = db.list_tables()
    assert "onnx_verdicts" in tables
    assert "shadow_comparisons" in tables
    assert "training_snapshots" in tables


def test_db_is_idempotent(tmp_path):
    p = str(tmp_path / "int.db")
    IntelligenceDB(p).init_schema()
    IntelligenceDB(p).init_schema()  # second call must not raise


def test_list_tables_hides_sqlite_internals(tmp_path):
    # AUTOINCREMENT columns cause SQLite to eagerly create `sqlite_sequence` at
    # DDL time; callers doing equality comparisons or iterating for DDL shouldn't
    # see internal bookkeeping tables.
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    assert set(db.list_tables()) == {
        "onnx_verdicts",
        "shadow_comparisons",
        "training_snapshots",
        "lifecycle_events_mirror",
    }


def test_unique_constraint_on_training_dataset_hash(tmp_path):
    db = IntelligenceDB(str(tmp_path / "int.db"))
    db.init_schema()
    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            "INSERT INTO training_snapshots (model_name, dataset_hash, row_ids_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("intent", "abc123", "[1,2,3]", "2026-04-16T00:00:00+00:00"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO training_snapshots (model_name, dataset_hash, row_ids_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("intent", "abc123", "[4,5]", "2026-04-16T00:00:01+00:00"),
            )
            conn.commit()
    finally:
        conn.close()
