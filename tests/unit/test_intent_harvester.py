"""Phase 25 Task 16: Intent harvester tests.

Three signal sources:
  1. Immediate — classification vs. actual tool activity on same turn.
  2. Deferred (next-turn) — user's follow-up message contradicts prior label.
  3. Sampled teacher LLM — external classifier relabels at low sample rate.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.harvesters import HarvesterSignal
from gateway.intelligence.harvesters.intent import IntentHarvester


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_db(tmp_path: Path) -> IntelligenceDB:
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    return db


def _insert_verdict(db: IntelligenceDB, *, request_id: str, prediction: str = "normal") -> int:
    conn = sqlite3.connect(db.path)
    try:
        cur = conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            "confidence, request_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "intent", "h" * 64, "{}", prediction, 0.9, request_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _read_divergence(db: IntelligenceDB, vid: int) -> tuple[str | None, str | None]:
    conn = sqlite3.connect(db.path)
    try:
        row = conn.execute(
            "SELECT divergence_signal, divergence_source FROM onnx_verdicts WHERE id=?",
            (vid,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        conn.close()


def _signal(
    request_id: str | None,
    prediction: str,
    *,
    session_id: str | None = None,
    prompt: str = "",
    tool_events: list | None = None,
) -> HarvesterSignal:
    payload = {"tool_events_detail": tool_events or []}
    ctx = {"session_id": session_id, "prompt": prompt}
    return HarvesterSignal(
        request_id=request_id,
        model_name="intent",
        prediction=prediction,
        response_payload=payload,
        context=ctx,
    )


# ── Signal 1: Immediate action check ────────────────────────────────────────

@pytest.mark.anyio
async def test_web_search_classification_no_tool_signals_false_positive(tmp_path):
    # Classified web_search but nothing was actually searched — classic FP.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, request_id="r1", prediction="web_search")

    h = IntentHarvester(db)
    await h.process(_signal("r1", "web_search", tool_events=[]))

    s, src = _read_divergence(db, vid)
    assert s == "normal"
    assert src == "immediate_action_mismatch"


@pytest.mark.anyio
async def test_web_search_with_tool_called_no_signal(tmp_path):
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, request_id="r2", prediction="web_search")

    h = IntentHarvester(db)
    tool_events = [{"tool_name": "web_search", "tool_type": "builtin"}]
    await h.process(_signal("r2", "web_search", tool_events=tool_events))

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_normal_prediction_no_immediate_check(tmp_path):
    # "normal" has no expected action; the harvester cannot infer a
    # divergence from the immediate signal alone.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, request_id="r3", prediction="normal")

    h = IntentHarvester(db)
    await h.process(_signal("r3", "normal", tool_events=[]))

    s, _ = _read_divergence(db, vid)
    assert s is None


# ── Signal 2: Next-turn contradiction ────────────────────────────────────────

@pytest.mark.anyio
async def test_next_turn_search_keyword_flags_prior_normal(tmp_path):
    # Turn 1: prior="normal"
    # Turn 2: user says "actually, search for X" → prior was a missed web_search.
    db = _make_db(tmp_path)
    prior_vid = _insert_verdict(db, request_id="prior", prediction="normal")

    h = IntentHarvester(db)
    # Turn 1: classify, remember as pending (no divergence yet)
    await h.process(_signal("prior", "normal", session_id="s1", prompt="tell me about X"))
    s_after_turn_1, _ = _read_divergence(db, prior_vid)
    assert s_after_turn_1 is None

    # Turn 2: follow-up with a clear web-search hint.
    # Insert current-turn row so the harvester can be tested end-to-end
    # (even though the deferred signal targets the PRIOR row).
    _insert_verdict(db, request_id="cur", prediction="web_search")
    await h.process(_signal(
        "cur", "web_search", session_id="s1",
        prompt="actually, search for the latest news about X",
    ))

    prior_sig, prior_src = _read_divergence(db, prior_vid)
    assert prior_sig == "web_search"
    assert prior_src == "next_turn_contradiction"


@pytest.mark.anyio
async def test_next_turn_matches_prior_no_signal(tmp_path):
    # Prior correctly classified web_search AND the tool was called (so the
    # immediate-action check is satisfied); follow-up is neutral
    # acknowledgement. Nothing should flag the prior row.
    db = _make_db(tmp_path)
    prior_vid = _insert_verdict(db, request_id="prior", prediction="web_search")
    prior_tools = [{"tool_name": "web_search", "tool_type": "builtin"}]
    h = IntentHarvester(db)
    await h.process(_signal(
        "prior", "web_search", session_id="s2",
        prompt="search for X", tool_events=prior_tools,
    ))

    _insert_verdict(db, request_id="cur", prediction="normal")
    await h.process(_signal("cur", "normal", session_id="s2", prompt="thanks, that helps"))

    s, _ = _read_divergence(db, prior_vid)
    assert s is None


@pytest.mark.anyio
async def test_no_session_id_means_no_next_turn_state(tmp_path):
    # Without a session we can't link turns — the harvester can't emit a
    # next-turn signal, but must still run the immediate check.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, request_id="r1", prediction="web_search")
    h = IntentHarvester(db)
    await h.process(_signal("r1", "web_search", session_id=None, prompt="anything", tool_events=[]))

    s, src = _read_divergence(db, vid)
    # Immediate check still fires — empty tool events + web_search label → normal.
    assert s == "normal"
    assert src == "immediate_action_mismatch"


# ── Signal 3: Sampled teacher LLM ────────────────────────────────────────────

@pytest.mark.anyio
async def test_teacher_sample_called_when_under_rate(tmp_path, monkeypatch):
    # Force sampling to fire (rate=1.0 so random() < rate is always true).
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, request_id="rt1", prediction="normal")

    fake_client = MagicMock()
    fake_client.post = AsyncMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": "web_search"}}],
    }
    fake_client.post.return_value = fake_resp

    h = IntentHarvester(
        db,
        teacher_url="http://teacher.example/v1/chat/completions",
        teacher_sample_rate=1.0,
        http_client=fake_client,
    )
    await h.process(_signal("rt1", "normal", prompt="look up current weather"))

    s, src = _read_divergence(db, vid)
    assert s == "web_search"
    assert src == "teacher_llm"


@pytest.mark.anyio
async def test_teacher_sample_skipped_when_over_rate(tmp_path):
    # Rate 0 — teacher must never be called.
    db = _make_db(tmp_path)
    _insert_verdict(db, request_id="rt2", prediction="normal")

    fake_client = MagicMock()
    fake_client.post = AsyncMock()

    h = IntentHarvester(
        db, teacher_url="http://teacher.example",
        teacher_sample_rate=0.0, http_client=fake_client,
    )
    await h.process(_signal("rt2", "normal", prompt="hello"))
    assert not fake_client.post.called


@pytest.mark.anyio
async def test_teacher_agreement_no_signal(tmp_path):
    # Teacher returns the same label the classifier picked → not a
    # divergence; no back-write.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, request_id="rt3", prediction="normal")

    fake_client = MagicMock()
    fake_client.post = AsyncMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": "normal"}}],
    }
    fake_client.post.return_value = fake_resp

    h = IntentHarvester(
        db, teacher_url="http://teacher.example",
        teacher_sample_rate=1.0, http_client=fake_client,
    )
    await h.process(_signal("rt3", "normal", prompt="hello"))

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_teacher_network_failure_is_fail_open(tmp_path):
    # Teacher call raises — harvester must not propagate the error.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, request_id="rt4", prediction="normal")

    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=RuntimeError("boom"))

    h = IntentHarvester(
        db, teacher_url="http://teacher.example",
        teacher_sample_rate=1.0, http_client=fake_client,
    )
    # Must not raise.
    await h.process(_signal("rt4", "normal", prompt="hello"))

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_teacher_invalid_label_rejected(tmp_path):
    # Teacher returns something that isn't a known intent label —
    # treat as failed parse, emit no divergence.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, request_id="rt5", prediction="normal")

    fake_client = MagicMock()
    fake_client.post = AsyncMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": "absolutely-not-an-intent-label"}}],
    }
    fake_client.post.return_value = fake_resp

    h = IntentHarvester(
        db, teacher_url="http://teacher.example",
        teacher_sample_rate=1.0, http_client=fake_client,
    )
    await h.process(_signal("rt5", "normal", prompt="hello"))

    s, _ = _read_divergence(db, vid)
    assert s is None


# ── Null / edge cases ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_null_request_id_is_noop(tmp_path):
    db = _make_db(tmp_path)
    _insert_verdict(db, request_id="anything", prediction="web_search")

    h = IntentHarvester(db)
    await h.process(_signal(None, "web_search", tool_events=[]))

    conn = sqlite3.connect(db.path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM onnx_verdicts "
            "WHERE model_name='intent' AND divergence_signal IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0


@pytest.mark.anyio
async def test_pending_store_is_bounded(tmp_path):
    # Flood sessions well past the cap — oldest entries must evict so the
    # in-memory state can't grow without bound.
    db = _make_db(tmp_path)
    h = IntentHarvester(db, max_pending=4)

    for i in range(10):
        await h.process(_signal(f"r{i}", "normal", session_id=f"s{i}", prompt="q"))

    assert len(h._pending) <= 4


def test_target_model_constant():
    assert IntentHarvester.target_model == "intent"
