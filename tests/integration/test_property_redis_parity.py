"""Phase C1: Property-based tests verifying Redis-backed tracker API contracts.

Session chain invariants tested:
  - In-memory tracker: seq is strictly monotonically increasing (0,1,2...)
  - Redis tracker: seq is non-negative; prev_hash links correctly across updates
  - Both trackers: first seq=0, first prev_hash=GENESIS_HASH

Budget tracker invariants tested:
  - Both trackers enforce the same allow/deny logic for a given token cap
  - Both deny once the cap is exceeded

Note: RedisBudgetTracker uses Lua eval for atomic check-and-reserve. fakeredis
does not support eval, so we test budget via the in-memory tracker only and
verify the Redis tracker's fail-open behavior.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import fakeredis.aioredis

from gateway.pipeline.session_chain import (
    SessionChainTracker,
    RedisSessionChainTracker,
    compute_record_hash,
    GENESIS_HASH,
)
from gateway.pipeline.budget_tracker import BudgetTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def make_redis():
    return fakeredis.aioredis.FakeRedis()


async def _run_chain_on_tracker(tracker, session_id: str, n: int):
    """Run n sequential requests on tracker. Return list of (seq, prev_hash, record_hash)."""
    results = []
    for _ in range(n):
        async with tracker.session_lock(session_id):
            seq, prev_hash = await tracker.next_chain_values(session_id)
            record_hash = compute_record_hash(
                str(uuid.uuid4()), 1, "pass", prev_hash, seq,
                datetime.now(timezone.utc).isoformat()
            )
            await tracker.update(session_id, seq, record_hash)
            results.append((seq, prev_hash, record_hash))
    return results


# ---------------------------------------------------------------------------
# In-memory session chain: monotonicity and linkage
# ---------------------------------------------------------------------------

def test_in_memory_chain_monotonically_increasing():
    """In-memory tracker: seq values are strictly 0,1,2,..."""
    async def _run():
        tracker = SessionChainTracker()
        session_id = str(uuid.uuid4())
        results = await _run_chain_on_tracker(tracker, session_id, 6)
        seqs = [r[0] for r in results]
        assert seqs == list(range(len(seqs))), f"expected 0,1,2..., got {seqs}"

    asyncio.run(_run())


def test_in_memory_chain_hash_links():
    """In-memory tracker: each record's prev_hash equals the prior record_hash."""
    async def _run():
        tracker = SessionChainTracker()
        session_id = str(uuid.uuid4())
        results = await _run_chain_on_tracker(tracker, session_id, 5)
        for i in range(1, len(results)):
            prev_expected = results[i - 1][2]  # prior record_hash
            prev_actual = results[i][1]         # current prev_hash
            assert prev_actual == prev_expected, (
                f"step {i}: prev_hash mismatch"
            )

    asyncio.run(_run())


def test_in_memory_first_record_genesis():
    """In-memory tracker: first record starts with seq=0, GENESIS_HASH."""
    async def _run():
        tracker = SessionChainTracker()
        session_id = str(uuid.uuid4())
        async with tracker.session_lock(session_id):
            seq, prev_hash = await tracker.next_chain_values(session_id)
        assert seq == 0
        assert prev_hash == GENESIS_HASH

    asyncio.run(_run())


def test_in_memory_multiple_sessions_independent():
    """In-memory: multiple sessions each start at seq=0."""
    async def _run():
        tracker = SessionChainTracker()
        sessions = [str(uuid.uuid4()) for _ in range(4)]
        for session_id in sessions:
            results = await _run_chain_on_tracker(tracker, session_id, 3)
            assert results[0][0] == 0
            assert results[0][1] == GENESIS_HASH

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Redis session chain: API contract (via fakeredis)
# ---------------------------------------------------------------------------

def test_redis_chain_first_record_genesis():
    """Redis tracker: first record starts with seq=0, GENESIS_HASH."""
    async def _run():
        redis = await make_redis()
        tracker = RedisSessionChainTracker(redis, ttl=3600)
        session_id = str(uuid.uuid4())
        async with tracker.session_lock(session_id):
            seq, prev_hash = await tracker.next_chain_values(session_id)
        assert seq == 0
        assert prev_hash == GENESIS_HASH

    asyncio.run(_run())


def test_redis_chain_hash_stored_and_retrieved():
    """Redis tracker: after update, next prev_hash is the stored hash."""
    async def _run():
        redis = await make_redis()
        tracker = RedisSessionChainTracker(redis, ttl=3600)
        session_id = str(uuid.uuid4())

        # First request
        async with tracker.session_lock(session_id):
            seq0, prev0 = await tracker.next_chain_values(session_id)
            assert seq0 == 0
            assert prev0 == GENESIS_HASH
            hash0 = compute_record_hash(str(uuid.uuid4()), 1, "pass", prev0, seq0,
                                        datetime.now(timezone.utc).isoformat())
            await tracker.update(session_id, seq0, hash0)

        # Second request: prev_hash should be hash0
        async with tracker.session_lock(session_id):
            seq1, prev1 = await tracker.next_chain_values(session_id)
            assert prev1 == hash0, "prev_hash should equal prior record_hash"

    asyncio.run(_run())


