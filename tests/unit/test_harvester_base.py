"""Phase 25 Task 13: harvester framework base + async queue.

Covers:
  * `HarvesterSignal` is an immutable record with the documented fields.
  * `HarvesterRunner.submit` is non-blocking and returns `False` on overflow
    so the hot-path orchestrator caller never pays queue-full latency.
  * `run()` dispatches each signal only to harvesters whose `target_model`
    matches `signal.model_name`.
  * A failing harvester logs + continues; other harvesters and subsequent
    signals are unaffected.
  * `stop()` cleanly drains — the background task exits without raising.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gateway.intelligence.harvesters import Harvester, HarvesterRunner, HarvesterSignal


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── HarvesterSignal ─────────────────────────────────────────────────────────

def test_signal_is_frozen():
    sig = HarvesterSignal(
        request_id="r1",
        model_name="intent",
        prediction="normal",
        response_payload={"a": 1},
        context={"session_id": "s1"},
    )
    import dataclasses as _dc
    with pytest.raises(_dc.FrozenInstanceError):
        sig.prediction = "web_search"  # type: ignore[misc]


def test_signal_allows_null_request_id():
    # Some inference sites have no request-id context (background jobs).
    # The signal dataclass must accept `None` without raising.
    sig = HarvesterSignal(
        request_id=None,
        model_name="safety",
        prediction="safe",
        response_payload={},
        context={},
    )
    assert sig.request_id is None


# ── HarvesterRunner dispatch ────────────────────────────────────────────────

class _RecordingHarvester(Harvester):
    def __init__(self, target_model: str) -> None:
        self.target_model = target_model
        self.received: list[HarvesterSignal] = []

    async def process(self, signal: HarvesterSignal) -> None:
        self.received.append(signal)


class _RaisingHarvester(Harvester):
    target_model = "intent"

    def __init__(self) -> None:
        self.call_count = 0

    async def process(self, signal: HarvesterSignal) -> None:
        self.call_count += 1
        raise RuntimeError("boom — simulated harvester failure")


@pytest.mark.anyio
async def test_runner_filters_by_target_model():
    intent_h = _RecordingHarvester(target_model="intent")
    safety_h = _RecordingHarvester(target_model="safety")
    runner = HarvesterRunner(harvesters=[intent_h, safety_h])
    runner.start()
    try:
        runner.submit(HarvesterSignal("r1", "intent", "normal", {}, {}))
        runner.submit(HarvesterSignal("r2", "safety", "safe", {}, {}))
        # Give the runner a moment to drain.
        await _drain(runner)

        assert [s.model_name for s in intent_h.received] == ["intent"]
        assert [s.model_name for s in safety_h.received] == ["safety"]
    finally:
        await runner.stop()


@pytest.mark.anyio
async def test_runner_fanout_to_multiple_matching_harvesters():
    # Two harvesters registered for the same model both see the signal.
    a = _RecordingHarvester(target_model="safety")
    b = _RecordingHarvester(target_model="safety")
    runner = HarvesterRunner(harvesters=[a, b])
    runner.start()
    try:
        runner.submit(HarvesterSignal("r1", "safety", "violence", {}, {}))
        await _drain(runner)
        assert len(a.received) == 1
        assert len(b.received) == 1
    finally:
        await runner.stop()


@pytest.mark.anyio
async def test_runner_isolates_harvester_failures():
    # A raising harvester must not take down the runner nor prevent a
    # peer harvester from seeing the signal.
    raising = _RaisingHarvester()
    recording = _RecordingHarvester(target_model="intent")
    runner = HarvesterRunner(harvesters=[raising, recording])
    runner.start()
    try:
        runner.submit(HarvesterSignal("r1", "intent", "normal", {}, {}))
        runner.submit(HarvesterSignal("r2", "intent", "web_search", {}, {}))
        await _drain(runner)

        assert raising.call_count == 2
        assert len(recording.received) == 2
        # Runner task still alive after two raising iterations.
        assert runner._task is not None
        assert not runner._task.done()
    finally:
        await runner.stop()


@pytest.mark.anyio
async def test_submit_returns_false_when_full():
    # Pin queue size at 2 and flood it. Without a running consumer, the
    # third submit must return False instead of blocking or raising.
    runner = HarvesterRunner(harvesters=[], max_queue=2)
    assert runner.submit(HarvesterSignal("r1", "intent", "n", {}, {})) is True
    assert runner.submit(HarvesterSignal("r2", "intent", "n", {}, {})) is True
    assert runner.submit(HarvesterSignal("r3", "intent", "n", {}, {})) is False
    # qsize sanity — exactly two queued, the third was dropped.
    assert runner._queue.qsize() == 2


@pytest.mark.anyio
async def test_stop_is_idempotent_and_joins_task():
    runner = HarvesterRunner(harvesters=[])
    runner.start()
    await runner.stop()
    # Second stop is a no-op, must not raise.
    await runner.stop()
    assert runner._task is None or runner._task.done()


@pytest.mark.anyio
async def test_register_adds_harvester_post_start():
    # Orchestrator wires harvesters after the runner starts (main.py creates
    # it empty; Tasks 14-16 register per-model harvesters). `register` must
    # take effect on subsequent signals without requiring a restart.
    runner = HarvesterRunner(harvesters=[])
    runner.start()
    try:
        late = _RecordingHarvester(target_model="schema_mapper")
        runner.register(late)
        runner.submit(HarvesterSignal("r1", "schema_mapper", "complete", {}, {}))
        await _drain(runner)
        assert len(late.received) == 1
    finally:
        await runner.stop()


# ── helpers ─────────────────────────────────────────────────────────────────

async def _drain(runner: HarvesterRunner, *, timeout: float = 1.0) -> None:
    """Wait until the runner's queue is empty AND any in-flight dispatch is done.

    The runner may have pulled a signal off the queue but not yet awaited
    the harvester tasks, so `qsize()==0` alone isn't sufficient. We use an
    explicit barrier call for determinism.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while runner._queue.qsize() > 0:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("runner did not drain within timeout")
        await asyncio.sleep(0)
    # One extra scheduler tick so the last dispatch fires before we check.
    await runner.join_inflight()
