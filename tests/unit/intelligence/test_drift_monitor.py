"""DriftMonitor: rolling-accuracy regression detector."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.drift_monitor import DriftMonitor, DriftSignal


def _seed(db, *, model, prediction, divergence_signal, ts):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            " confidence, request_id, timestamp, divergence_signal, divergence_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model, "h", "{}", prediction, 0.9, None, ts.isoformat(),
             divergence_signal, "test"),
        )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    d = IntelligenceDB(str(tmp_path / "intel.db"))
    d.init_schema()
    return d


@pytest.mark.anyio
async def test_drift_monitor_emits_signal_on_regression(db):
    now = datetime.now(timezone.utc)
    # Baseline (1h..7h ago): 95% accuracy, 100 samples
    baseline_ts = now - timedelta(hours=2)
    for i in range(100):
        sig = "A" if i < 95 else "B"
        _seed(db, model="intent", prediction="A", divergence_signal=sig, ts=baseline_ts)
    # Recent (last 1h): 80% accuracy, 100 samples — 15-pt drop
    recent_ts = now - timedelta(minutes=10)
    for i in range(100):
        sig = "A" if i < 80 else "B"
        _seed(db, model="intent", prediction="A", divergence_signal=sig, ts=recent_ts)

    monitor = DriftMonitor(
        db,
        window_hours=1,
        baseline_window_count=6,
        threshold=0.05,
        min_samples=50,
        min_coverage=0.30,
        models=["intent"],
        clock=lambda: now,
    )
    signals = await monitor.check_once()
    assert len(signals) == 1
    sig = signals[0]
    assert isinstance(sig, DriftSignal)
    assert sig.model == "intent"
    assert sig.baseline_accuracy == pytest.approx(0.95)
    assert sig.current_accuracy == pytest.approx(0.80)
    assert sig.delta >= 0.05


@pytest.mark.anyio
async def test_drift_monitor_skips_when_recent_below_min_samples(db):
    now = datetime.now(timezone.utc)
    baseline_ts = now - timedelta(hours=2)
    for i in range(100):
        _seed(db, model="intent", prediction="A", divergence_signal="A", ts=baseline_ts)
    # Only 5 recent rows — below default min_samples=50
    recent_ts = now - timedelta(minutes=10)
    for _ in range(5):
        _seed(db, model="intent", prediction="A", divergence_signal="B", ts=recent_ts)

    monitor = DriftMonitor(db, models=["intent"], clock=lambda: now)
    signals = await monitor.check_once()
    assert signals == []


@pytest.mark.anyio
async def test_drift_monitor_skips_when_coverage_too_low(db):
    now = datetime.now(timezone.utc)
    # Baseline: 100 rows, but only 10% have a signal
    baseline_ts = now - timedelta(hours=2)
    for i in range(100):
        sig = "A" if i < 10 else None
        _seed(db, model="intent", prediction="A", divergence_signal=sig, ts=baseline_ts)
    recent_ts = now - timedelta(minutes=10)
    for i in range(100):
        sig = "B" if i < 10 else None
        _seed(db, model="intent", prediction="A", divergence_signal=sig, ts=recent_ts)

    monitor = DriftMonitor(
        db, min_coverage=0.30, min_samples=5, models=["intent"], clock=lambda: now,
    )
    signals = await monitor.check_once()
    assert signals == []


@pytest.mark.anyio
async def test_drift_monitor_no_signal_when_stable(db):
    now = datetime.now(timezone.utc)
    baseline_ts = now - timedelta(hours=2)
    recent_ts = now - timedelta(minutes=10)
    for ts in (baseline_ts, recent_ts):
        for i in range(100):
            sig = "A" if i < 92 else "B"
            _seed(db, model="intent", prediction="A", divergence_signal=sig, ts=ts)
    monitor = DriftMonitor(db, threshold=0.05, models=["intent"], clock=lambda: now)
    signals = await monitor.check_once()
    assert signals == []


@pytest.mark.anyio
async def test_attach_drift_monitor_schedules_retrain(db, tmp_path):
    from gateway.intelligence.distillation.dataset import TrainingDataset
    from gateway.intelligence.distillation.worker import DistillationWorker
    from gateway.intelligence.registry import ModelRegistry

    class _Builder:
        def build(self, model, *, since_timestamp, min_samples):
            return TrainingDataset(X=[{"k": "v"}], y=["a"], row_ids=[1])

    class _Trainer:
        invocations = 0
        def train(self, X, y, version, candidates_dir: Path):
            type(self).invocations += 1
            p = candidates_dir / f"intent-{version}.onnx"
            p.write_bytes(b"x")
            return p

    registry = ModelRegistry(base_path=str(tmp_path / "models"))
    registry.ensure_structure()
    worker = DistillationWorker(
        db=db, builder=_Builder(), trainers={"intent": _Trainer()},
        registry=registry, min_divergences=1,
    )

    now = datetime.now(timezone.utc)
    baseline_ts = now - timedelta(hours=2)
    recent_ts = now - timedelta(minutes=10)
    for i in range(100):
        sig = "A" if i < 95 else "B"
        _seed(db, model="intent", prediction="A", divergence_signal=sig, ts=baseline_ts)
    for i in range(100):
        sig = "A" if i < 80 else "B"
        _seed(db, model="intent", prediction="A", divergence_signal=sig, ts=recent_ts)

    monitor = DriftMonitor(db, models=["intent"], clock=lambda: now)
    worker.attach_drift_monitor(monitor)

    signals = await monitor.check_once()
    assert len(signals) == 1
    # The listener spawns retrain_one as a background task; let it complete.
    pending = list(worker._drift_retrain_tasks)
    assert pending, "drift listener should have scheduled at least one retrain task"
    await asyncio.gather(*pending)
    assert _Trainer.invocations == 1
