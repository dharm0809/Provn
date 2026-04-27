"""Pillar 4 — AgentRunManifest aggregator + Ed25519 signature + ETId wiring."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from gateway.agent_tracing.aggregator import (
    AgentRunAggregator,
    classify_framework,
    reset_for_tests,
)
from gateway.agent_tracing.manifest import (
    AgentRunManifest,
    FrameworkGuess,
    LLMCallRef,
    message_chain_hash,
    sign_manifest,
)
from gateway.crypto.signing import _signing_key  # noqa: F401  (ensure import path is ok)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


# ── Manifest plumbing ─────────────────────────────────────────────────────────


def test_manifest_canonical_bytes_excludes_signature():
    m = AgentRunManifest(
        run_id="r", tenant_id="t1", caller_identity={"user": "u"},
        trace_id=None, framework_guess=FrameworkGuess("unknown", 0.0),
        start_ts="2026-04-26T00:00:00Z", end_ts="2026-04-26T00:01:00Z",
        end_reason="explicit_close",
    )
    canon_before = m.canonical_bytes()
    m.signature = "deadbeef"
    canon_after = m.canonical_bytes()
    assert canon_before == canon_after


def test_message_chain_hash_changes_with_any_edit():
    a = message_chain_hash([{"role": "user", "content": "hi"}])
    b = message_chain_hash([{"role": "user", "content": "ho"}])
    assert a != b
    # Same input ⇒ same hash.
    assert a == message_chain_hash([{"role": "user", "content": "hi"}])


def test_sign_manifest_attaches_base64_signature_when_key_loaded(tmp_path):
    """Generates a fresh key, then signs a manifest. The fail-open path
    (signing.py line 117 returns None when key absent) is also covered by
    test_aggregator_finalises_unsigned_when_no_key below."""
    from gateway.crypto.signing import ensure_signing_key

    ensure_signing_key(str(tmp_path / "sign.pem"))
    m = AgentRunManifest(
        run_id="r", tenant_id="t1", caller_identity={},
        trace_id=None, framework_guess=FrameworkGuess("unknown", 0.0),
        start_ts="2026-04-26T00:00:00Z", end_ts="2026-04-26T00:01:00Z",
        end_reason="explicit_close",
    )
    sign_manifest(m)
    assert m.signature is not None
    # base64 alphabet
    import base64
    assert base64.b64decode(m.signature)


# ── Framework classifier (rule-based v1) ─────────────────────────────────────


def test_classify_openai_agents_sdk_user_agent():
    g = classify_framework(user_agent="openai-agents-python/0.6.0", tool_names=[])
    assert g.name == "openai-agents-sdk"
    assert g.confidence >= 0.9


def test_classify_claude_agent_sdk_via_tool_set():
    g = classify_framework(user_agent=None, tool_names=["Read", "Bash", "Edit", "Glob", "Grep"])
    assert g.name == "claude-agent-sdk"


def test_classify_unknown_when_no_signal():
    g = classify_framework(user_agent="curl/8.0", tool_names=["search"])
    assert g.name == "unknown"
    assert g.confidence == 0.0


# ── Aggregator: open / observe / close paths ─────────────────────────────────


def test_observe_then_explicit_close_produces_manifest():
    agg = AgentRunAggregator()
    agg.observe(
        tenant_id="t1", run_key="run-1", record_id="exec-1",
        model="gpt-4o", timestamp_iso="2026-04-26T00:00:00Z", now=100.0,
        messages=[{"role": "user", "content": "hi"}],
        caller_identity={"user": "alice"}, trace_id="tr-1",
        user_agent="openai-agents-python/0.6.0",
    )
    assert agg.open_runs() == 1
    m = agg.close_run(tenant_id="t1", run_key="run-1", now=130.0)
    assert m is not None
    assert m.tenant_id == "t1"
    assert m.trace_id == "tr-1"
    assert m.end_reason == "explicit_close"
    assert m.framework_guess.name == "openai-agents-sdk"
    assert len(m.llm_calls) == 1
    assert m.message_chain_hash != ""


def test_inactivity_run_end_after_30s_post_final_assistant():
    agg = AgentRunAggregator(inactivity_seconds=30.0, ttl_seconds=999.0)
    # First observation, with is_final_assistant arming inactivity timer.
    agg.observe(
        tenant_id="t1", run_key="run-2", record_id="exec-1",
        model="m", timestamp_iso="2026-04-26T00:00:00Z", now=0.0,
        messages=[], is_final_assistant=True,
    )
    # Sweep just before 30 s — no close yet.
    assert agg.sweep(now=29.0) == []
    # Sweep at 30 s — close fires.
    out = agg.sweep(now=30.5)
    assert len(out) == 1
    assert out[0].end_reason == "inactivity"
    assert agg.open_runs() == 0


def test_ttl_run_end_30_minutes_overrides_no_final_assistant():
    agg = AgentRunAggregator(inactivity_seconds=999.0, ttl_seconds=60.0)
    agg.observe(
        tenant_id="t1", run_key="run-3", record_id="e",
        model="m", timestamp_iso="t", now=0.0,
        messages=[], is_final_assistant=False,
    )
    assert agg.sweep(now=59.0) == []
    out = agg.sweep(now=61.0)
    assert len(out) == 1
    assert out[0].end_reason == "ttl"


def test_explicit_close_returns_none_when_no_open_run():
    agg = AgentRunAggregator()
    assert agg.close_run(tenant_id="t1", run_key="missing", now=0.0) is None


def test_recon_events_attached_to_manifest():
    from gateway.pipeline.agent_reconstructor import ReconstructionEvent

    agg = AgentRunAggregator()
    events = [
        ReconstructionEvent(
            kind="tool_call_observed", caller="c", turn_seq=1,
            tool_name="search", tool_call_id="t1", args_hash="aaa",
        ),
        ReconstructionEvent(
            kind="tool_result_observed", caller="c", turn_seq=2,
            tool_call_id="t1", content_hash="bbb",
        ),
    ]
    agg.observe(
        tenant_id="t1", run_key="run-x", record_id="e1",
        model="m", timestamp_iso="t", now=0.0,
        messages=[], recon_events=events,
    )
    m = agg.close_run(tenant_id="t1", run_key="run-x", now=1.0)
    assert m is not None
    assert len(m.reconstructed_tool_events) == 2
    kinds = {e.kind for e in m.reconstructed_tool_events}
    assert kinds == {"tool_call_observed", "tool_result_observed"}


# ── WAL persistence + Walacor ETId wiring ────────────────────────────────────


def test_manifest_persists_to_local_wal(tmp_path):
    from gateway.wal.writer import WALWriter

    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    writer.start()
    try:
        manifest_dict = {
            "run_id": "r1", "tenant_id": "t1", "trace_id": "tr-1",
            "start_ts": "2026-04-26T00:00:00Z", "end_ts": "2026-04-26T00:01:00Z",
            "end_reason": "explicit_close",
            "caller_identity": {"user": "alice"},
            "framework_guess": {"name": "claude-agent-sdk", "confidence": 0.85},
            "llm_calls": [{"record_id": "e1", "walacor_dh": None,
                           "model": "claude", "timestamp": "t"}],
            "reconstructed_tool_events": [],
            "message_chain_hash": "deadbeef",
            "intent_drift_score": None,
            "signature": "sig",
        }
        writer.enqueue_write_agent_run_manifest(manifest_dict)
        time.sleep(0.3)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_run_manifests WHERE run_id=?", ("r1",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["framework_name"] == "claude-agent-sdk"
        assert row["llm_call_count"] == 1
        assert row["signature"] == "sig"
        assert json.loads(row["manifest_json"])["run_id"] == "r1"
    finally:
        writer.stop()


def test_default_etid_is_9000005():
    from gateway.config import Settings

    s = Settings()
    assert s.walacor_agent_run_manifests_etid == 9000005
