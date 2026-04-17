"""Phase 25 Task 15: Safety harvester — LlamaGuard as teacher.

When both SafetyClassifier (student, ONNX) and LlamaGuard (teacher, LLM)
run on the same response, compare their categories. If they disagree,
LlamaGuard's category is the training signal we back-write onto the
SafetyClassifier's verdict row. Agreement contributes nothing — the
classifier already got it right.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.harvesters import HarvesterSignal
from gateway.intelligence.harvesters.safety import (
    LLAMA_GUARD_TO_SAFETY_LABEL,
    SafetyHarvester,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_db(tmp_path: Path) -> IntelligenceDB:
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    return db


def _insert_verdict(db: IntelligenceDB, *, model: str, request_id: str) -> int:
    conn = sqlite3.connect(db.path)
    try:
        cur = conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            "confidence, request_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                model, "h" * 64, "{}", "safe", 0.9, request_id,
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


def _signal(request_id: str | None, decisions: list[dict]) -> HarvesterSignal:
    return HarvesterSignal(
        request_id=request_id,
        model_name="safety",
        prediction="safe",
        response_payload={"analyzer_decisions": decisions},
        context={},
    )


def _safety_decision(category: str, *, verdict: str = "PASS", confidence: float = 0.9) -> dict:
    return {
        "analyzer_id": "truzenai.safety.v1",
        "verdict": verdict,
        "confidence": confidence,
        "category": category,
        "reason": "test",
    }


def _llama_decision(category: str, *, verdict: str = "PASS", confidence: float = 0.95) -> dict:
    return {
        "analyzer_id": "walacor.llama_guard.v3",
        "verdict": verdict,
        "confidence": confidence,
        "category": category,
        "reason": "test",
    }


# ── mapping table ───────────────────────────────────────────────────────────

def test_mapping_covers_llama_guard_safe_label():
    # When LlamaGuard returns PASS, its decision.category is the literal
    # "safety" — needs to normalize to SafetyClassifier's "safe" label.
    assert LLAMA_GUARD_TO_SAFETY_LABEL["safety"] == "safe"


def test_mapping_covers_core_unsafe_labels():
    # Spot-check: the big-ticket violence/sex/self-harm/child mappings
    # MUST exist or the training signal loses the most valuable rows.
    assert LLAMA_GUARD_TO_SAFETY_LABEL["violent_crimes"] == "violence"
    assert LLAMA_GUARD_TO_SAFETY_LABEL["sex_crimes"] == "sexual"
    assert LLAMA_GUARD_TO_SAFETY_LABEL["sexual_content"] == "sexual"
    assert LLAMA_GUARD_TO_SAFETY_LABEL["self_harm"] == "self_harm"
    assert LLAMA_GUARD_TO_SAFETY_LABEL["child_safety"] == "child_safety"
    assert LLAMA_GUARD_TO_SAFETY_LABEL["hate_discrimination"] == "hate_speech"
    assert LLAMA_GUARD_TO_SAFETY_LABEL["indiscriminate_weapons"] == "dangerous"
    assert LLAMA_GUARD_TO_SAFETY_LABEL["nonviolent_crimes"] == "criminal"


# ── harvester happy path ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_disagreement_writes_llama_label(tmp_path):
    # Classifier says safety (pass); LlamaGuard flags violence. Signal = "violence".
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="safety", request_id="r1")

    h = SafetyHarvester(db)
    sig = _signal("r1", [
        _safety_decision("safety"),
        _llama_decision("violent_crimes", verdict="WARN"),
    ])
    await h.process(sig)

    s, src = _read_divergence(db, vid)
    assert s == "violence"
    assert src == "llama_guard_disagreement"


@pytest.mark.anyio
async def test_disagreement_uses_normalized_labels(tmp_path):
    # Classifier correctly flags "sexual"; LlamaGuard returns "sexual_content"
    # which maps to "sexual" — that's agreement post-normalization, no signal.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="safety", request_id="r2")

    h = SafetyHarvester(db)
    sig = _signal("r2", [
        _safety_decision("sexual", verdict="WARN"),
        _llama_decision("sexual_content", verdict="WARN"),
    ])
    await h.process(sig)

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_safe_agreement_no_signal(tmp_path):
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="safety", request_id="r3")

    h = SafetyHarvester(db)
    sig = _signal("r3", [
        _safety_decision("safety"),
        _llama_decision("safety"),
    ])
    await h.process(sig)

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_false_negative_classifier_flagged_by_teacher(tmp_path):
    # Classifier said safe, teacher flags — the most important training case.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="safety", request_id="r4")

    h = SafetyHarvester(db)
    sig = _signal("r4", [
        _safety_decision("safety"),
        _llama_decision("child_safety", verdict="BLOCK"),
    ])
    await h.process(sig)

    s, src = _read_divergence(db, vid)
    assert s == "child_safety"
    assert src == "llama_guard_disagreement"


# ── harvester skip paths ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_missing_llama_guard_no_signal(tmp_path):
    # Classifier ran, teacher didn't (LlamaGuard disabled / not installed).
    # Nothing to compare against — no signal.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="safety", request_id="r5")

    h = SafetyHarvester(db)
    sig = _signal("r5", [_safety_decision("violence", verdict="WARN")])
    await h.process(sig)

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_missing_safety_classifier_no_signal(tmp_path):
    # Only the teacher ran — the harvester has no student verdict to
    # compare against. Skip.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="safety", request_id="r6")

    h = SafetyHarvester(db)
    sig = _signal("r6", [_llama_decision("violent_crimes", verdict="BLOCK")])
    await h.process(sig)

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_llama_guard_failopen_skipped(tmp_path):
    # LlamaGuard fell open (confidence=0.0 with reason="timeout"/"parse_error").
    # Never treat that as a teacher signal — the classifier hasn't actually
    # been "corrected" by anyone.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="safety", request_id="r7")

    h = SafetyHarvester(db)
    sig = _signal("r7", [
        _safety_decision("violence", verdict="WARN"),
        _llama_decision("safety", confidence=0.0),  # fail-open
    ])
    await h.process(sig)

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_unmappable_llama_category_skipped(tmp_path):
    # LlamaGuard flagged "elections" — no equivalent in SafetyClassifier's
    # 8-label vocab, so this is not learnable as SafetyClassifier training
    # signal. Skip without writing anything.
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="safety", request_id="r8")

    h = SafetyHarvester(db)
    sig = _signal("r8", [
        _safety_decision("safety"),
        _llama_decision("elections", verdict="WARN"),
    ])
    await h.process(sig)

    s, _ = _read_divergence(db, vid)
    assert s is None


@pytest.mark.anyio
async def test_null_request_id_noop(tmp_path):
    db = _make_db(tmp_path)
    _insert_verdict(db, model="safety", request_id="whatever")

    h = SafetyHarvester(db)
    sig = _signal(None, [
        _safety_decision("safety"),
        _llama_decision("violent_crimes", verdict="BLOCK"),
    ])
    await h.process(sig)

    conn = sqlite3.connect(db.path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM onnx_verdicts "
            "WHERE model_name='safety' AND divergence_signal IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0


@pytest.mark.anyio
async def test_updates_all_safety_rows_for_request(tmp_path):
    # Two safety verdicts with the same request_id (e.g. streaming +
    # trailing post-stream analysis, or a retry). Plan's UPDATE does NOT
    # pin to "latest only" — it updates every safety row. Verify both.
    db = _make_db(tmp_path)
    vid_a = _insert_verdict(db, model="safety", request_id="r9")
    vid_b = _insert_verdict(db, model="safety", request_id="r9")

    h = SafetyHarvester(db)
    sig = _signal("r9", [
        _safety_decision("safety"),
        _llama_decision("violent_crimes", verdict="BLOCK"),
    ])
    await h.process(sig)

    assert _read_divergence(db, vid_a)[0] == "violence"
    assert _read_divergence(db, vid_b)[0] == "violence"


@pytest.mark.anyio
async def test_does_not_touch_other_models(tmp_path):
    # Intent verdict with same request_id must remain untouched.
    db = _make_db(tmp_path)
    intent_vid = _insert_verdict(db, model="intent", request_id="r10")
    safety_vid = _insert_verdict(db, model="safety", request_id="r10")

    h = SafetyHarvester(db)
    sig = _signal("r10", [
        _safety_decision("safety"),
        _llama_decision("violent_crimes", verdict="BLOCK"),
    ])
    await h.process(sig)

    assert _read_divergence(db, intent_vid)[0] is None
    assert _read_divergence(db, safety_vid)[0] == "violence"


def test_target_model_constant():
    assert SafetyHarvester.target_model == "safety"
