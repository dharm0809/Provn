"""Phase 25 Task 23: shadow metrics + McNemar tests.

Drives real SQLite fixtures — no mocks for the DB layer. Scipy's
`binomtest` is available in-env so McNemar p-values are computed
against the same code the gate will use.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.shadow_metrics import (
    ShadowMetrics,
    _mcnemar_exact,
    compute_metrics,
)


def _make_db(tmp_path: Path) -> IntelligenceDB:
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    return db


def _insert_shadow(
    db: IntelligenceDB,
    *,
    model: str,
    candidate_version: str,
    input_hash: str,
    production_prediction: str,
    candidate_prediction: str | None,
    candidate_error: str | None = None,
    production_confidence: float = 0.9,
    candidate_confidence: float | None = 0.9,
) -> None:
    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            "INSERT INTO shadow_comparisons "
            "(model_name, candidate_version, input_hash, production_prediction, "
            "production_confidence, candidate_prediction, candidate_confidence, "
            "candidate_error, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, candidate_version, input_hash,
                production_prediction, production_confidence,
                candidate_prediction,
                candidate_confidence if candidate_error is None else None,
                candidate_error,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_verdict_with_truth(
    db: IntelligenceDB,
    *,
    model: str,
    input_hash: str,
    divergence: str,
) -> None:
    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, confidence, "
            "request_id, timestamp, divergence_signal, divergence_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, input_hash, "{}", "stub", 0.5, "r1",
                datetime.now(timezone.utc).isoformat(),
                divergence, "test",
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── empty / no-signal cases ─────────────────────────────────────────────────

def test_empty_shadow_comparisons_returns_empty_metrics(tmp_path):
    db = _make_db(tmp_path)
    m = compute_metrics(db, "intent", "v1")
    assert m.sample_count == 0
    assert m.labeled_count == 0
    assert m.candidate_accuracy == 0.0
    assert m.production_accuracy == 0.0
    assert m.disagreement_rate == 0.0
    assert m.candidate_error_rate == 0.0
    assert m.mcnemar_p_value == 1.0


def test_no_ground_truth_still_computes_disagreement_and_error(tmp_path):
    # 4 rows; 2 agree, 2 disagree. No verdict rows → no ground truth,
    # so accuracy fields are zero and McNemar returns 1.0.
    db = _make_db(tmp_path)
    _insert_shadow(db, model="intent", candidate_version="v1",
                   input_hash="h1",
                   production_prediction="normal", candidate_prediction="normal")
    _insert_shadow(db, model="intent", candidate_version="v1",
                   input_hash="h2",
                   production_prediction="normal", candidate_prediction="web_search")
    _insert_shadow(db, model="intent", candidate_version="v1",
                   input_hash="h3",
                   production_prediction="web_search", candidate_prediction="normal")
    _insert_shadow(db, model="intent", candidate_version="v1",
                   input_hash="h4",
                   production_prediction="normal", candidate_prediction="normal")

    m = compute_metrics(db, "intent", "v1")
    assert m.sample_count == 4
    assert m.labeled_count == 0
    # 2 disagreements / 4 rows.
    assert m.disagreement_rate == 0.5
    assert m.candidate_error_rate == 0.0
    assert m.mcnemar_p_value == 1.0


def test_candidate_error_rows_count_as_disagreement(tmp_path):
    db = _make_db(tmp_path)
    _insert_shadow(db, model="intent", candidate_version="v1",
                   input_hash="h1",
                   production_prediction="normal", candidate_prediction=None,
                   candidate_error="crashed")
    _insert_shadow(db, model="intent", candidate_version="v1",
                   input_hash="h2",
                   production_prediction="normal", candidate_prediction="normal")

    m = compute_metrics(db, "intent", "v1")
    assert m.sample_count == 2
    assert m.candidate_error_rate == 0.5
    # 1 error row → disagrees; 1 agreeing row.
    assert m.disagreement_rate == 0.5


# ── accuracy + McNemar ──────────────────────────────────────────────────────

def test_identical_predictions_high_mcnemar_p(tmp_path):
    # Candidate matches production on every labeled row → b=c=0 → p=1.0.
    db = _make_db(tmp_path)
    for i in range(10):
        h = f"h{i}"
        _insert_verdict_with_truth(db, model="intent", input_hash=h, divergence="web_search")
        _insert_shadow(db, model="intent", candidate_version="v1",
                       input_hash=h,
                       production_prediction="web_search",
                       candidate_prediction="web_search")

    m = compute_metrics(db, "intent", "v1")
    assert m.labeled_count == 10
    assert m.production_accuracy == 1.0
    assert m.candidate_accuracy == 1.0
    assert m.mcnemar_p_value == 1.0


def test_candidate_consistently_better_significant_mcnemar(tmp_path):
    # Production wrong on 20 labeled rows, candidate right on all of them.
    # b=0 prod-only-correct, c=20 cand-only-correct → exact McNemar gives
    # a tiny p-value.
    db = _make_db(tmp_path)
    for i in range(20):
        h = f"h{i}"
        _insert_verdict_with_truth(db, model="intent", input_hash=h, divergence="web_search")
        _insert_shadow(db, model="intent", candidate_version="v1",
                       input_hash=h,
                       production_prediction="normal",
                       candidate_prediction="web_search")

    m = compute_metrics(db, "intent", "v1")
    assert m.labeled_count == 20
    assert m.production_accuracy == 0.0
    assert m.candidate_accuracy == 1.0
    # 2^-20 ≈ 9.5e-7 — well below any reasonable alpha.
    assert m.mcnemar_p_value < 0.001


def test_random_disagreement_gives_high_p(tmp_path):
    # b=5, c=5 → min=5, n=10, two-sided p ≈ 1.0 under binomial(10, 0.5).
    db = _make_db(tmp_path)
    # 5 rows where production is right, candidate wrong:
    for i in range(5):
        h = f"a{i}"
        _insert_verdict_with_truth(db, model="intent", input_hash=h, divergence="web_search")
        _insert_shadow(db, model="intent", candidate_version="v1",
                       input_hash=h,
                       production_prediction="web_search",
                       candidate_prediction="normal")
    # 5 rows where candidate is right, production wrong:
    for i in range(5):
        h = f"b{i}"
        _insert_verdict_with_truth(db, model="intent", input_hash=h, divergence="web_search")
        _insert_shadow(db, model="intent", candidate_version="v1",
                       input_hash=h,
                       production_prediction="normal",
                       candidate_prediction="web_search")

    m = compute_metrics(db, "intent", "v1")
    assert m.labeled_count == 10
    assert m.production_accuracy == 0.5
    assert m.candidate_accuracy == 0.5
    # With exactly balanced discordance the two-sided p equals 1.0.
    assert m.mcnemar_p_value == pytest.approx(1.0)


def test_mcnemar_exact_small_values():
    # 10 discordant pairs all in one direction → very low p.
    assert _mcnemar_exact(0, 10) < 0.01
    assert _mcnemar_exact(10, 0) < 0.01
    # No discordant pairs — nothing to distinguish.
    assert _mcnemar_exact(0, 0) == 1.0
    # Balanced discordance → maximum p.
    assert _mcnemar_exact(5, 5) == pytest.approx(1.0)


def test_metrics_filter_by_candidate_version(tmp_path):
    # Other candidate versions don't leak into the target query.
    db = _make_db(tmp_path)
    _insert_shadow(db, model="intent", candidate_version="v1",
                   input_hash="h1",
                   production_prediction="normal", candidate_prediction="normal")
    _insert_shadow(db, model="intent", candidate_version="v2",
                   input_hash="h2",
                   production_prediction="normal", candidate_prediction="web_search")

    m1 = compute_metrics(db, "intent", "v1")
    m2 = compute_metrics(db, "intent", "v2")
    assert m1.sample_count == 1
    assert m2.sample_count == 1
    assert m1.disagreement_rate == 0.0
    assert m2.disagreement_rate == 1.0


def test_metrics_filter_by_model_name(tmp_path):
    # Safety rows don't contaminate the intent metrics.
    db = _make_db(tmp_path)
    _insert_shadow(db, model="intent", candidate_version="v1",
                   input_hash="h1",
                   production_prediction="normal", candidate_prediction="normal")
    _insert_shadow(db, model="safety", candidate_version="v1",
                   input_hash="h2",
                   production_prediction="safe", candidate_prediction="violence")

    m = compute_metrics(db, "intent", "v1")
    assert m.sample_count == 1
    assert m.disagreement_rate == 0.0
