"""Pillar 1 — wire reconstruction message-diff engine.

Includes the two reference scenarios from
``docs/research/AGENT-TRACING-RESEARCH.md`` §12.6 (a 5-turn ReAct loop and a
parent + child sub-agent run) baked in as regression tests.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

import pytest

from gateway.pipeline.agent_reconstructor import (
    MessageDiffEngine,
    ReconstructionEvent,
    caller_key_for,
    reset_engine_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_engine():
    reset_engine_for_tests()
    yield
    reset_engine_for_tests()


# ── Engine fundamentals ──────────────────────────────────────────────────────


def test_first_request_emits_no_events():
    eng = MessageDiffEngine()
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]
    assert eng.observe("alice", msgs, time.time()) == []
    assert eng.stats.cache_misses == 1
    assert eng.stats.cache_entries == 1


def test_assistant_tool_call_diffed_into_event():
    eng = MessageDiffEngine()
    base = [{"role": "user", "content": "find flights"}]
    eng.observe("a", base, 0.0)
    follow = base + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search_flights", "arguments": '{"q":"sfo->jfk"}'},
                }
            ],
        }
    ]
    events = eng.observe("a", follow, 1.0)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "tool_call_observed"
    assert ev.tool_name == "search_flights"
    assert ev.tool_call_id == "call_1"
    assert ev.args_hash is not None


def test_tool_result_message_diffed_into_event():
    eng = MessageDiffEngine()
    base = [
        {"role": "user", "content": "find flights"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "search", "arguments": "{}"}}],
        },
    ]
    eng.observe("a", base, 0.0)
    follow = base + [{"role": "tool", "tool_call_id": "c1", "content": "result"}]
    events = eng.observe("a", follow, 0.5)
    kinds = [e.kind for e in events]
    assert kinds == ["tool_result_observed"]


# ── §12.6 reference scenario 1: 5-turn ReAct loop ─────────────────────────────


def _react_turn(messages, tool_name, args, result_text):
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(args)},
        }],
    })
    messages.append({"role": "tool", "tool_call_id": call_id, "content": result_text})
    return call_id


def test_five_turn_react_loop_reconstructs_each_step():
    """5 ReAct turns → 1 tool_call_observed + 1 tool_result_observed per turn = 10 events."""
    eng = MessageDiffEngine()
    base = [
        {"role": "system", "content": "agent system prompt"},
        {"role": "user", "content": "research X"},
    ]
    eng.observe("alice", list(base), 0.0)

    total_events = 0
    expected_kind_seq: list[str] = []
    for i in range(5):
        _react_turn(base, f"tool_{i}", {"step": i}, f"result_{i}")
        events = eng.observe("alice", list(base), float(i + 1))
        total_events += len(events)
        expected_kind_seq.extend(e.kind for e in events)

    assert total_events == 10
    assert expected_kind_seq == [
        "tool_call_observed", "tool_result_observed",
        "tool_call_observed", "tool_result_observed",
        "tool_call_observed", "tool_result_observed",
        "tool_call_observed", "tool_result_observed",
        "tool_call_observed", "tool_result_observed",
    ]
    assert eng.stats.cache_hits == 5  # one hit per follow-up
    assert eng.stats.evictions == 0


# ── §12.6 reference scenario 2: parent + child sub-agent ─────────────────────


def test_parent_and_child_subagent_run():
    """Parent emits a Task tool_call; child runs as a separate caller (different
    api key). Each caller's stream is reconstructed independently — cross-caller
    stitching is a Pillar 2 concern, not Pillar 1's."""
    eng = MessageDiffEngine()
    parent: list[dict] = [
        {"role": "system", "content": "coordinator"},
        {"role": "user", "content": "review repo"},
    ]
    child: list[dict] = [
        {"role": "system", "content": "code-reviewer"},
        {"role": "user", "content": "review src/auth/"},
    ]

    eng.observe("parent", list(parent), 0.0)
    parent.append({
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "task_1", "type": "function",
            "function": {"name": "Task", "arguments": '{"prompt":"review"}'},
        }],
    })
    p_events = eng.observe("parent", list(parent), 1.0)
    assert [e.kind for e in p_events] == ["tool_call_observed"]
    assert p_events[0].tool_name == "Task"

    # Child arrives as a different caller — first observation is a baseline.
    eng.observe("child", list(child), 1.5)
    child.append({
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "read_1", "type": "function",
            "function": {"name": "Read", "arguments": '{"path":"x.py"}'},
        }],
    })
    child.append({"role": "tool", "tool_call_id": "read_1", "content": "<file>"})
    c_events = eng.observe("child", list(child), 4.0)
    assert sorted(e.kind for e in c_events) == [
        "tool_call_observed", "tool_result_observed",
    ]

    # Parent receives the sub-agent's result (carried in the next turn).
    parent.append({"role": "tool", "tool_call_id": "task_1", "content": "2 findings"})
    p_events_2 = eng.observe("parent", list(parent), 8.0)
    assert [e.kind for e in p_events_2] == ["tool_result_observed"]


