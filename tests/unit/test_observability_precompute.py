"""Tests for the observability precompute worker.

Contract: each tick refreshes both the readiness module-level cache
(``gateway.readiness.runner._cache``) and the connections module-level
cache (``gateway.connections.api._CACHE``). Failure in one refresh must
not block the other; per-tick exceptions must not kill the run loop.
"""
from __future__ import annotations

import asyncio
import types

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _reset_state():
    from gateway.readiness import runner as readiness_runner
    from gateway.connections import api as conn_api
    readiness_runner._cache = None
    conn_api._reset_cache_for_tests()
    yield
    readiness_runner._cache = None
    conn_api._reset_cache_for_tests()


@pytest.mark.anyio
async def test_tick_refreshes_both_caches(monkeypatch):
    """One tick writes into both the readiness cache and the connections cache."""
    from gateway.observability.precompute import ObservabilityPrecomputeWorker
    from gateway.readiness import runner as readiness_runner
    from gateway.connections import api as conn_api

    sentinel_report = types.SimpleNamespace(status="ready", summary={"green": 1})
    sentinel_snapshot = {"generated_at": "now", "tiles": [{"id": "x"}]}

    calls = {"readiness": 0, "connections": 0}

    async def fake_run_all(ctx, *, fresh=False):  # signature matches the real one
        calls["readiness"] += 1
        # Write into the runner cache the way the real implementation does.
        readiness_runner._cache = (0.0, sentinel_report)
        return sentinel_report

    async def fake_build_snapshot(ctx):
        calls["connections"] += 1
        return sentinel_snapshot

    monkeypatch.setattr("gateway.readiness.runner.run_all", fake_run_all)
    monkeypatch.setattr("gateway.connections.builder.build_snapshot", fake_build_snapshot)

    ctx = types.SimpleNamespace()
    worker = ObservabilityPrecomputeWorker(ctx)
    await worker._tick_once()

    assert calls["readiness"] == 1
    assert calls["connections"] == 1
    # Readiness cache populated.
    assert readiness_runner._cache is not None
    assert readiness_runner._cache[1] is sentinel_report
    # Connections cache populated and ts non-zero.
    assert conn_api._CACHE["snapshot"] is sentinel_snapshot
    assert conn_api._CACHE["ts"] > 0
    # Health surface reflects success.
    assert worker.health["last_tick_ok"] is True
    assert worker.health["last_error"] is None
    assert worker.health["tick_count"] == 1


@pytest.mark.anyio
async def test_failure_in_one_does_not_block_the_other(monkeypatch):
    """If readiness raises, connections still refreshes — and vice versa."""
    from gateway.observability.precompute import ObservabilityPrecomputeWorker
    from gateway.connections import api as conn_api

    async def boom_run_all(ctx, *, fresh=False):
        raise RuntimeError("readiness exploded")

    async def fake_build_snapshot(ctx):
        return {"ok": True}

    monkeypatch.setattr("gateway.readiness.runner.run_all", boom_run_all)
    monkeypatch.setattr("gateway.connections.builder.build_snapshot", fake_build_snapshot)

    ctx = types.SimpleNamespace()
    worker = ObservabilityPrecomputeWorker(ctx)
    await worker._tick_once()

    # Connections refreshed despite readiness failure.
    assert conn_api._CACHE["snapshot"] == {"ok": True}
    # last_tick_ok=True because at least one refresh succeeded; last_error captured.
    assert worker.health["last_tick_ok"] is True
    assert worker.health["last_error"] is not None


@pytest.mark.anyio
async def test_run_loop_survives_a_failing_tick(monkeypatch):
    """The run loop must keep ticking even if every refresh raises."""
    from gateway.observability.precompute import ObservabilityPrecomputeWorker

    async def boom(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr("gateway.readiness.runner.run_all", boom)
    monkeypatch.setattr("gateway.connections.builder.build_snapshot", boom)

    ctx = types.SimpleNamespace()
    worker = ObservabilityPrecomputeWorker(ctx, tick_interval_s=5.0)
    worker.start()
    # Give it enough time to complete its initial tick.
    await asyncio.sleep(0.05)
    assert worker._task is not None
    assert not worker._task.done(), "run loop must not die on tick failure"
    await worker.stop()
    assert worker.health["last_tick_ok"] is False
    assert worker.health["last_error"] is not None


@pytest.mark.anyio
async def test_stop_is_idempotent():
    """stop() is safe to call without start, and safe to call twice."""
    from gateway.observability.precompute import ObservabilityPrecomputeWorker

    worker = ObservabilityPrecomputeWorker(types.SimpleNamespace())
    await worker.stop()  # no-op before start
    await worker.stop()  # still no-op
