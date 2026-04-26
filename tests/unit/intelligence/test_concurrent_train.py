"""Per-model lock prevents concurrent trainer races.

Two simultaneous force_cycle / retrain_one calls for the same model must
not both enter trainer.train(); the loser is reported as
`already_running` and the trainer fires exactly once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.distillation.dataset import TrainingDataset
from gateway.intelligence.distillation.worker import DistillationWorker
from gateway.intelligence.registry import ModelRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeBuilder:
    def build(self, model: str, *, since_timestamp, min_samples):
        # Return a non-empty dataset so _train_one proceeds to trainer.train
        return TrainingDataset(
            X=[{"k": "v"}, {"k": "w"}],
            y=["a", "b"],
            row_ids=[1, 2],
        )


class _SlowTrainer:
    """Trainer that sleeps inside `train` so concurrent callers overlap."""
    def __init__(self) -> None:
        self.invocations = 0

    def train(self, X, y, version, candidates_dir: Path):
        self.invocations += 1
        # Simulate real work; long enough that an unlocked second call
        # would be inside trainer.train at the same time.
        import time
        time.sleep(0.3)
        path = candidates_dir / f"intent-{version}.onnx"
        path.write_bytes(b"fake-onnx")
        return path


@pytest.mark.anyio
async def test_concurrent_retrain_same_model_serializes_to_one_invocation(tmp_path):
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    registry = ModelRegistry(base_path=str(tmp_path / "models"))
    registry.ensure_structure()
    trainer = _SlowTrainer()

    worker = DistillationWorker(
        db=db,
        builder=_FakeBuilder(),
        trainers={"intent": trainer},
        registry=registry,
        min_divergences=1,
    )

    # Two concurrent retrain_one calls for the same model.
    r1, r2 = await asyncio.gather(
        worker.retrain_one("intent"),
        worker.retrain_one("intent"),
    )

    statuses = sorted(
        ("trained" if "intent" in r.trained else
         "already_running" if "intent" in r.already_running else
         "skipped" if "intent" in r.skipped else
         "failed")
        for r in (r1, r2)
    )
    assert statuses == ["already_running", "trained"], (r1, r2)
    assert trainer.invocations == 1


@pytest.mark.anyio
async def test_concurrent_retrain_different_models_runs_in_parallel(tmp_path):
    """Per-model locks must not block training of unrelated models."""
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    registry = ModelRegistry(base_path=str(tmp_path / "models"))
    registry.ensure_structure()
    intent_trainer = _SlowTrainer()
    safety_trainer = _SlowTrainer()

    worker = DistillationWorker(
        db=db,
        builder=_FakeBuilder(),
        trainers={"intent": intent_trainer, "safety": safety_trainer},
        registry=registry,
        min_divergences=1,
    )

    r1, r2 = await asyncio.gather(
        worker.retrain_one("intent"),
        worker.retrain_one("safety"),
    )
    assert "intent" in r1.trained
    assert "safety" in r2.trained
    assert intent_trainer.invocations == 1
    assert safety_trainer.invocations == 1
