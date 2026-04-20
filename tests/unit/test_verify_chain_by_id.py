"""verify_chain must walk record_id pointers and check sequence contiguity."""
from __future__ import annotations
import sqlite3
import tempfile
import json
import os
import pytest

from gateway.lineage.reader import LineageReader


def _make_db(records: list[dict]) -> str:
    """Create a temp WAL db seeded with execution records."""
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute("""
        CREATE TABLE wal_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id TEXT,
            session_id TEXT,
            sequence_number INTEGER,
            event_type TEXT DEFAULT 'execution',
            record_json TEXT,
            created_at TEXT DEFAULT '2026-01-01T00:00:00'
        )
    """)
    for r in records:
        conn.execute(
            "INSERT INTO wal_records (execution_id, session_id, sequence_number, event_type, record_json) VALUES (?,?,?,?,?)",
            (r.get("execution_id"), r.get("session_id"), r.get("sequence_number"), "execution", json.dumps(r)),
        )
    conn.commit()
    conn.close()
    return tmp


def _make_record(seq: int, record_id: str, prev_id: str | None, session: str = "s1") -> dict:
    return {
        "execution_id": f"exec-{seq}",
        "session_id": session,
        "sequence_number": seq,
        "record_id": record_id,
        "previous_record_id": prev_id,
        "timestamp": f"2026-01-0{seq+1}T00:00:00Z",
    }


def test_verify_chain_walks_id_pointers() -> None:
    records = [
        _make_record(0, "id-0", None),
        _make_record(1, "id-1", "id-0"),
        _make_record(2, "id-2", "id-1"),
    ]
    db = _make_db(records)
    try:
        reader = LineageReader(db)
        result = reader.verify_chain("s1")
        assert result["valid"] is True
        assert result["records_checked"] == 3
        assert result["errors"] == []
    finally:
        os.unlink(db)


def test_verify_chain_detects_broken_pointer() -> None:
    records = [
        _make_record(0, "id-0", None),
        _make_record(1, "id-1", "WRONG-POINTER"),  # should be "id-0"
        _make_record(2, "id-2", "id-1"),
    ]
    db = _make_db(records)
    try:
        reader = LineageReader(db)
        result = reader.verify_chain("s1")
        assert result["valid"] is False
        assert any("id pointer" in e.lower() or "mismatch" in e.lower() for e in result["errors"])
    finally:
        os.unlink(db)


def test_verify_chain_detects_sequence_gap() -> None:
    records = [
        _make_record(0, "id-0", None),
        _make_record(1, "id-1", "id-0"),
        _make_record(3, "id-3", "id-1"),  # seq 2 is missing
    ]
    db = _make_db(records)
    try:
        reader = LineageReader(db)
        result = reader.verify_chain("s1")
        assert result["valid"] is False
        assert any("sequence" in e.lower() or "gap" in e.lower() for e in result["errors"])
    finally:
        os.unlink(db)


def test_verify_chain_response_includes_walacor_attestation() -> None:
    records = [
        _make_record(0, "id-0", None),
        _make_record(1, "id-1", "id-0"),
    ]
    db = _make_db(records)
    try:
        reader = LineageReader(db)
        result = reader.verify_chain("s1")
        assert "walacor_attestation" in result
        assert len(result["walacor_attestation"]) == 2
        att = result["walacor_attestation"][0]
        assert "record_id" in att
        assert "walacor_block_id" in att
        assert "walacor_trans_id" in att
        assert "walacor_dh" in att
    finally:
        os.unlink(db)


def test_verify_chain_empty_session_is_valid() -> None:
    db = _make_db([])
    try:
        reader = LineageReader(db)
        result = reader.verify_chain("nonexistent")
        assert result["valid"] is True
        assert result["records_checked"] == 0
    finally:
        os.unlink(db)
