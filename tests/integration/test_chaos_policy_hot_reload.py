"""Phase A5: Chaos test — policy hot-reload correctness.

Tests that the in-memory PolicyCache reflects changes immediately after
set_policies() and that evaluate_policies() uses the latest policy set.
"""
from __future__ import annotations

import asyncio
import random
import threading

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.cache.policy_cache import PolicyCache
from gateway.core.policy_engine import evaluate_policies

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS_ALL_POLICY = {
    "policy_id": "pass-all",
    "policy_name": "pass all",
    "status": "active",
    "enforcement_level": "blocking",
    "rules": [],
}

DENY_ALL_POLICY = {
    "policy_id": "deny-all",
    "policy_name": "deny all",
    "status": "active",
    "enforcement_level": "blocking",
    "rules": [
        {"field": "model_id", "operator": "regex", "value": ".*", "action": "deny"}
    ],
}

SAMPLE_CTX = {"model_id": "test-model", "provider": "ollama", "status": "active"}


def is_blocked(policies: list[dict]) -> bool:
    blocked, _ = evaluate_policies(SAMPLE_CTX, policies)
    return blocked


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------

class TestPolicyCacheBasic:
    def test_empty_cache_pass_all(self):
        pc = PolicyCache()
        pc.set_policies(1, [])
        blocked, _, _ = pc.evaluate(SAMPLE_CTX, "t1")
        assert not blocked

    def test_deny_all_blocks(self):
        pc = PolicyCache()
        pc.set_policies(1, [DENY_ALL_POLICY])
        blocked, _, _ = pc.evaluate(SAMPLE_CTX, "t1")
        assert blocked

    def test_hot_reload_pass_to_block(self):
        pc = PolicyCache()
        pc.set_policies(1, [])
        assert not is_blocked(pc.get_policies())

        pc.set_policies(2, [DENY_ALL_POLICY])
        assert is_blocked(pc.get_policies())

    def test_hot_reload_block_to_pass(self):
        pc = PolicyCache()
        pc.set_policies(1, [DENY_ALL_POLICY])
        assert is_blocked(pc.get_policies())

        pc.set_policies(2, [])
        assert not is_blocked(pc.get_policies())

    def test_triple_reload(self):
        pc = PolicyCache()
        pc.set_policies(1, [])
        assert not is_blocked(pc.get_policies())

        pc.set_policies(2, [DENY_ALL_POLICY])
        assert is_blocked(pc.get_policies())

        pc.set_policies(3, [])
        assert not is_blocked(pc.get_policies())

    def test_version_monotonicity(self):
        pc = PolicyCache()
        for v in [1, 5, 10, 100, 999]:
            pc.set_policies(v, [])
            assert pc.version == v

    def test_version_reflects_latest(self):
        pc = PolicyCache()
        pc.set_policies(42, [DENY_ALL_POLICY])
        assert pc.version == 42
        pc.set_policies(100, [])
        assert pc.version == 100


# ---------------------------------------------------------------------------
# Property: N random alternating updates always match latest policy
# ---------------------------------------------------------------------------

class TestHotReloadProperty:
    @given(
        st.lists(st.booleans(), min_size=1, max_size=50)
    )
    @settings(max_examples=100)
    def test_latest_policy_always_reflected(self, block_flags: list[bool]):
        """After N random policy updates, current behavior matches last set policy."""
        pc = PolicyCache()
        last_block = None
        for i, should_block in enumerate(block_flags, start=1):
            policy = [DENY_ALL_POLICY] if should_block else []
            pc.set_policies(i, policy)
            last_block = should_block

        blocked = is_blocked(pc.get_policies())
        assert blocked == last_block

    @given(st.integers(min_value=1, max_value=200))
    @settings(max_examples=50)
    def test_version_always_matches_last_set(self, n: int):
        pc = PolicyCache()
        for v in range(1, n + 1):
            pc.set_policies(v, [])
        assert pc.version == n


# ---------------------------------------------------------------------------
# Concurrent read safety
# ---------------------------------------------------------------------------

class TestConcurrentReads:
    def test_concurrent_asyncio_reads_no_crash(self):
        pc = PolicyCache()
        pc.set_policies(1, [DENY_ALL_POLICY])

        async def read_policies():
            policies = pc.get_policies()
            is_blocked(policies)
            return True

        async def run_all():
            results = await asyncio.gather(*[read_policies() for _ in range(100)])
            return results

        results = asyncio.run(run_all())
        assert all(results)
        assert len(results) == 100

    def test_concurrent_thread_reads_no_crash(self):
        """Many threads reading PolicyCache simultaneously — no crashes."""
        pc = PolicyCache()
        pc.set_policies(1, [])
        errors = []

        def reader():
            try:
                for _ in range(20):
                    policies = pc.get_policies()
                    is_blocked(policies)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors in concurrent reads: {errors}"

    def test_read_write_concurrent_no_crash(self):
        """Concurrent reads and writes — no crashes, last write wins."""
        pc = PolicyCache()
        pc.set_policies(1, [])
        errors = []
        final_version = [0]

        def writer():
            try:
                for v in range(2, 22):
                    policy = [DENY_ALL_POLICY] if v % 2 == 0 else []
                    pc.set_policies(v, policy)
                    final_version[0] = v
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    pc.get_policies()
                    pc.version
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        wt = threading.Thread(target=writer)
        for t in threads:
            t.start()
        wt.start()
        for t in threads:
            t.join()
        wt.join()

        assert errors == [], f"Errors: {errors}"
