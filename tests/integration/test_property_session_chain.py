"""Phase B1: Property-based tests for session chain invariants."""

from __future__ import annotations

import asyncio
import sys
import os
import uuid

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from gateway.pipeline.session_chain import (
    SessionChainTracker,
    compute_record_hash,
    GENESIS_HASH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_chain(session_id: str, n: int):
    """Run n sequential requests on a single session. Return list of (seq, prev_hash, record_hash)."""
    tracker = SessionChainTracker()
    records = []

    async def _go():
        for i in range(n):
            async with tracker.session_lock(session_id):
                seq, prev_hash = await tracker.next_chain_values(session_id)
                ts = f"2026-01-01T00:00:{i:02d}Z"
                rh = compute_record_hash(
                    execution_id=f"exec-{i}",
                    policy_version=1,
                    policy_result="pass",
                    previous_record_hash=prev_hash,
                    sequence_number=seq,
                    timestamp=ts,
                )
                await tracker.update(session_id, seq, rh)
                records.append((seq, prev_hash, rh))

    asyncio.run(_go())
    return records


# ---------------------------------------------------------------------------
# Test 1: Genesis invariant
# ---------------------------------------------------------------------------

def test_genesis_invariant():
    sid = str(uuid.uuid4())
    records = _run_chain(sid, 1)
    seq, prev_hash, _ = records[0]
    assert seq == 0
    assert prev_hash == GENESIS_HASH
    assert GENESIS_HASH == "0" * 128


# ---------------------------------------------------------------------------
# Test 2: Contiguity
# ---------------------------------------------------------------------------

def test_sequence_contiguity():
    sid = str(uuid.uuid4())
    n = 8
    records = _run_chain(sid, n)
    seqs = [r[0] for r in records]
    assert seqs == list(range(n))


# ---------------------------------------------------------------------------
# Test 3: Chain linkage
# ---------------------------------------------------------------------------

def test_chain_linkage():
    sid = str(uuid.uuid4())
    records = _run_chain(sid, 6)
    for i in range(1, len(records)):
        _, prev_hash, _ = records[i]
        _, _, prev_record_hash = records[i - 1]
        assert prev_hash == prev_record_hash, (
            f"record[{i}].previous_record_hash != record[{i-1}].record_hash"
        )


# ---------------------------------------------------------------------------
# Test 4: Hash recomputation (determinism)
# ---------------------------------------------------------------------------

def test_hash_recomputation_determinism():
    kwargs = dict(
        execution_id="abc",
        policy_version=1,
        policy_result="pass",
        previous_record_hash=GENESIS_HASH,
        sequence_number=0,
        timestamp="2026-01-01T00:00:00Z",
    )
    h1 = compute_record_hash(**kwargs)
    h2 = compute_record_hash(**kwargs)
    assert h1 == h2
    assert len(h1) == 128  # SHA3-512 hex = 128 chars


# ---------------------------------------------------------------------------
# Test 5: Multi-session isolation
# ---------------------------------------------------------------------------

def test_multi_session_isolation():
    tracker = SessionChainTracker()
    sid_a = "session-a"
    sid_b = "session-b"

    async def _go():
        # 3 records for A, 5 for B
        for i in range(3):
            async with tracker.session_lock(sid_a):
                seq, prev = await tracker.next_chain_values(sid_a)
                rh = compute_record_hash("e", 1, "pass", prev, seq, f"t{i}")
                await tracker.update(sid_a, seq, rh)

        for i in range(5):
            async with tracker.session_lock(sid_b):
                seq, prev = await tracker.next_chain_values(sid_b)
                rh = compute_record_hash("e", 1, "pass", prev, seq, f"t{i}")
                await tracker.update(sid_b, seq, rh)

        # Check A's state hasn't been corrupted
        async with tracker.session_lock(sid_a):
            seq_a, _ = await tracker.next_chain_values(sid_a)
        async with tracker.session_lock(sid_b):
            seq_b, _ = await tracker.next_chain_values(sid_b)

        return seq_a, seq_b

    seq_a, seq_b = asyncio.run(_go())
    assert seq_a == 3   # A had 3 records, next is seq 3
    assert seq_b == 5   # B had 5 records, next is seq 5


# ---------------------------------------------------------------------------
# Test 6: Property — arbitrary session/request counts
# ---------------------------------------------------------------------------

@given(
    session_count=st.integers(min_value=1, max_value=5),
    requests_per_session=st.integers(min_value=1, max_value=10),
)
@h_settings(max_examples=30, deadline=5000)
def test_property_chain_valid_arbitrary(session_count, requests_per_session):
    tracker = SessionChainTracker()

    async def _go():
        for s in range(session_count):
            sid = f"session-{s}"
            prev_rh = GENESIS_HASH
            for i in range(requests_per_session):
                async with tracker.session_lock(sid):
                    seq, prev_hash = await tracker.next_chain_values(sid)
                    ts = f"t{s}-{i}"
                    rh = compute_record_hash("e", 1, "pass", prev_hash, seq, ts)
                    await tracker.update(sid, seq, rh)

                # Verify linkage
                assert seq == i, f"session {s} record {i}: expected seq={i} got {seq}"
                assert prev_hash == prev_rh, "chain linkage broken"
                prev_rh = rh

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Test 7: record_hash is stable (same inputs → same output)
# ---------------------------------------------------------------------------

@given(
    execution_id=st.text(min_size=1, max_size=50),
    policy_version=st.integers(min_value=0, max_value=100),
    policy_result=st.sampled_from(["pass", "fail", "warn"]),
    sequence_number=st.integers(min_value=0, max_value=1000),
    timestamp=st.text(min_size=1, max_size=30),
)
@h_settings(max_examples=50, deadline=2000)
def test_property_record_hash_stable(execution_id, policy_version, policy_result, sequence_number, timestamp):
    h1 = compute_record_hash(
        execution_id=execution_id,
        policy_version=policy_version,
        policy_result=policy_result,
        previous_record_hash=GENESIS_HASH,
        sequence_number=sequence_number,
        timestamp=timestamp,
    )
    h2 = compute_record_hash(
        execution_id=execution_id,
        policy_version=policy_version,
        policy_result=policy_result,
        previous_record_hash=GENESIS_HASH,
        sequence_number=sequence_number,
        timestamp=timestamp,
    )
    assert h1 == h2
    assert len(h1) == 128


# ---------------------------------------------------------------------------
# Test 8: Different inputs → different hashes (common case, not collision proof)
# ---------------------------------------------------------------------------

def test_different_inputs_different_hashes():
    base = dict(
        execution_id="exec-1",
        policy_version=1,
        policy_result="pass",
        previous_record_hash=GENESIS_HASH,
        sequence_number=0,
        timestamp="t1",
    )
    h1 = compute_record_hash(**base)
    # Change execution_id
    h2 = compute_record_hash(**{**base, "execution_id": "exec-2"})
    # Change sequence_number
    h3 = compute_record_hash(**{**base, "sequence_number": 1})
    assert h1 != h2
    assert h1 != h3
    assert h2 != h3