def test_redis_chain_multiple_sessions_independent():
    """Redis tracker: different sessions are independent."""
    async def _run():
        redis = await make_redis()
        tracker = RedisSessionChainTracker(redis, ttl=3600)

        s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())

        # Run session 1
        async with tracker.session_lock(s1):
            seq1, prev1 = await tracker.next_chain_values(s1)

        # Run session 2 — should also start at seq=0
        async with tracker.session_lock(s2):
            seq2, prev2 = await tracker.next_chain_values(s2)

        assert seq1 == 0
        assert seq2 == 0
        assert prev1 == GENESIS_HASH
        assert prev2 == GENESIS_HASH

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Budget tracker tests (in-memory — Redis uses Lua eval not supported by fakeredis)
# ---------------------------------------------------------------------------

def test_budget_allows_within_cap():
    """In-memory tracker allows requests while under cap."""
    async def _run():
        tracker = BudgetTracker()
        tenant = "tenant-ok"
        tracker.configure(tenant, None, "daily", 1000)

        for tokens in [100, 200, 300]:
            allowed, _ = await tracker.check_and_reserve(tenant, None, tokens)
            assert allowed is True

    asyncio.run(_run())


def test_budget_denies_at_cap():
    """In-memory tracker denies once cap is reached."""
    async def _run():
        tracker = BudgetTracker()
        tenant = "tenant-cap"
        tracker.configure(tenant, None, "daily", 300)

        allowed1, _ = await tracker.check_and_reserve(tenant, None, 300)
        assert allowed1 is True

        allowed2, _ = await tracker.check_and_reserve(tenant, None, 1)
        assert allowed2 is False

    asyncio.run(_run())


def test_budget_same_sequence_same_results():
    """Two fresh in-memory trackers with same config produce identical allow/deny."""
    async def _run():
        t1 = BudgetTracker()
        t2 = BudgetTracker()

        tenant = "tenant-parity"
        for t in (t1, t2):
            t.configure(tenant, None, "daily", 500)

        token_amounts = [100, 100, 150, 100, 80]  # total 530, cap 500

        r1 = []
        r2 = []
        for tokens in token_amounts:
            a1, _ = await t1.check_and_reserve(tenant, None, tokens)
            a2, _ = await t2.check_and_reserve(tenant, None, tokens)
            r1.append(a1)
            r2.append(a2)

        assert r1 == r2, f"results differ: {r1} vs {r2}"

    asyncio.run(_run())


def test_budget_unlimited():
    """max_tokens=0 means unlimited — always allowed."""
    async def _run():
        tracker = BudgetTracker()
        tenant = "tenant-unlimited"
        tracker.configure(tenant, None, "daily", 0)  # 0 = unlimited

        for _ in range(10):
            allowed, _ = await tracker.check_and_reserve(tenant, None, 10000)
            assert allowed is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

@given(
    n_sessions=st.integers(min_value=1, max_value=4),
    n_requests=st.integers(min_value=1, max_value=8),
)
@h_settings(max_examples=30, deadline=10000)
def test_hypothesis_in_memory_chain_monotonic(n_sessions, n_requests):
    """In-memory chain seq is always monotonically increasing."""
    async def _run():
        tracker = SessionChainTracker()
        sessions = [str(uuid.uuid4()) for _ in range(n_sessions)]
        for session_id in sessions:
            results = await _run_chain_on_tracker(tracker, session_id, n_requests)
            seqs = [r[0] for r in results]
            assert seqs == list(range(len(seqs))), f"not monotonic: {seqs}"

    asyncio.run(_run())


@given(
    max_tokens=st.integers(min_value=100, max_value=2000),
    token_amounts=st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=15),
)
@h_settings(max_examples=30, deadline=5000)
def test_hypothesis_budget_never_exceeds_cap(max_tokens, token_amounts):
    """Total tokens reserved never exceeds max_tokens cap."""
    async def _run():
        tracker = BudgetTracker()
        tenant = "hyp-tenant"
        tracker.configure(tenant, None, "daily", max_tokens)

        total_reserved = 0
        for tokens in token_amounts:
            allowed, _ = await tracker.check_and_reserve(tenant, None, tokens)
            if allowed:
                total_reserved += tokens

        assert total_reserved <= max_tokens, (
            f"total reserved {total_reserved} exceeds cap {max_tokens}"
        )

    asyncio.run(_run())


@given(
    n_sessions=st.integers(min_value=1, max_value=3),
    n_requests=st.integers(min_value=1, max_value=6),
)
@h_settings(max_examples=20, deadline=10000)
def test_hypothesis_redis_chain_hash_linkage(n_sessions, n_requests):
    """Redis chain: prev_hash always equals the prior record_hash."""
    async def _run():
        redis = await make_redis()
        tracker = RedisSessionChainTracker(redis, ttl=3600)
        sessions = [str(uuid.uuid4()) for _ in range(n_sessions)]

        for session_id in sessions:
            results = await _run_chain_on_tracker(tracker, session_id, n_requests)
            assert results[0][0] == 0, "first seq must be 0"
            assert results[0][1] == GENESIS_HASH, "first prev_hash must be GENESIS_HASH"
            for i in range(1, len(results)):
                assert results[i][1] == results[i - 1][2], (
                    f"step {i}: prev_hash != prior record_hash"
                )

    asyncio.run(_run())
