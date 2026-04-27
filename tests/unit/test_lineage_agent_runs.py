"""Dashboard backend — lineage reader + classification cascade."""

from __future__ import annotations

import json
import sqlite3
import time

from gateway.lineage.api import classify_execution_intent
from gateway.lineage.reader import LineageReader


# ── §9.4 cascade ──────────────────────────────────────────────────────────────


def test_classify_trace_id_is_agent_run_step():
    out = classify_execution_intent({"metadata": {"trace_id": "a" * 32}})
    assert out["intent"] == "agent_run_step"
    assert "trace" in out["reason"].lower()


def test_classify_agent_run_id_is_agent_run_step():
    out = classify_execution_intent({"metadata": {"agent_run_id": "run-1"}})
    assert out["intent"] == "agent_run_step"


def test_classify_responses_api_chain():
    out = classify_execution_intent({"metadata": {"previous_response_id": "resp_x"}})
    assert out["intent"] == "agent_run_step"


def test_classify_tool_role_in_request_messages_is_loop_step():
    out = classify_execution_intent({"metadata": {
        "_request_messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "x", "content": "result"},
        ],
    }})
    assert out["intent"] == "tool_loop_step"


def test_classify_response_tool_calls_is_tool_call_emitted():
    out = classify_execution_intent({"metadata": {
        "response_tool_calls": [{"id": "c", "function": {"name": "search"}}],
    }})
    assert out["intent"] == "tool_call_emitted"


def test_classify_plain_chat_default():
    out = classify_execution_intent({"metadata": {}})
    assert out["intent"] == "chat"


# ── Reader endpoints ─────────────────────────────────────────────────────────


def test_list_agent_runs_returns_empty_on_fresh_db(tmp_path):
    """Old WALs without the Pillar 4 migration applied yet must surface a
    clean empty list, not 500."""
    from gateway.wal.writer import WALWriter

    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    try:
        writer.write_attempt(
            request_id="r-1", tenant_id="t1",
            path="/v1/chat/completions", disposition="forwarded", status_code=200,
        )
    finally:
        writer.close()
    reader = LineageReader(db_path)
    try:
        assert reader.list_agent_runs() == []
        assert reader.count_agent_runs() == 0
        assert reader.get_agent_run("missing") is None
    finally:
        reader.close()


def test_agent_run_round_trip(tmp_path):
    from gateway.wal.writer import WALWriter

    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    writer.start()
    try:
        manifest = {
            "run_id": "r-abc", "tenant_id": "t1", "trace_id": "trace-xyz",
            "start_ts": "2026-04-26T00:00:00Z", "end_ts": "2026-04-26T00:01:30Z",
            "end_reason": "explicit_close",
            "caller_identity": {"user": "alice"},
            "framework_guess": {"name": "openai-agents-sdk", "confidence": 0.95},
            "llm_calls": [{"record_id": "exec-1", "walacor_dh": None,
                           "model": "gpt-4o", "timestamp": "t"}],
            "reconstructed_tool_events": [],
            "message_chain_hash": "deadbeef",
            "intent_drift_score": None,
            "signature": "sig-bytes",
        }
        writer.enqueue_write_agent_run_manifest(manifest)
        writer.enqueue_write_recon_event({
            "event_id": "ev-1", "execution_id": "exec-1",
            "tenant_id": "t1", "caller_key": "ck",
            "timestamp": "2026-04-26T00:00:30Z",
            "kind": "tool_call_observed",
            "tool_name": "search", "tool_call_id": "tc1",
            "args_hash": "aaaa", "content_hash": None,
            "trace_id": "trace-xyz", "agent_run_id": "r-abc",
            "turn_seq": 1, "source": "reconstructed",
        })
        time.sleep(0.3)
    finally:
        writer.stop()

    reader = LineageReader(db_path)
    try:
        runs = reader.list_agent_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "r-abc"
        assert runs[0]["framework_name"] == "openai-agents-sdk"
        assert runs[0]["llm_call_count"] == 1
        assert runs[0]["signed"] is True

        detail = reader.get_agent_run("r-abc")
        assert detail is not None
        assert detail["trace_id"] == "trace-xyz"
        assert len(detail["reconstructed_tool_events"]) == 1
        ev = detail["reconstructed_tool_events"][0]
        assert ev["kind"] == "tool_call_observed"
        assert ev["tool_name"] == "search"
    finally:
        reader.close()
