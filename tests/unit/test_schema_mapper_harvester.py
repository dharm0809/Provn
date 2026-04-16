"""Phase 25 Task 14: SchemaMapper harvester — overflow-key training signal.

The harvester turns overflow keys captured at audit time into a training
label for the distillation worker. For each unclassified field whose path
matches a canonical-fallback rule, we back-write the rule's label onto
the corresponding `onnx_verdicts` row via `divergence_signal`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.harvesters import HarvesterSignal
from gateway.intelligence.harvesters.schema_mapper import SchemaMapperHarvester
from gateway.schema.mapper import classify_overflow_path


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
                model, "h" * 64, "{}", "complete", 0.9, request_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _read_divergence(db: IntelligenceDB, verdict_id: int) -> tuple[str | None, str | None]:
    conn = sqlite3.connect(db.path)
    try:
        row = conn.execute(
            "SELECT divergence_signal, divergence_source FROM onnx_verdicts WHERE id=?",
            (verdict_id,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        conn.close()


def _signal(request_id: str | None, overflow_keys: list[str]) -> HarvesterSignal:
    payload = {"canonical": {"overflow_keys": overflow_keys}}
    return HarvesterSignal(
        request_id=request_id,
        model_name="schema_mapper",
        prediction="complete",
        response_payload=payload,
        context={},
    )


# ── classify_overflow_path ─────────────────────────────────────────────────

def test_classify_overflow_path_matches_leaf_and_path_tokens():
    # `content` leaf in a path that contains "content" token → content label.
    assert classify_overflow_path("weird_wrapper.message.content") == "content"


def test_classify_overflow_path_matches_case_insensitive_leaf():
    # Ollama-style camelCase `completionReason` → finish_reason via
    # rule (("completion",), "completionReason", "finish_reason").
    assert classify_overflow_path("choices[0].completionReason") == "finish_reason"


def test_classify_overflow_path_returns_none_on_no_rule():
    # Random unrelated field — no rule matches.
    assert classify_overflow_path("weirdo.fooKey") is None


def test_classify_overflow_path_none_on_empty_path():
    assert classify_overflow_path("") is None


def test_classify_overflow_path_strips_trailing_index():
    # Leaf with a `[0]` suffix must still match the rule table.
    assert classify_overflow_path("choices.content[0]") == "content"


# ── SchemaMapperHarvester.process ───────────────────────────────────────────

@pytest.mark.anyio
async def test_harvester_sets_divergence_when_overflow_key_matches_rule(tmp_path):
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="schema_mapper", request_id="req-1")

    harvester = SchemaMapperHarvester(db)
    signal = _signal("req-1", ["weird_wrapper.message.content"])
    await harvester.process(signal)

    sig, src = _read_divergence(db, vid)
    assert sig == "content"
    assert src == "schema_overflow_fallback"


@pytest.mark.anyio
async def test_harvester_no_overflow_keys_is_noop(tmp_path):
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="schema_mapper", request_id="req-2")

    harvester = SchemaMapperHarvester(db)
    await harvester.process(_signal("req-2", []))

    sig, src = _read_divergence(db, vid)
    assert sig is None
    assert src is None


@pytest.mark.anyio
async def test_harvester_no_matching_rule_is_noop(tmp_path):
    db = _make_db(tmp_path)
    vid = _insert_verdict(db, model="schema_mapper", request_id="req-3")

    harvester = SchemaMapperHarvester(db)
    await harvester.process(_signal("req-3", ["weirdo.foo", "unrelated.bar"]))

    sig, src = _read_divergence(db, vid)
    assert sig is None
    assert src is None


@pytest.mark.anyio
async def test_harvester_null_request_id_is_noop(tmp_path):
    db = _make_db(tmp_path)
    _insert_verdict(db, model="schema_mapper", request_id="whatever")

    harvester = SchemaMapperHarvester(db)
    # Must not raise and must not touch any row when request_id is None.
    await harvester.process(_signal(None, ["weird_wrapper.message.content"]))

    # Every schema_mapper row should still have null divergence.
    conn = sqlite3.connect(db.path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM onnx_verdicts "
            "WHERE model_name='schema_mapper' AND divergence_signal IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


@pytest.mark.anyio
async def test_harvester_targets_latest_verdict_for_request(tmp_path):
    # Two verdict rows with the same request_id (e.g. retries); only the
    # most recent one receives the divergence signal.
    db = _make_db(tmp_path)
    vid_old = _insert_verdict(db, model="schema_mapper", request_id="req-4")
    # Bump timestamp by writing the second row after a short delay-free
    # insert — `datetime.now()` at microsecond resolution guarantees ordering.
    vid_new = _insert_verdict(db, model="schema_mapper", request_id="req-4")

    harvester = SchemaMapperHarvester(db)
    await harvester.process(_signal("req-4", ["message.content"]))

    old_sig, _ = _read_divergence(db, vid_old)
    new_sig, _ = _read_divergence(db, vid_new)
    assert old_sig is None
    assert new_sig == "content"


@pytest.mark.anyio
async def test_harvester_targets_schema_mapper_only(tmp_path):
    # A safety verdict with the same request_id must not be touched.
    db = _make_db(tmp_path)
    safety_vid = _insert_verdict(db, model="safety", request_id="req-5")
    mapper_vid = _insert_verdict(db, model="schema_mapper", request_id="req-5")

    harvester = SchemaMapperHarvester(db)
    await harvester.process(_signal("req-5", ["message.content"]))

    safety_sig, _ = _read_divergence(db, safety_vid)
    mapper_sig, _ = _read_divergence(db, mapper_vid)
    assert safety_sig is None
    assert mapper_sig == "content"


@pytest.mark.anyio
async def test_harvester_target_model_constant():
    # Drift guard — the runner filters on this exact string.
    assert SchemaMapperHarvester.target_model == "schema_mapper"
