"""Phase 25 Task 17: dataset builder tests.

Pulls divergent verdicts, dedupes by input_hash, class-balances the
majority/minority split, caps per-session contribution to 10% for
adversarial robustness, and returns (X, y, row_ids).
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.distillation.dataset import DatasetBuilder


def _make_db(tmp_path: Path) -> IntelligenceDB:
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    return db


def _insert(
    db: IntelligenceDB,
    *,
    model: str,
    request_id: str | None,
    input_hash: str,
    prediction: str,
    divergence: str | None,
    training_text: str | None = None,
    features_json: str = "{}",
    timestamp: str | None = None,
) -> int:
    conn = sqlite3.connect(db.path)
    try:
        cur = conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, confidence, "
            "request_id, timestamp, divergence_signal, divergence_source, training_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, input_hash, features_json, prediction, 0.9,
                request_id,
                timestamp or datetime.now(timezone.utc).isoformat(),
                divergence,
                "test" if divergence else None,
                training_text,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# ── happy path ──────────────────────────────────────────────────────────────

def test_build_returns_divergent_rows_only(tmp_path):
    db = _make_db(tmp_path)
    # Included: has divergence + training_text.
    good_id = _insert(db, model="intent", request_id="r1", input_hash="h1",
                      prediction="normal", divergence="web_search",
                      training_text="please look this up online")
    # Excluded: no divergence signal.
    _insert(db, model="intent", request_id="r2", input_hash="h2",
            prediction="normal", divergence=None,
            training_text="whatever")

    ds = DatasetBuilder(db).build("intent")

    assert ds.row_ids == [good_id]
    assert ds.X == ["please look this up online"]
    assert ds.y == ["web_search"]


def test_build_dedupes_by_input_hash(tmp_path):
    # Two rows with identical input_hash collapse to one training example.
    db = _make_db(tmp_path)
    _insert(db, model="intent", request_id="r1", input_hash="dup",
            prediction="normal", divergence="web_search",
            training_text="search for the news")
    _insert(db, model="intent", request_id="r2", input_hash="dup",
            prediction="normal", divergence="web_search",
            training_text="search for the news")

    ds = DatasetBuilder(db).build("intent")
    assert len(ds.X) == 1


def test_build_skips_rows_without_training_text_for_text_models(tmp_path):
    # Intent and safety need raw text; rows with NULL training_text are
    # dropped so we don't feed the trainer an empty X.
    db = _make_db(tmp_path)
    kept = _insert(db, model="intent", request_id="r1", input_hash="h1",
                   prediction="normal", divergence="web_search",
                   training_text="real text")
    _insert(db, model="intent", request_id="r2", input_hash="h2",
            prediction="normal", divergence="web_search",
            training_text=None)

    ds = DatasetBuilder(db).build("intent")
    assert ds.row_ids == [kept]


def test_schema_mapper_uses_features_not_training_text(tmp_path):
    # schema_mapper trains on feature vectors, not prompts — rows without
    # training_text ARE eligible as long as features_json is non-trivial.
    db = _make_db(tmp_path)
    rid = _insert(db, model="schema_mapper", request_id="r1", input_hash="h1",
                  prediction="complete", divergence="content",
                  features_json='{"f1": 1.0, "f2": 2.0}',
                  training_text=None)

    ds = DatasetBuilder(db).build("schema_mapper")
    assert ds.row_ids == [rid]
    assert ds.y == ["content"]
    # X carries the JSON so the trainer can parse features.
    assert ds.X == ['{"f1": 1.0, "f2": 2.0}']


def test_per_session_cap_limits_adversarial_contribution(tmp_path):
    # One session emits 20 divergent rows; two other sessions emit 2
    # each. Cap is 10% → only 3 of the 24 total rows can come from the
    # dominant session (after dedupe, 20 unique hashes).
    # To make the arithmetic easy: total 24 rows, 10% cap ≈ 2.4 → 2
    # rows permitted per session.
    db = _make_db(tmp_path)
    for i in range(20):
        _insert(db, model="intent", request_id=f"dominant-{i}", input_hash=f"hd{i}",
                prediction="normal", divergence="web_search",
                training_text=f"text-{i}")
        # Force the session_id via request_id prefix. The builder reads
        # the request_id column; to cap by SESSION we need a separate
        # session column — we cheat here by reusing request_id's prefix
        # as the session identifier in the builder. (See builder impl.)
    for i in range(2):
        _insert(db, model="intent", request_id=f"other-a-{i}", input_hash=f"ha{i}",
                prediction="normal", divergence="web_search",
                training_text=f"alt-{i}")
    for i in range(2):
        _insert(db, model="intent", request_id=f"other-b-{i}", input_hash=f"hb{i}",
                prediction="normal", divergence="web_search",
                training_text=f"beta-{i}")

    ds = DatasetBuilder(db, per_session_cap_ratio=0.1).build("intent")
    # Cap at 10% means any one session contributes at most ceil(total * 0.1)
    # rows. With 24 eligible rows, cap is ≤ 3. Dominant session must be
    # truncated; the other sessions keep their 2 each.
    # Note: cap is applied per-session on the deduped set, so we can
    # verify by confirming NO session has more than `ceil(0.1 * total)` rows.
    # Exact total depends on the cap application order; robust assertion:
    counts: Counter = Counter()
    for rid in ds.row_ids:
        # Reverse-lookup request_id from row id.
        with sqlite3.connect(db.path) as conn:
            req = conn.execute(
                "SELECT request_id FROM onnx_verdicts WHERE id=?", (rid,)
            ).fetchone()[0]
        session = req.rsplit("-", 1)[0]
        counts[session] += 1
    dominant = max(counts.values())
    assert dominant <= 3  # 10% of 24 is 2.4 → ceil=3


def test_class_balance_caps_majority_to_two_times_minority(tmp_path):
    # 20 "web_search" examples, 2 "normal" examples → majority cap is
    # 2 * 2 = 4, so the returned dataset has at most 4 web_search rows.
    db = _make_db(tmp_path)
    for i in range(20):
        _insert(db, model="intent", request_id=f"maj-{i}", input_hash=f"m{i}",
                prediction="normal", divergence="web_search",
                training_text=f"majority-{i}")
    for i in range(2):
        _insert(db, model="intent", request_id=f"min-{i}", input_hash=f"n{i}",
                prediction="web_search", divergence="normal",
                training_text=f"minority-{i}")

    ds = DatasetBuilder(db, per_session_cap_ratio=1.0).build("intent")
    counts = Counter(ds.y)
    # Minority keeps all of its rows.
    assert counts["normal"] == 2
    # Majority is capped to 2 × minority.
    assert counts["web_search"] == 4


def test_builds_empty_when_no_divergent_rows(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, model="intent", request_id="r1", input_hash="h1",
            prediction="normal", divergence=None,
            training_text="no divergence")
    ds = DatasetBuilder(db).build("intent")
    assert ds.X == []
    assert ds.y == []
    assert ds.row_ids == []


def test_since_timestamp_filters_old_rows(tmp_path):
    db = _make_db(tmp_path)
    # Old row — before the cutoff.
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _insert(db, model="intent", request_id="r-old", input_hash="ho",
            prediction="normal", divergence="web_search",
            training_text="ancient", timestamp=old_ts)
    # New row — after.
    new_id = _insert(db, model="intent", request_id="r-new", input_hash="hn",
                     prediction="normal", divergence="web_search",
                     training_text="fresh")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ds = DatasetBuilder(db).build("intent", since_timestamp=cutoff)
    assert ds.row_ids == [new_id]


def test_min_samples_returns_empty_when_under_threshold(tmp_path):
    # Callers can set a minimum via the builder; not hitting it returns
    # an empty dataset so the DistillationWorker can short-circuit.
    db = _make_db(tmp_path)
    _insert(db, model="intent", request_id="r1", input_hash="h1",
            prediction="normal", divergence="web_search",
            training_text="only one")
    ds = DatasetBuilder(db).build("intent", min_samples=10)
    assert ds.X == []


def test_skips_rows_with_null_request_id(tmp_path):
    # Rows without request_id cannot be attributed to a session for the
    # 10% cap; drop them defensively rather than letting them dilute the
    # per-session accounting.
    db = _make_db(tmp_path)
    _insert(db, model="intent", request_id=None, input_hash="h1",
            prediction="normal", divergence="web_search",
            training_text="orphan")
    ds = DatasetBuilder(db).build("intent")
    assert ds.X == []
