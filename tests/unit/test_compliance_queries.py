"""Unit tests for LineageReader compliance query methods."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest
from gateway.core import compute_sha3_512_string

from gateway.lineage.reader import LineageReader


_GENESIS_HASH = "0" * 128


def _create_wal_db(db_path: str):
    from gateway.wal.writer import _apply_schema
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    _apply_schema(conn)
    conn.commit()
    return conn


def _compute_hash(execution_id, policy_version, policy_result, prev_hash, seq, timestamp):
    canonical = "|".join([
        execution_id, str(policy_version), policy_result, prev_hash, str(seq), timestamp,
    ])
    return compute_sha3_512_string(canonical)


def _insert_chained_records(conn, session_id: str, count: int = 3,
                            model_id: str = "qwen3:4b", provider: str = "ollama",
                            policy_result: str = "pass",
                            date_prefix: str = "2026-03-05"):
    prev_hash = _GENESIS_HASH
    records = []
    for i in range(count):
        eid = f"exec-{session_id}-{i}"
        ts = f"{date_prefix}T10:00:{i:02d}+00:00"
        record_hash = _compute_hash(eid, 1, policy_result, prev_hash, i, ts)
        metadata = {
            "response_policy_result": "skipped",
            "analyzer_decisions": [],
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
        record = {
            "execution_id": eid,
            "session_id": session_id,
            "model_attestation_id": f"self-attested:{model_id}",
            "model_id": model_id,
            "provider": provider,
            "policy_version": 1,
            "policy_result": policy_result,
            "tenant_id": "test-tenant",
            "gateway_id": "gw-test",
            "timestamp": ts,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "latency_ms": 200.0 + i * 50,
            "sequence_number": i,
            "record_hash": record_hash,
            "previous_record_hash": prev_hash,
            "metadata": metadata,
        }
        conn.execute(
            """INSERT INTO wal_records
               (execution_id, record_json, created_at, event_type, session_id,
                timestamp, model_id, provider, prompt_tokens, completion_tokens,
                total_tokens, latency_ms, sequence_number, policy_result)
               VALUES (?, ?, ?, 'execution', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, json.dumps(record), ts, session_id, ts, model_id, provider,
             100, 50, 150, 200.0 + i * 50, i, policy_result),
        )
        prev_hash = record_hash
        records.append(record)
    conn.commit()
    return records


def _insert_attempts(conn, count: int, date_prefix: str = "2026-03-05",
                     dispositions: list[str] | None = None):
    if dispositions is None:
        dispositions = ["allowed"] * count
    for i in range(count):
        disp = dispositions[i] if i < len(dispositions) else "allowed"
        conn.execute(
            """INSERT INTO gateway_attempts
               (request_id, timestamp, tenant_id, provider, model_id, path, disposition, execution_id, status_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"req-{date_prefix}-{i}", f"{date_prefix}T10:00:{i:02d}+00:00", "test-tenant",
             "ollama", "qwen3:4b", "/v1/chat/completions", disp,
             f"exec-{i}" if disp == "allowed" else None,
             200 if disp == "allowed" else 403),
        )
    conn.commit()


@pytest.fixture
def wal_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "wal.db")
        conn = _create_wal_db(db_path)
        yield db_path, conn
        conn.close()


# ---------------------------------------------------------------------------
# get_compliance_summary
# ---------------------------------------------------------------------------

def test_compliance_summary_counts_by_disposition(wal_db):
    """Compliance summary correctly counts allowed/denied/blocked."""
    db_path, conn = wal_db
    _insert_attempts(conn, 5, "2026-03-05",
                     dispositions=["allowed", "allowed", "allowed", "denied_auth", "denied_policy"])
    _insert_chained_records(conn, "sess-a", count=3, date_prefix="2026-03-05")

    reader = LineageReader(db_path)
    summary = reader.get_compliance_summary("2026-03-01", "2026-03-10")
    reader.close()

    assert summary["total_requests"] == 5
    assert summary["allowed"] == 3
    assert summary["denied"] == 2


def test_compliance_summary_empty_range(wal_db):
    """Empty date range returns zeroed stats."""
    db_path, conn = wal_db
    _insert_attempts(conn, 3, "2026-03-05")

    reader = LineageReader(db_path)
    summary = reader.get_compliance_summary("2026-01-01", "2026-01-02")
    reader.close()

    assert summary["total_requests"] == 0
    assert summary["allowed"] == 0
    assert summary["denied"] == 0


# ---------------------------------------------------------------------------
# get_execution_export
# ---------------------------------------------------------------------------

def test_execution_export_respects_date_range(wal_db):
    """Only returns records in the specified date range."""
    db_path, conn = wal_db
    _insert_chained_records(conn, "sess-march", count=3, date_prefix="2026-03-05")
    _insert_chained_records(conn, "sess-feb", count=2, date_prefix="2026-02-15")

    reader = LineageReader(db_path)
    exports = reader.get_execution_export("2026-03-01", "2026-03-10")
    reader.close()

    assert len(exports) == 3
    for ex in exports:
        assert ex["session_id"] == "sess-march"


def test_execution_export_limit(wal_db):
    """Honors the limit parameter."""
    db_path, conn = wal_db
    _insert_chained_records(conn, "sess-big", count=5, date_prefix="2026-03-05")

    reader = LineageReader(db_path)
    exports = reader.get_execution_export("2026-03-01", "2026-03-10", limit=2)
    reader.close()

    assert len(exports) == 2


# ---------------------------------------------------------------------------
# get_attestation_summary
# ---------------------------------------------------------------------------

def test_attestation_summary_groups_by_model(wal_db):
    """Groups records by model_id with request counts."""
    db_path, conn = wal_db
    _insert_chained_records(conn, "sess-q", count=3, model_id="qwen3:4b",
                            date_prefix="2026-03-05")
    _insert_chained_records(conn, "sess-g", count=2, model_id="gpt-4o", provider="openai",
                            date_prefix="2026-03-05")

    reader = LineageReader(db_path)
    atts = reader.get_attestation_summary("2026-03-01", "2026-03-10")
    reader.close()

    assert len(atts) == 2
    by_model = {a["model_id"]: a for a in atts}
    assert by_model["qwen3:4b"]["request_count"] == 3
    assert by_model["gpt-4o"]["request_count"] == 2
    assert by_model["gpt-4o"]["provider"] == "openai"


# ---------------------------------------------------------------------------
# get_chain_verification_report
# ---------------------------------------------------------------------------

def test_chain_verification_report_includes_all_sessions(wal_db):
    """Runs verify for each active session in the period."""
    db_path, conn = wal_db
    _insert_chained_records(conn, "sess-v1", count=3, date_prefix="2026-03-05")
    _insert_chained_records(conn, "sess-v2", count=2, date_prefix="2026-03-05")
    # Outside range — should not appear
    _insert_chained_records(conn, "sess-old", count=2, date_prefix="2026-01-15")

    reader = LineageReader(db_path)
    report = reader.get_chain_verification_report("2026-03-01", "2026-03-10")
    reader.close()

    assert len(report) == 2
    session_ids = {r["session_id"] for r in report}
    assert "sess-v1" in session_ids
    assert "sess-v2" in session_ids
    assert "sess-old" not in session_ids
    for r in report:
        assert r["valid"] is True
