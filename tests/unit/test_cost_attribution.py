"""Unit tests for cost attribution: pricing CRUD, cost computation, and cost summary."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from gateway.control.store import ControlPlaneStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    """Create a temp ControlPlaneStore, yield it, then cleanup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "control.db")
        s = ControlPlaneStore(db_path)
        yield s
        s.close()


@pytest.fixture
def wal_db():
    """Create a temp WAL database with wal_records table for LineageReader tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "wal.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wal_records (
                execution_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                delivered_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gateway_attempts (
                request_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                tenant_id TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                model_id TEXT DEFAULT '',
                path TEXT DEFAULT '',
                disposition TEXT DEFAULT 'forwarded',
                execution_id TEXT DEFAULT '',
                status_code INTEGER DEFAULT 200,
                user TEXT DEFAULT ''
            )
        """)
        conn.commit()
        conn.close()
        yield db_path


# ---------------------------------------------------------------------------
# Pricing CRUD tests
# ---------------------------------------------------------------------------

def test_upsert_and_list_pricing(store: ControlPlaneStore):
    """Create pricing entries and list them."""
    result = store.upsert_model_pricing({
        "model_pattern": "gpt-4*",
        "input_cost_per_1k": 0.03,
        "output_cost_per_1k": 0.06,
        "currency": "USD",
    })
    assert "pricing_id" in result
    assert result["model_pattern"] == "gpt-4*"
    assert result["input_cost_per_1k"] == 0.03
    assert result["output_cost_per_1k"] == 0.06

    result2 = store.upsert_model_pricing({
        "model_pattern": "claude-3*",
        "input_cost_per_1k": 0.015,
        "output_cost_per_1k": 0.075,
    })
    assert result2["model_pattern"] == "claude-3*"

    rows = store.list_model_pricing()
    assert len(rows) == 2
    patterns = {r["model_pattern"] for r in rows}
    assert patterns == {"gpt-4*", "claude-3*"}


def test_upsert_pricing_updates_on_conflict(store: ControlPlaneStore):
    """Upserting the same model_pattern updates costs."""
    store.upsert_model_pricing({
        "model_pattern": "gpt-4*",
        "input_cost_per_1k": 0.03,
        "output_cost_per_1k": 0.06,
    })
    store.upsert_model_pricing({
        "model_pattern": "gpt-4*",
        "input_cost_per_1k": 0.02,
        "output_cost_per_1k": 0.04,
    })
    rows = store.list_model_pricing()
    assert len(rows) == 1
    assert rows[0]["input_cost_per_1k"] == 0.02
    assert rows[0]["output_cost_per_1k"] == 0.04


def test_get_model_pricing_exact_match(store: ControlPlaneStore):
    """Exact model_pattern match works."""
    store.upsert_model_pricing({
        "model_pattern": "qwen3:4b",
        "input_cost_per_1k": 0.001,
        "output_cost_per_1k": 0.002,
    })
    pricing = store.get_model_pricing("qwen3:4b")
    assert pricing is not None
    assert pricing["model_pattern"] == "qwen3:4b"
    assert pricing["input_cost_per_1k"] == 0.001


def test_get_model_pricing_wildcard(store: ControlPlaneStore):
    """Wildcard fnmatch pattern matches model_id."""
    store.upsert_model_pricing({
        "model_pattern": "gpt-4*",
        "input_cost_per_1k": 0.03,
        "output_cost_per_1k": 0.06,
    })
    pricing = store.get_model_pricing("gpt-4-turbo")
    assert pricing is not None
    assert pricing["model_pattern"] == "gpt-4*"

    pricing2 = store.get_model_pricing("gpt-4o")
    assert pricing2 is not None
    assert pricing2["model_pattern"] == "gpt-4*"


def test_get_model_pricing_no_match(store: ControlPlaneStore):
    """Returns None when no pattern matches."""
    store.upsert_model_pricing({
        "model_pattern": "gpt-4*",
        "input_cost_per_1k": 0.03,
        "output_cost_per_1k": 0.06,
    })
    pricing = store.get_model_pricing("claude-3-opus")
    assert pricing is None


def test_delete_pricing(store: ControlPlaneStore):
    """Delete removes pricing entry."""
    result = store.upsert_model_pricing({
        "model_pattern": "gpt-4*",
        "input_cost_per_1k": 0.03,
        "output_cost_per_1k": 0.06,
    })
    pricing_id = result["pricing_id"]
    assert store.delete_model_pricing(pricing_id) is True
    rows = store.list_model_pricing()
    assert len(rows) == 0


def test_delete_pricing_nonexistent(store: ControlPlaneStore):
    """Delete of non-existent pricing returns False."""
    assert store.delete_model_pricing("nonexistent-id") is False


# ---------------------------------------------------------------------------
# Cost computation tests
# ---------------------------------------------------------------------------

def test_cost_computation():
    """Verify cost formula: prompt * input/1000 + completion * output/1000."""
    prompt_tokens = 500
    completion_tokens = 200
    input_cost_per_1k = 0.03
    output_cost_per_1k = 0.06

    cost = (
        prompt_tokens * input_cost_per_1k / 1000
        + completion_tokens * output_cost_per_1k / 1000
    )
    assert round(cost, 6) == 0.027


def test_cost_computation_zero_tokens():
    """Zero tokens should produce zero cost."""
    prompt_tokens = 0
    completion_tokens = 0
    input_cost_per_1k = 0.03
    output_cost_per_1k = 0.06

    cost = (
        prompt_tokens * input_cost_per_1k / 1000
        + completion_tokens * output_cost_per_1k / 1000
    )
    assert cost == 0.0