# ── Bounds: 500 KB ceiling, eviction, window ─────────────────────────────────


def test_oversize_payload_skipped_and_counts():
    eng = MessageDiffEngine(max_payload_bytes=1024)
    # one giant message
    msgs = [{"role": "user", "content": "x" * 5000}]
    assert eng.observe("alice", msgs, 0.0) == []
    assert eng.stats.oversize_skipped == 1
    # follow-up of normal size still works (caller starts fresh)
    assert eng.observe("alice", [{"role": "user", "content": "hi"}], 1.0) == []
    # Oversize requests are counted under oversize_skipped, not cache_misses,
    # so the only miss is the second (legitimate) baseline observation.
    assert eng.stats.cache_misses == 1
    assert eng.stats.oversize_skipped == 1


def test_byte_cap_evicts_oldest_caller():
    # Each payload is ~620 B serialised; cap of 1500 fits 2 entries, so the 3rd
    # and 4th observations must evict alice (and then bob).
    eng = MessageDiffEngine(max_bytes=1500, max_payload_bytes=10_000)
    msg = lambda who: [{"role": "user", "content": who * 600}]
    eng.observe("alice", msg("a"), 0.0)
    eng.observe("bob", msg("b"), 0.1)
    eng.observe("carol", msg("c"), 0.2)
    eng.observe("dave", msg("d"), 0.3)
    assert eng.stats.evictions >= 1


def test_window_expiry_does_not_diff_against_stale_entry():
    eng = MessageDiffEngine(window_seconds=10.0)
    eng.observe("a", [{"role": "user", "content": "1"}], 0.0)
    # second request 30s later — outside the window
    events = eng.observe(
        "a",
        [{"role": "user", "content": "1"}, {"role": "user", "content": "2"}],
        30.0,
    )
    assert events == []
    assert eng.stats.cache_misses == 2


# ── Caller key derivation ────────────────────────────────────────────────────


def test_caller_key_prefers_user_then_tenant_fallback():
    assert caller_key_for({"user": "u1"}, "t1") == "t1:user:u1"
    assert caller_key_for({"api_key_id": "k1"}, "t1") == "t1:api_key_id:k1"
    assert caller_key_for(None, "t1") == "t1:tenant"
    assert caller_key_for({}, None) == ":tenant"


# ── Persistence round-trip via WAL writer ────────────────────────────────────


def test_recon_event_persists_to_table(tmp_path):
    from gateway.wal.writer import WALWriter

    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    writer.start()
    try:
        writer.enqueue_write_recon_event({
            "event_id": "e1",
            "execution_id": "exec-1",
            "tenant_id": "t1",
            "caller_key": "t1:user:alice",
            "timestamp": "2026-01-01T00:00:00Z",
            "kind": "tool_call_observed",
            "tool_name": "search",
            "tool_call_id": "c1",
            "args_hash": "deadbeef",
            "trace_id": "abc",
            "agent_run_id": "run-1",
            "turn_seq": 2,
        })
        time.sleep(0.3)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM reconstructed_tool_events WHERE event_id=?", ("e1",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["kind"] == "tool_call_observed"
        assert row["tool_name"] == "search"
        assert row["caller_key"] == "t1:user:alice"
        assert row["source"] == "reconstructed"
    finally:
        writer.stop()


# ── Connections tile ─────────────────────────────────────────────────────────


def test_connections_tile_is_green_with_no_traffic():
    from gateway.connections.builder import build_agent_reconstructor_tile

    tile = build_agent_reconstructor_tile(ctx=object())
    assert tile["id"] == "agent_reconstructor"
    assert tile["status"] in {"green", "amber", "red"}
    assert "cache_bytes_now" in tile["detail"]
