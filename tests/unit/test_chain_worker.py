"""Tests for the background chain integrity census worker.

Pins the contract:
  - One tick enumerates the window and writes every session_id the
    reader returns to the SQLite chain_verification store.
  - Subsequent ticks REPLACE existing rows (idempotent upsert) and
    PRUNE rows whose session_id is no longer in the window.
  - Reader/store failures inside _tick_once are swallowed (fail-open);
    the worker keeps ticking on the next interval.
  - stop() drains cleanly.
"""
from __future__ import annotations

import os
import types

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _stub_async_reader(verifications):
    """Async reader whose get_chain_verification_report returns ``verifications``."""
    reader = types.SimpleNamespace()

    async def _report(start, end, sample_limit=50):
        # Echo the window only via the verifications list — tests pass
        # whatever they want returned.
        return list(verifications)

    async def _verify(sid):
        return {"session_id": sid, "valid": True, "verification_level": "ok",
                "errors": [], "records_checked": 1}

    reader.get_chain_verification_report = _report
    reader.verify_chain = _verify
    return reader


@pytest.mark.anyio
async def test_tick_populates_store_with_session_ids(tmp_path):
    """One tick → every session in the reader's window appears in the store."""
    from gateway.compliance.chain_store import ChainVerificationStore
    from gateway.compliance.chain_worker import ChainIntegrityWorker

    verifications = [
        {"session_id": f"s{i}", "valid": True, "verification_level": "ok",
         "errors": [], "records_checked": 3}
        for i in range(5)
    ]
    reader = _stub_async_reader(verifications)
    store = ChainVerificationStore(str(tmp_path / "chain.db"))
    worker = ChainIntegrityWorker(
        reader, store,
        tick_interval_s=60.0, window_days=7,
        lock_path=str(tmp_path / "chain.lock"),
    )

    await worker._tick_once()

    rows = store.get_all()
    assert {r["session_id"] for r in rows} == {f"s{i}" for i in range(5)}
    assert worker.health["last_tick_ok"] is True
    assert worker.health["last_sessions_seen"] == 5
    assert store.get_meta("last_tick_at") is not None


@pytest.mark.anyio
async def test_subsequent_tick_with_no_new_sessions_does_not_grow_store(tmp_path):
    """Re-running the tick over the same window is idempotent — no row growth."""
    from gateway.compliance.chain_store import ChainVerificationStore
    from gateway.compliance.chain_worker import ChainIntegrityWorker

    verifications = [
        {"session_id": "s1", "valid": True, "verification_level": "ok",
         "errors": [], "records_checked": 1},
    ]
    reader = _stub_async_reader(verifications)
    store = ChainVerificationStore(str(tmp_path / "chain.db"))
    worker = ChainIntegrityWorker(
        reader, store, tick_interval_s=60.0, window_days=7,
        lock_path=str(tmp_path / "chain.lock"),
    )

    await worker._tick_once()
    assert store.count() == 1
    await worker._tick_once()
    # Idempotent: still one row for s1.
    assert store.count() == 1


@pytest.mark.anyio
async def test_sessions_outside_window_are_pruned(tmp_path):
    """Sessions present in tick N but absent from tick N+1 are removed."""
    from gateway.compliance.chain_store import ChainVerificationStore
    from gateway.compliance.chain_worker import ChainIntegrityWorker

    reader = types.SimpleNamespace()
    current = {"set": [
        {"session_id": "s1", "valid": True, "verification_level": "ok",
         "errors": [], "records_checked": 1},
        {"session_id": "s2", "valid": True, "verification_level": "ok",
         "errors": [], "records_checked": 1},
    ]}

    async def _report(start, end, sample_limit=50):
        return list(current["set"])
    reader.get_chain_verification_report = _report

    store = ChainVerificationStore(str(tmp_path / "chain.db"))
    worker = ChainIntegrityWorker(
        reader, store, tick_interval_s=60.0, window_days=7,
        lock_path=str(tmp_path / "chain.lock"),
    )

    await worker._tick_once()
    assert {r["session_id"] for r in store.get_all()} == {"s1", "s2"}

    # s1 ages out of the window — only s2 returned by the reader now.
    current["set"] = [
        {"session_id": "s2", "valid": True, "verification_level": "ok",
         "errors": [], "records_checked": 1},
    ]
    await worker._tick_once()
    assert {r["session_id"] for r in store.get_all()} == {"s2"}


@pytest.mark.anyio
async def test_reader_failure_does_not_kill_worker(tmp_path):
    """Reader exception inside _tick_once must NOT raise — fail-open contract."""
    from gateway.compliance.chain_store import ChainVerificationStore
    from gateway.compliance.chain_worker import ChainIntegrityWorker

    reader = types.SimpleNamespace()

    async def _boom(start, end, sample_limit=50):
        raise RuntimeError("walacor unreachable")
    reader.get_chain_verification_report = _boom

    store = ChainVerificationStore(str(tmp_path / "chain.db"))
    worker = ChainIntegrityWorker(
        reader, store, tick_interval_s=60.0,
        lock_path=str(tmp_path / "chain.lock"),
    )
    # Must not raise.
    await worker._tick_once()
    assert worker.health["last_tick_ok"] is False
    assert "walacor unreachable" in (worker.health["last_error"] or "")
    assert store.count() == 0


@pytest.mark.anyio
async def test_stop_drains_cleanly(tmp_path):
    """start() → stop() awaits the task with no leak."""
    from gateway.compliance.chain_store import ChainVerificationStore
    from gateway.compliance.chain_worker import ChainIntegrityWorker

    verifications = [
        {"session_id": "s1", "valid": True, "verification_level": "ok",
         "errors": [], "records_checked": 1},
    ]
    reader = _stub_async_reader(verifications)
    store = ChainVerificationStore(str(tmp_path / "chain.db"))
    worker = ChainIntegrityWorker(
        reader, store, tick_interval_s=60.0,
        lock_path=str(tmp_path / "chain.lock"),
    )
    worker.start()
    await worker.stop()
    assert worker._task is not None and worker._task.done()


@pytest.mark.anyio
async def test_follower_skips_tick_when_lock_held(tmp_path):
    """When another process holds the lock, this worker is a follower no-op."""
    import sys
    if sys.platform.startswith("win"):  # pragma: no cover
        pytest.skip("fcntl-based leader election is POSIX-only")
    import fcntl

    from gateway.compliance.chain_store import ChainVerificationStore
    from gateway.compliance.chain_worker import ChainIntegrityWorker

    lock_path = str(tmp_path / "chain.lock")
    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        store = ChainVerificationStore(str(tmp_path / "chain.db"))
        reader = _stub_async_reader([
            {"session_id": "s1", "valid": True, "verification_level": "ok",
             "errors": [], "records_checked": 1},
        ])
        worker = ChainIntegrityWorker(
            reader, store, tick_interval_s=60.0, lock_path=lock_path,
        )
        await worker._tick_once()
        # Follower must NOT have written anything to the store.
        assert store.count() == 0
        assert worker.health["last_was_leader"] is False
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
