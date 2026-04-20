"""Regression tests for per-session chain serialization.

Before the fix, concurrent `next_chain_values + update` pairs for the
same session_id could interleave: Request B's `next_chain_values` would
return the same `last_record_hash` as Request A's because A hadn't yet
called `update()`. The result was two records with the same
`previous_record_hash`, breaking Merkle-chain linkage.

The fix is `session_lock(session_id)` — a per-session asyncio.Lock that
the orchestrator holds across the entire (reserve → write → update)
critical section.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from gateway.pipeline.session_chain import (
    GENESIS_HASH,
    SessionChainTracker,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _record_hash(execution_id: str, prev: str, seq: int) -> str:
    """Cheap synthetic hash that mimics the real chain formula's shape."""
    canonical = f"{execution_id}|{prev}|{seq}"
    return hashlib.sha3_512(canonical.encode()).hexdigest()


@pytest.mark.anyio
async def test_session_lock_serializes_chain_transactions():
    """Two concurrent requests for the same session must produce a
    valid chain: each record's previous_record_hash must equal the
    preceding record's record_hash. The lock is required for this
    guarantee — without it both requests read the same prev_hash.
    """
    tracker = SessionChainTracker()
    session_id = "test-sess"
    results: list[tuple[int, str, str]] = []  # (seq, prev, hash)

    async def one_request(exec_id: str) -> None:
        async with tracker.session_lock(session_id):
            cv = await tracker.next_chain_values(session_id)
            seq, prev = cv.sequence_number, cv.previous_record_hash
            # Simulate record write work so the race window is wide.
            await asyncio.sleep(0.01)
            rec_hash = _record_hash(exec_id, prev, seq)
            await tracker.update(session_id, seq, rec_hash)
            results.append((seq, prev, rec_hash))

    # Fire 10 concurrent requests for the SAME session.
    await asyncio.gather(*(one_request(f"exec-{i}") for i in range(10)))

    # Sort by seq so we can walk the chain
    results.sort(key=lambda t: t[0])
    seqs = [r[0] for r in results]
    assert seqs == list(range(10)), f"expected contiguous 0..9, got {seqs}"

    # Chain linkage: each record's prev must equal the previous record's hash.
    expected_prev = GENESIS_HASH
    for seq, prev, rec_hash in results:
        assert prev == expected_prev, (
            f"seq {seq}: prev mismatch — got {prev[:16]}, "
            f"expected {expected_prev[:16]}"
        )
        expected_prev = rec_hash


@pytest.mark.anyio
async def test_session_lock_does_not_block_other_sessions():
    """The per-session lock must only serialize same-session — distinct
    sessions run concurrently. Otherwise a single slow session would
    stall the entire gateway.
    """
    tracker = SessionChainTracker()
    running: set[str] = set()
    max_concurrent = [0]

    async def one_request(sid: str) -> None:
        async with tracker.session_lock(sid):
            running.add(sid)
            max_concurrent[0] = max(max_concurrent[0], len(running))
            await asyncio.sleep(0.05)
            running.discard(sid)

    # 6 distinct sessions firing concurrently.
    await asyncio.gather(*(one_request(f"sess-{i}") for i in range(6)))

    assert max_concurrent[0] >= 4, (
        f"expected >= 4 concurrent sessions, observed max {max_concurrent[0]}"
    )


@pytest.mark.anyio
async def test_session_lock_is_stable_across_calls():
    """Multiple calls to session_lock(same_id) must return the SAME
    lock instance — otherwise serialization is broken (each request
    holds its own private lock). This test guards against a
    `setdefault`-style regression where a fresh lock gets created
    every call.
    """
    tracker = SessionChainTracker()
    lock_a = tracker.session_lock("alpha")
    lock_b = tracker.session_lock("alpha")
    lock_c = tracker.session_lock("beta")
    assert lock_a is lock_b
    assert lock_a is not lock_c
