"""IntelligenceDB.accuracy_in_window — drift + post-promotion building block."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway.intelligence.db import AccuracySnapshot, IntelligenceDB


def _seed(db, *, model, prediction, divergence_signal, ts, version=None):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            " confidence, request_id, timestamp, divergence_signal, "
            " divergence_source, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model, "h", "{}", prediction, 0.9, None, ts.isoformat(),
             divergence_signal, "test", version),
        )


@pytest.fixture
def db(tmp_path):
    d = IntelligenceDB(str(tmp_path / "intel.db"))
    d.init_schema()
    return d


def test_accuracy_perfect_when_all_match(db):
    now = datetime.now(timezone.utc)
    for _ in range(10):
        _seed(db, model="intent", prediction="A", divergence_signal="A", ts=now)
    snap = db.accuracy_in_window(
        "intent", start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )
    assert isinstance(snap, AccuracySnapshot)
    assert snap.sample_count == 10
    assert snap.total_rows == 10
    assert snap.accuracy == pytest.approx(1.0)
    assert snap.coverage == pytest.approx(1.0)


def test_accuracy_split_when_signals_disagree(db):
    now = datetime.now(timezone.utc)
    for _ in range(8):
        _seed(db, model="intent", prediction="A", divergence_signal="A", ts=now)
    for _ in range(2):
        _seed(db, model="intent", prediction="A", divergence_signal="B", ts=now)
    snap = db.accuracy_in_window(
        "intent", start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )
    assert snap.sample_count == 10
    assert snap.accuracy == pytest.approx(0.8)
    assert snap.coverage == pytest.approx(1.0)


def test_coverage_drops_when_signal_sparse(db):
    """Rows without a divergence_signal count toward coverage denominator."""
    now = datetime.now(timezone.utc)
    for _ in range(2):
        _seed(db, model="intent", prediction="A", divergence_signal="A", ts=now)
    for _ in range(8):
        _seed(db, model="intent", prediction="A", divergence_signal=None, ts=now)
    snap = db.accuracy_in_window(
        "intent", start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )
    assert snap.sample_count == 2
    assert snap.total_rows == 10
    assert snap.accuracy == pytest.approx(1.0)
    assert snap.coverage == pytest.approx(0.2)


def test_window_excludes_rows_outside_range(db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=24)
    _seed(db, model="intent", prediction="A", divergence_signal="A", ts=old)
    _seed(db, model="intent", prediction="A", divergence_signal="B", ts=now)
    snap = db.accuracy_in_window(
        "intent", start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )
    assert snap.total_rows == 1
    assert snap.accuracy == pytest.approx(0.0)


def test_other_models_filtered_out(db):
    now = datetime.now(timezone.utc)
    _seed(db, model="intent", prediction="A", divergence_signal="A", ts=now)
    _seed(db, model="safety", prediction="X", divergence_signal="Y", ts=now)
    snap = db.accuracy_in_window(
        "intent", start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )
    assert snap.total_rows == 1


def test_init_schema_is_idempotent(tmp_path):
    """Re-running init_schema after the version column exists is a no-op."""
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    db.init_schema()  # second call must not raise
    with db._connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(onnx_verdicts)").fetchall()]
    assert "version" in cols
    assert "training_text" in cols


def test_accuracy_in_window_filters_by_version(db):
    now = datetime.now(timezone.utc)
    # 50 v1 verdicts at 100% accuracy
    for _ in range(50):
        _seed(db, model="intent", prediction="A", divergence_signal="A",
              ts=now, version="v1")
    # 50 v2 verdicts at 60% accuracy
    for i in range(50):
        sig = "A" if i < 30 else "B"
        _seed(db, model="intent", prediction="A", divergence_signal=sig,
              ts=now, version="v2")

    snap_v1 = db.accuracy_in_window(
        "intent", version="v1",
        start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )
    snap_v2 = db.accuracy_in_window(
        "intent", version="v2",
        start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )
    snap_all = db.accuracy_in_window(
        "intent",
        start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )

    assert snap_v1.accuracy == pytest.approx(1.0)
    assert snap_v1.sample_count == 50
    assert snap_v2.accuracy == pytest.approx(0.6)
    assert snap_v2.sample_count == 50
    # Aggregate sees both.
    assert snap_all.sample_count == 100
    assert snap_all.accuracy == pytest.approx(0.8)


def test_accuracy_in_window_version_filter_excludes_pre_migration_rows(db):
    """Rows with NULL version must not be counted toward a specific version's accuracy."""
    now = datetime.now(timezone.utc)
    # 20 pre-migration rows (version IS NULL): all "wrong"
    for _ in range(20):
        _seed(db, model="intent", prediction="A", divergence_signal="B",
              ts=now, version=None)
    # 10 v3 rows: all "right"
    for _ in range(10):
        _seed(db, model="intent", prediction="A", divergence_signal="A",
              ts=now, version="v3")

    snap = db.accuracy_in_window(
        "intent", version="v3",
        start=now - timedelta(hours=1), end=now + timedelta(seconds=1),
    )
    assert snap.sample_count == 10
    assert snap.accuracy == pytest.approx(1.0)


def test_empty_window_returns_zeros(db):
    now = datetime.now(timezone.utc)
    snap = db.accuracy_in_window(
        "intent", start=now - timedelta(hours=1), end=now,
    )
    assert snap.sample_count == 0
    assert snap.total_rows == 0
    assert snap.accuracy == 0.0
    assert snap.coverage == 0.0