# ---------------------------------------------------------------------------
# LineageReader cost summary tests
# ---------------------------------------------------------------------------

def test_cost_summary_reader_by_model(wal_db):
    """Test get_cost_summary grouped by model."""
    from gateway.lineage.reader import LineageReader

    # Insert test records into wal_records
    conn = sqlite3.connect(wal_db)
    records = [
        {
            "execution_id": "exec-1",
            "model_id": "gpt-4",
            "model_attestation_id": "att-1",
            "timestamp": "2099-01-01T00:00:00+00:00",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "estimated_cost_usd": 0.006,
        },
        {
            "execution_id": "exec-2",
            "model_id": "gpt-4",
            "model_attestation_id": "att-1",
            "timestamp": "2099-01-01T00:01:00+00:00",
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
            "estimated_cost_usd": 0.012,
        },
        {
            "execution_id": "exec-3",
            "model_id": "claude-3",
            "model_attestation_id": "att-2",
            "timestamp": "2099-01-01T00:02:00+00:00",
            "prompt_tokens": 50,
            "completion_tokens": 25,
            "total_tokens": 75,
            "estimated_cost_usd": 0.003,
        },
    ]
    for rec in records:
        conn.execute(
            "INSERT INTO wal_records (execution_id, record_json, created_at) VALUES (?, ?, ?)",
            (rec["execution_id"], json.dumps(rec), "2099-01-01T00:00:00"),
        )
    conn.commit()
    conn.close()

    reader = LineageReader(wal_db)
    try:
        result = reader.get_cost_summary(range_key="30d", group_by="model")
        assert result["range"] == "30d"
        assert result["group_by"] == "model"
        assert len(result["entries"]) == 2

        # gpt-4 should be first (higher cost)
        gpt4_entry = next(e for e in result["entries"] if e["model"] == "gpt-4")
        assert gpt4_entry["request_count"] == 2
        assert gpt4_entry["prompt_tokens"] == 300
        assert gpt4_entry["completion_tokens"] == 150
        assert gpt4_entry["total_tokens"] == 450
        assert gpt4_entry["cost_usd"] == 0.018

        claude_entry = next(e for e in result["entries"] if e["model"] == "claude-3")
        assert claude_entry["request_count"] == 1
        assert claude_entry["cost_usd"] == 0.003

        assert result["grand_total_usd"] == 0.021
    finally:
        reader.close()


def test_cost_summary_reader_by_user(wal_db):
    """Test get_cost_summary grouped by user."""
    from gateway.lineage.reader import LineageReader

    conn = sqlite3.connect(wal_db)
    records = [
        {
            "execution_id": "exec-u1",
            "model_id": "gpt-4",
            "user": "alice",
            "timestamp": "2099-01-01T00:00:00+00:00",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "estimated_cost_usd": 0.006,
        },
        {
            "execution_id": "exec-u2",
            "model_id": "gpt-4",
            "user": "bob",
            "timestamp": "2099-01-01T00:01:00+00:00",
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
            "estimated_cost_usd": 0.012,
        },
    ]
    for rec in records:
        conn.execute(
            "INSERT INTO wal_records (execution_id, record_json, created_at) VALUES (?, ?, ?)",
            (rec["execution_id"], json.dumps(rec), "2099-01-01T00:00:00"),
        )
    conn.commit()
    conn.close()

    reader = LineageReader(wal_db)
    try:
        result = reader.get_cost_summary(range_key="30d", group_by="user")
        assert result["group_by"] == "user"
        assert len(result["entries"]) == 2

        bob_entry = next(e for e in result["entries"] if e["user"] == "bob")
        assert bob_entry["cost_usd"] == 0.012

        alice_entry = next(e for e in result["entries"] if e["user"] == "alice")
        assert alice_entry["cost_usd"] == 0.006
    finally:
        reader.close()


def test_cost_summary_reader_empty(wal_db):
    """Test get_cost_summary with no records returns empty entries."""
    from gateway.lineage.reader import LineageReader

    reader = LineageReader(wal_db)
    try:
        result = reader.get_cost_summary(range_key="24h", group_by="model")
        assert result["entries"] == []
        assert result["grand_total_usd"] == 0.0
    finally:
        reader.close()


def test_cost_summary_excludes_tool_events(wal_db):
    """Tool event records (event_type=tool_call) should be excluded from cost summary."""
    from gateway.lineage.reader import LineageReader

    conn = sqlite3.connect(wal_db)
    # Regular execution record
    exec_record = {
        "execution_id": "exec-1",
        "model_id": "gpt-4",
        "timestamp": "2099-01-01T00:00:00+00:00",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "estimated_cost_usd": 0.006,
    }
    # Tool event (should be excluded)
    tool_record = {
        "execution_id": "exec-1",
        "event_id": "tool-1",
        "event_type": "tool_call",
        "model_id": "gpt-4",
        "timestamp": "2099-01-01T00:00:01+00:00",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    conn.execute(
        "INSERT INTO wal_records (execution_id, record_json, created_at) VALUES (?, ?, ?)",
        ("exec-1", json.dumps(exec_record), "2099-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO wal_records (execution_id, record_json, created_at) VALUES (?, ?, ?)",
        ("tool-1", json.dumps(tool_record), "2099-01-01T00:00:01"),
    )
    conn.commit()
    conn.close()

    reader = LineageReader(wal_db)
    try:
        result = reader.get_cost_summary(range_key="30d", group_by="model")
        assert len(result["entries"]) == 1
        assert result["entries"][0]["request_count"] == 1
        assert result["grand_total_usd"] == 0.006
    finally:
        reader.close()
