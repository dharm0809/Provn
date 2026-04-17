"""Phase 25 Task 20: DistillationWorker tests.

Uses stub trainers (return deterministic bytes, skip real sklearn) and a
real `ModelRegistry` + `IntelligenceDB`. We drive the worker via
`force_cycle()` rather than the background task so tests stay
deterministic.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.distillation.dataset import DatasetBuilder
from gateway.intelligence.distillation.trainers.base import Trainer
from gateway.intelligence.distillation.worker import (
    DistillationWorker,
    _make_version,
)
from gateway.intelligence.events import EventType
from gateway.intelligence.registry import ModelRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _StubTrainer(Trainer):
    """Writes deterministic bytes — skips sklearn and skl2onnx entirely."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.seen_xy: list[tuple[list[Any], list[str]]] = []

    def _fit(self, X, y):
        self.seen_xy.append((list(X), list(y)))
        return "stub-pipeline"

    def _to_onnx(self, pipeline, X_sample):
        return f"ONNX[{self.model_name}]".encode()


class _FakeWalacor:
    """Captures lifecycle events so tests can assert emission."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write_lifecycle_event(self, event):
        self.events.append(event)


def _insert_divergent(
    db: IntelligenceDB,
    *,
    model: str,
    n: int,
    labels: list[str],
    training_text: str | None = "text",
    features_json: str = "{}",
    session_prefix: str = "ss",
    start_ts: str | None = None,
) -> None:
    """Populate `n` divergent verdict rows, cycling labels, unique input_hashes."""
    conn = sqlite3.connect(db.path)
    try:
        for i in range(n):
            ts = start_ts or datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO onnx_verdicts "
                "(model_name, input_hash, input_features_json, prediction, confidence, "
                "request_id, timestamp, divergence_signal, divergence_source, training_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model,
                    f"{model}-{i}",
                    features_json,
                    "stub",
                    0.9,
                    f"{session_prefix}-{i}-uuid",
                    ts,
                    labels[i % len(labels)],
                    "test",
                    training_text,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_everything(tmp_path: Path, min_divergences: int = 2):
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    registry = ModelRegistry(str(tmp_path / "models"))
    registry.ensure_structure()
    builder = DatasetBuilder(db, per_session_cap_ratio=1.0)
    trainers = {
        "intent": _StubTrainer("intent"),
        "schema_mapper": _StubTrainer("schema_mapper"),
        "safety": _StubTrainer("safety"),
    }
    walacor = _FakeWalacor()
    worker = DistillationWorker(
        db=db,
        builder=builder,
        trainers=trainers,
        registry=registry,
        min_divergences=min_divergences,
        walacor_client=walacor,
    )
    return db, registry, trainers, walacor, worker


# ── force_cycle happy path ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_cycle_trains_model_with_enough_divergent_rows(tmp_path):
    db, registry, trainers, walacor, worker = _make_everything(tmp_path, min_divergences=2)

    _insert_divergent(db, model="intent", n=4,
                      labels=["web_search", "normal", "web_search", "normal"])

    result = await worker.force_cycle()

    assert "intent" in result.trained
    assert "intent" in result.candidates
    # Candidate file exists with the stub's deterministic bytes.
    path = result.candidates["intent"]
    assert path.parent == registry.base / "candidates"
    assert path.read_bytes() == b"ONNX[intent]"
    # Trainer received the dataset.
    assert trainers["intent"].seen_xy
    X, y = trainers["intent"].seen_xy[-1]
    assert len(X) == len(y)


@pytest.mark.anyio
async def test_cycle_skips_model_below_threshold(tmp_path):
    db, _, _, walacor, worker = _make_everything(tmp_path, min_divergences=10)

    # Only 3 rows — below min_divergences=10.
    _insert_divergent(db, model="intent", n=3, labels=["web_search", "normal", "web_search"])

    result = await worker.force_cycle()

    assert "intent" in result.skipped
    assert "intent" not in result.trained
    # No lifecycle events emitted when no training happened.
    assert walacor.events == []


@pytest.mark.anyio
async def test_cycle_handles_all_three_models_independently(tmp_path):
    db, registry, _, _, worker = _make_everything(tmp_path, min_divergences=2)

    _insert_divergent(db, model="intent", n=4,
                      labels=["web_search", "normal", "web_search", "normal"])
    _insert_divergent(db, model="safety", n=4,
                      labels=["violence", "safe", "violence", "safe"],
                      training_text="response body")
    _insert_divergent(db, model="schema_mapper", n=4,
                      labels=["content", "prompt_tokens", "content", "prompt_tokens"],
                      training_text=None,
                      features_json=json.dumps({"f1": 0.5}))

    result = await worker.force_cycle()

    assert set(result.trained) == {"intent", "safety", "schema_mapper"}
    for model in ("intent", "safety", "schema_mapper"):
        assert (registry.base / "candidates").glob(f"{model}-*.onnx")


@pytest.mark.anyio
async def test_cycle_records_training_snapshot(tmp_path):
    db, _, _, _, worker = _make_everything(tmp_path, min_divergences=2)
    _insert_divergent(db, model="intent", n=4, labels=["web_search", "normal", "web_search", "normal"])

    await worker.force_cycle()

    conn = sqlite3.connect(db.path)
    try:
        rows = conn.execute(
            "SELECT model_name, dataset_hash, row_ids_json FROM training_snapshots"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "intent"
    assert rows[0][1]  # non-empty dataset_hash
    # row_ids is a JSON list with length == training sample count (4 here).
    row_ids = json.loads(rows[0][2])
    assert len(row_ids) == 4


@pytest.mark.anyio
async def test_second_cycle_skips_already_trained_rows(tmp_path):
    db, registry, trainers, _, worker = _make_everything(tmp_path, min_divergences=2)

    # Turn 1: insert 4 divergent rows, train.
    _insert_divergent(db, model="intent", n=4, labels=["web_search", "normal", "web_search", "normal"])
    await worker.force_cycle()
    first_call_count = len(trainers["intent"].seen_xy)
    assert first_call_count == 1

    # Turn 2: the previous rows have been snapshotted. A second force_cycle
    # without new data must skip (dataset empty).
    result = await worker.force_cycle()
    assert "intent" in result.skipped


@pytest.mark.anyio
async def test_training_failure_does_not_block_sibling_models(tmp_path):
    db, registry, _, _, worker = _make_everything(tmp_path, min_divergences=2)

    class _BrokenTrainer(Trainer):
        model_name = "intent"
        def _fit(self, X, y): raise RuntimeError("boom")
        def _to_onnx(self, p, s): return b""

    # Swap intent's trainer for one that raises; keep safety working.
    worker._trainers["intent"] = _BrokenTrainer()

    _insert_divergent(db, model="intent", n=4, labels=["web_search", "normal", "web_search", "normal"])
    _insert_divergent(db, model="safety", n=4,
                      labels=["violence", "safe", "violence", "safe"],
                      training_text="resp")

    result = await worker.force_cycle()

    assert "intent" in result.failed
    assert "safety" in result.trained


# ── Walacor interaction ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_cycle_emits_lifecycle_events_when_walacor_wired(tmp_path):
    db, _, _, walacor, worker = _make_everything(tmp_path, min_divergences=2)
    _insert_divergent(db, model="intent", n=4, labels=["web_search", "normal", "web_search", "normal"])

    await worker.force_cycle()

    types = [e.event_type for e in walacor.events]
    assert EventType.TRAINING_DATASET_FINGERPRINT in types
    assert EventType.CANDIDATE_CREATED in types


@pytest.mark.anyio
async def test_cycle_works_without_walacor(tmp_path):
    # Walacor unwired — cycle still produces candidate file + snapshot.
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    registry = ModelRegistry(str(tmp_path / "models"))
    registry.ensure_structure()
    worker = DistillationWorker(
        db=db,
        builder=DatasetBuilder(db, per_session_cap_ratio=1.0),
        trainers={"intent": _StubTrainer("intent")},
        registry=registry,
        min_divergences=2,
        walacor_client=None,
    )

    _insert_divergent(db, model="intent", n=4, labels=["web_search", "normal", "web_search", "normal"])
    result = await worker.force_cycle()

    assert "intent" in result.trained
    assert result.candidates["intent"].exists()


@pytest.mark.anyio
async def test_cycle_survives_walacor_failure(tmp_path):
    db, registry, _, _, worker = _make_everything(tmp_path, min_divergences=2)

    class _BrokenWalacor:
        async def write_lifecycle_event(self, event):
            raise RuntimeError("walacor down")
    worker._walacor = _BrokenWalacor()

    _insert_divergent(db, model="intent", n=4, labels=["web_search", "normal", "web_search", "normal"])
    result = await worker.force_cycle()

    # Training still succeeds — walacor outage is logged + swallowed.
    assert "intent" in result.trained
    assert result.candidates["intent"].exists()


# ── Misc ────────────────────────────────────────────────────────────────────

def test_rejects_non_canonical_trainer_keys(tmp_path):
    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    registry = ModelRegistry(str(tmp_path / "models"))
    registry.ensure_structure()
    with pytest.raises(ValueError, match="non-canonical"):
        DistillationWorker(
            db=db,
            builder=DatasetBuilder(db),
            trainers={"not_a_real_model": _StubTrainer("not_a_real_model")},
            registry=registry,
        )


def test_make_version_is_filesystem_safe():
    # Must match the candidate-filename regex in ModelRegistry — only
    # `[a-zA-Z0-9_.\-]+` is allowed.
    import re
    v = _make_version()
    assert re.match(r"^[a-zA-Z0-9_.\-]+$", v)


@pytest.mark.anyio
async def test_should_trigger_counts_all_divergent_rows(tmp_path):
    db, _, _, _, worker = _make_everything(tmp_path, min_divergences=5)
    # 3 rows — below 5, should not trigger.
    _insert_divergent(db, model="intent", n=3, labels=["web_search", "normal", "web_search"])
    assert worker._should_trigger() is False
    # 6 rows across two models — at threshold.
    _insert_divergent(db, model="safety", n=3, labels=["violence", "safe", "violence"])
    assert worker._should_trigger() is True


@pytest.mark.anyio
async def test_run_loop_does_not_block_event_loop_on_should_trigger(tmp_path, monkeypatch):
    """Regression for C3: `_should_trigger` opens SQLite and runs
    COUNT(*); it must execute on a worker thread so the event loop
    stays responsive while the scan is in flight."""
    import asyncio
    import threading

    db, _, _, _, worker = _make_everything(tmp_path, min_divergences=1)

    sleep_started = asyncio.Event()
    in_loop_thread_id = threading.get_ident()
    seen_thread_ids: list[int] = []

    def slow_trigger() -> bool:
        seen_thread_ids.append(threading.get_ident())
        sleep_started.set()
        # Block for long enough that a synchronous call would stall
        # the event loop for the full duration.
        import time as _t
        _t.sleep(0.2)
        return False

    monkeypatch.setattr(worker, "_should_trigger", slow_trigger, raising=True)
    monkeypatch.setattr(worker, "_poll_interval_s", 0.01, raising=True)

    worker.start()
    try:
        # While `run()` is off in a thread running slow_trigger, the
        # event loop must still be able to schedule this sleep.
        await asyncio.wait_for(sleep_started.wait(), timeout=1.0)
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(0.01)
        elapsed = asyncio.get_event_loop().time() - start
        # Without to_thread, elapsed would be close to 0.2s (blocked on
        # the sync sleep). With to_thread, it's ~0.01s.
        assert elapsed < 0.1, (
            f"event loop stalled during _should_trigger "
            f"(elapsed={elapsed:.3f}s — expected <0.1s)"
        )
    finally:
        await worker.stop()

    # And confirm the trigger did run on a DIFFERENT thread than the
    # event loop itself.
    assert seen_thread_ids, "slow_trigger never ran"
    assert seen_thread_ids[0] != in_loop_thread_id, (
        "_should_trigger ran on the event loop thread"
    )
