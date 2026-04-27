"""Tier 0 persistence tests — agent-tracing columns survive a write+read trip."""

from __future__ import annotations

import sqlite3
import time

from gateway.wal.writer import WALWriter


def _read_attempt(db_path: str, request_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM gateway_attempts WHERE request_id=?", (request_id,)
    ).fetchone()
    conn.close()
    assert row is not None, "attempt row not found"
    return dict(row)


def test_attempt_row_has_agent_tracing_columns(tmp_path):
    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    try:
        writer.write_attempt(
            request_id="req-1",
            tenant_id="t1",
            path="/v1/chat/completions",
            disposition="forwarded",
            status_code=200,
            trace_id="0af7651916cd43dd8448eb211c80319c",
            parent_span_id="b7ad6b7169203331",
            agent_run_id="run-42",
            agent_name="code-reviewer",
            parent_observation_id="obs-7",
            parent_record_id="rec-prev",
            previous_response_id="resp_abc",
            conversation_id="conv_xyz",
        )
        row = _read_attempt(db_path, "req-1")
        assert row["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
        assert row["parent_span_id"] == "b7ad6b7169203331"
        assert row["agent_run_id"] == "run-42"
        assert row["agent_name"] == "code-reviewer"
        assert row["parent_observation_id"] == "obs-7"
        assert row["parent_record_id"] == "rec-prev"
        assert row["previous_response_id"] == "resp_abc"
        assert row["conversation_id"] == "conv_xyz"
    finally:
        writer.close()


def test_attempt_row_without_agent_tracing_keeps_columns_null(tmp_path):
    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    try:
        writer.write_attempt(
            request_id="req-2",
            tenant_id="t1",
            path="/v1/chat/completions",
            disposition="forwarded",
            status_code=200,
        )
        row = _read_attempt(db_path, "req-2")
        for col in (
            "trace_id", "parent_span_id", "agent_run_id", "agent_name",
            "parent_observation_id", "parent_record_id",
            "previous_response_id", "conversation_id",
        ):
            assert row[col] is None
    finally:
        writer.close()


def test_enqueue_write_attempt_persists_agent_tracing_fields(tmp_path):
    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    writer.start()
    try:
        writer.enqueue_write_attempt(
            request_id="req-3",
            tenant_id="t1",
            path="/v1/chat/completions",
            disposition="forwarded",
            status_code=200,
            trace_id="t" * 32,
            agent_run_id="run-99",
        )
        time.sleep(0.3)
        row = _read_attempt(db_path, "req-3")
        assert row["trace_id"] == "t" * 32
        assert row["agent_run_id"] == "run-99"
    finally:
        writer.stop()


def test_execution_record_promotes_agent_tracing_to_columns(tmp_path):
    """An execution record with metadata.{trace_id, agent_run_id, agent_name}
    should land in the wal_records hot columns so /v1/lineage queries can use
    the partial index instead of json_extract."""
    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    writer.start()
    try:
        writer.enqueue_write_execution({
            "execution_id": "exec-tier0",
            "session_id": "s1",
            "model_attestation_id": "m1",
            "timestamp": "2026-01-01T00:00:00Z",
            "metadata": {
                "trace_id": "abc" * 10 + "ab",  # 32 chars
                "agent_run_id": "run-7",
                "agent_name": "planner",
            },
        })
        time.sleep(0.3)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT trace_id, agent_run_id, agent_name FROM wal_records WHERE execution_id=?",
            ("exec-tier0",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["trace_id"] == "abc" * 10 + "ab"
        assert row["agent_run_id"] == "run-7"
        assert row["agent_name"] == "planner"
    finally:
        writer.stop()
