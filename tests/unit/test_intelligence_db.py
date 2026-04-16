from __future__ import annotations

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
