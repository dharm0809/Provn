"""DistillationWorker — scheduler + orchestration.

Ties the dataset builder (Task 17), the per-model trainers (Task 18-19),
and the registry (Task 9-12) into a single background asyncio loop. A
cycle:

    for model in ("intent", "schema_mapper", "safety"):
        dataset = build(model, since=last_snapshot)
        if dataset is too small → skip
        candidate = trainer.train(dataset, version, registry.candidates)
        emit `training_dataset_fingerprint` + `candidate_created` lifecycle
        record a `training_snapshots` row (marks this data as consumed)

Shadow validation is Phase F; this worker stops once the candidate is
written to disk — it does NOT touch production.

Triggering
----------
* Interval poll (`poll_interval_s`, default 1h).
* On each tick the total count of divergent rows is compared against
  `min_divergences` — below threshold, the cycle is skipped.
* `force_cycle()` skips the gate for manual / dashboard retrain triggers
  (Task 28).

Cron semantics (plan says "nightly at `distillation_schedule_cron`") are
intentionally deferred — interval polling is simpler to reason about and
equivalent in effect for the default daily cadence. Switching to cron
can be a follow-up once Task 37 chaos testing is done.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.distillation.dataset import DatasetBuilder, TrainingDataset
from gateway.intelligence.distillation.trainers.base import Trainer, TrainingError
from gateway.intelligence.events import (
    LifecycleEvent,
    build_candidate_created,
    build_training_fingerprint,
)
from gateway.intelligence.registry import ALLOWED_MODEL_NAMES, ModelRegistry

logger = logging.getLogger(__name__)


_MODELS_IN_CYCLE_ORDER: tuple[str, ...] = ("intent", "schema_mapper", "safety")


@dataclass
class CycleResult:
    """What a single cycle produced — useful for tests + force-retrain endpoint."""
    trained: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    candidates: dict[str, Path] = field(default_factory=dict)
    # Models whose lock was already held by another cycle and were
    # therefore not entered. Surfaced separately from `skipped` (which
    # means "trained but threshold not met") so dashboards can render
    # the distinction.
    already_running: list[str] = field(default_factory=list)


class DistillationWorker:
    def __init__(
        self,
        db: IntelligenceDB,
        builder: DatasetBuilder,
        trainers: dict[str, Trainer],
        registry: ModelRegistry,
        *,
        min_divergences: int = 500,
        poll_interval_s: float = 3600.0,
        walacor_client: Any | None = None,
    ) -> None:
        unknown = set(trainers) - ALLOWED_MODEL_NAMES
        if unknown:
            raise ValueError(
                f"trainers dict contains non-canonical model names: {unknown}"
            )
        self._db = db
        self._builder = builder
        self._trainers = dict(trainers)
        self._registry = registry
        self._min_divergences = max(1, int(min_divergences))
        self._poll_interval_s = max(1.0, float(poll_interval_s))
        self._walacor = walacor_client
        self._running = False
        self._task: asyncio.Task | None = None
        # Per-model cycle locks. Two concurrent retrain triggers for the
        # same model (manual force_cycle + scheduled poll, two dashboard
        # tabs) would otherwise both call trainer.train, both write
        # candidates with timestamp-based versions, and either collide on
        # disk or silently overwrite each other. Locks are per-model so
        # `intent` and `safety` can still train in parallel.
        self._cycle_locks: dict[str, asyncio.Lock] = {}
        # Strong references for drift-monitor-spawned retrain tasks; see
        # attach_drift_monitor. Without these the GC can collect a
        # pending task whose coroutine is still running.
        self._drift_retrain_tasks: set[asyncio.Task] = set()

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self.run(), name="distillation-worker")

    async def run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval_s)
                if not self._running:
                    return
                # `_should_trigger` opens SQLite and runs a COUNT(*) on
                # `onnx_verdicts` — that scan must not run on the event
                # loop thread (would stall every in-flight request).
                if await asyncio.to_thread(self._should_trigger):
                    await self._run_cycle()
            except Exception:
                # Hot path contract: background worker never propagates.
                # `asyncio.CancelledError` inherits from BaseException so
                # `except Exception` doesn't swallow cancellation.
                logger.exception("distillation iteration failed")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except Exception:
                logger.debug("distillation worker task raised on stop", exc_info=True)
            self._task = None

    async def force_cycle(self) -> CycleResult:
        """Run one full cycle right now, bypassing the interval + threshold.

        Used by the dashboard force-retrain endpoint (Task 28) and by
        tests that want deterministic execution without spinning up the
        background task.
        """
        return await self._run_cycle()

    def attach_drift_monitor(self, monitor: "Any") -> None:
        """Subscribe a forced cycle to drift signals from `monitor`.

        Listener is sync (drift monitor expectation). It schedules
        `retrain_one(sig.model)` as a background task and returns
        immediately so the monitor's check loop isn't held up by
        training latency. Failures are logged and swallowed — drift
        detection is observational; a missed retrain is recoverable on
        the next cycle.
        """
        def _on_drift(sig) -> None:  # type: ignore[no-untyped-def]
            logger.info(
                "drift signal: model=%s baseline=%.3f current=%.3f delta=%.3f samples=%d",
                sig.model, sig.baseline_accuracy, sig.current_accuracy,
                sig.delta, sig.sample_count,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("drift listener fired with no running loop — skipping")
                return
            task = loop.create_task(
                self.retrain_one(sig.model),
                name=f"drift-retrain-{sig.model}",
            )
            # Hold a reference so the task isn't GC'd mid-flight.
            self._drift_retrain_tasks.add(task)
            task.add_done_callback(self._drift_retrain_tasks.discard)

        if not hasattr(self, "_drift_retrain_tasks"):
            self._drift_retrain_tasks = set()
        monitor.on_drift(_on_drift)

    async def retrain_one(self, model_name: str) -> CycleResult:
        """Train a single model immediately, bypassing the trigger gate.

        Wraps `_train_one` so the Task 28 `/retrain/{model}` endpoint
        has a single-model variant alongside the all-models
        `force_cycle`. Returns a CycleResult populated only for the
        requested model — trained / skipped / failed buckets follow
        the same semantics as a full cycle so the caller's parser
        doesn't have to branch.
        """
        result = CycleResult()
        if model_name not in ALLOWED_MODEL_NAMES:
            # Non-canonical model name — neither trained nor skipped,
            # just rejected. Surface in `failed` so the dashboard
            # renders the error consistently.
            result.failed.append(model_name)
            return result
        await self._train_with_lock(model_name, result)
        return result

    def _lock_for(self, model_name: str) -> asyncio.Lock:
        """Lazy per-model lock so callers don't need to pre-register models."""
        lock = self._cycle_locks.get(model_name)
        if lock is None:
            lock = asyncio.Lock()
            self._cycle_locks[model_name] = lock
        return lock

    async def _train_with_lock(self, model_name: str, result: CycleResult) -> None:
        """Run `_train_one` under the per-model lock; record the bucket.

        If the lock is already held by a concurrent cycle for the same
        model, append to `already_running` and return immediately. The
        in-flight cycle will write the candidate; this caller doesn't
        wait for it.
        """
        lock = self._lock_for(model_name)
        if lock.locked():
            logger.info(
                "distillation skip %s: cycle already running for this model",
                model_name,
            )
            result.already_running.append(model_name)
            return
        async with lock:
            try:
                outcome = await self._train_one(model_name)
                if outcome is None:
                    result.skipped.append(model_name)
                else:
                    result.trained.append(model_name)
                    result.candidates[model_name] = outcome
            except Exception:
                logger.exception("training failed for %s", model_name)
                result.failed.append(model_name)

    # ── Cycle ──────────────────────────────────────────────────────────

    async def _run_cycle(self) -> CycleResult:
        result = CycleResult()
        for model_name in _MODELS_IN_CYCLE_ORDER:
            await self._train_with_lock(model_name, result)
        return result

    async def _train_one(self, model_name: str) -> Path | None:
        trainer = self._trainers.get(model_name)
        if trainer is None:
            logger.debug("no trainer registered for %s — skipping", model_name)
            return None

        since = await asyncio.to_thread(self._last_snapshot_timestamp, model_name)
        dataset: TrainingDataset = await asyncio.to_thread(
            self._builder.build,
            model_name,
            since_timestamp=since,
            min_samples=self._min_divergences,
        )
        if not dataset.X:
            logger.info(
                "distillation skip %s: dataset below threshold (min=%d)",
                model_name, self._min_divergences,
            )
            return None

        # Time the actual training work — dataset build above is a
        # cheap SQL query, the heavy compute is trainer.train + ONNX
        # export. We only count cycles that actually trained, hence
        # the timer starts AFTER the early-return path.
        import time
        train_start = time.perf_counter()

        version = _make_version()
        candidates_dir = self._registry.base / "candidates"
        candidate_path = await asyncio.to_thread(
            trainer.train, dataset.X, dataset.y, version, candidates_dir,
        )
        content_hash = await asyncio.to_thread(_hash_file, candidate_path)
        try:
            from gateway.metrics.prometheus import distillation_run_duration_seconds
            distillation_run_duration_seconds.labels(model=model_name).observe(
                time.perf_counter() - train_start,
            )
        except Exception:
            pass

        fp_event = build_training_fingerprint(
            model_name=model_name,
            row_ids=dataset.row_ids,
            content_hash=content_hash,
        )
        dataset_hash = fp_event.payload["dataset_hash"]
        create_event = build_candidate_created(
            model_name=model_name,
            candidate_version=version,
            dataset_hash=dataset_hash,
            training_sample_count=len(dataset.X),
        )
        await self._write_lifecycle(fp_event)
        await self._write_lifecycle(create_event)

        # Record the snapshot so `since_timestamp` on the next cycle
        # excludes these rows. `INSERT OR IGNORE` keeps repeated force
        # triggers idempotent on dataset_hash (the column is UNIQUE).
        await asyncio.to_thread(
            self._record_snapshot, model_name, dataset_hash, dataset.row_ids,
        )
        logger.info(
            "distillation trained %s: version=%s samples=%d path=%s",
            model_name, version, len(dataset.X), candidate_path,
        )
        return candidate_path

    # ── Walacor + SQLite helpers ───────────────────────────────────────

    async def _write_lifecycle(self, event: LifecycleEvent) -> None:
        """Emit a lifecycle event via whatever writer interface is wired.

        Preferred path (Task 21) is the `LifecycleEventWriter` — it
        retries with backoff and mirrors to SQLite. Older fakes used in
        Task 20 tests still work via the `write_lifecycle_event` /
        `write_record` fallbacks.
        """
        if self._walacor is None:
            logger.debug(
                "distillation: no walacor client — skipping %s",
                event.event_type.value,
            )
            return
        try:
            if hasattr(self._walacor, "write_event"):
                # Task 21 LifecycleEventWriter interface — retry +
                # mirror are already owned by the writer.
                await self._walacor.write_event(event)
            elif hasattr(self._walacor, "write_lifecycle_event"):
                await self._walacor.write_lifecycle_event(event)
            else:
                await self._walacor.write_record(event.to_record())
        except Exception:
            # Never let a Walacor outage break the local cycle — the
            # candidate file is already on disk and the snapshot row
            # will mark the data consumed.
            logger.warning(
                "lifecycle write failed (event=%s)",
                event.event_type.value, exc_info=True,
            )

    def _should_trigger(self) -> bool:
        conn = sqlite3.connect(self._db.path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM onnx_verdicts "
                "WHERE divergence_signal IS NOT NULL"
            ).fetchone()[0]
            return n >= self._min_divergences
        finally:
            conn.close()

    def _last_snapshot_timestamp(self, model_name: str) -> str | None:
        conn = sqlite3.connect(self._db.path)
        try:
            row = conn.execute(
                "SELECT MAX(created_at) FROM training_snapshots WHERE model_name = ?",
                (model_name,),
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()

    def _record_snapshot(
        self, model_name: str, dataset_hash: str, row_ids: list[int],
    ) -> None:
        conn = sqlite3.connect(self._db.path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO training_snapshots "
                "(model_name, dataset_hash, row_ids_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    model_name,
                    dataset_hash,
                    json.dumps(sorted(row_ids)),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


# ── Module helpers ──────────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    """Content hash for a candidate `.onnx` file (SHA256 of bytes).

    Used as the immutable fingerprint piece of
    `build_training_fingerprint` so that a byte-identical retrain
    produces the same dataset_hash and the snapshot INSERT is a no-op.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_version() -> str:
    """Timestamp-based version string. Compact + sortable.

    `ModelRegistry.list_candidates` expects versions to match
    `[a-zA-Z0-9_.\\-]+` — this format satisfies that.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
