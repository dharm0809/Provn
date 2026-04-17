"""Phase A4 — Property tests for BudgetTracker.

Invariant I5: sum of tokens billed ≤ configured cap; once cap reached,
subsequent requests return denied.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from gateway.pipeline.budget_tracker import BudgetTracker, BudgetState, _period_start


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def make_tracker() -> BudgetTracker:
    return BudgetTracker()


# ---------------------------------------------------------------------------
# 1. Budget cap never exceeded
# ---------------------------------------------------------------------------

@given(
    max_tokens=st.integers(min_value=1, max_value=10_000),
    requests=st.lists(st.integers(min_value=1, max_value=500), min_size=1, max_size=30),
)
@settings(max_examples=200)
def test_cap_never_exceeded(max_tokens, requests):
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", max_tokens)

    async def run_sequence():
        total_reserved = 0
        for est in requests:
            allowed, remaining = await tracker.check_and_reserve("tenant", None, est)
            if allowed:
                total_reserved += est
        return total_reserved

    total_reserved = run(run_sequence())
    assert total_reserved <= max_tokens


# ---------------------------------------------------------------------------
# 2. Unlimited budget (max_tokens=0) always allows
# ---------------------------------------------------------------------------

@given(
    requests=st.lists(st.integers(min_value=1, max_value=10_000), min_size=1, max_size=20),
)
@settings(max_examples=100)
def test_unlimited_always_allows(requests):
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", 0)  # 0 = unlimited

    async def run_all():
        for est in requests:
            allowed, remaining = await tracker.check_and_reserve("tenant", None, est)
            assert allowed is True
            assert remaining == -1

    run(run_all())


# ---------------------------------------------------------------------------
# 3. After cap reached → all subsequent requests denied
# ---------------------------------------------------------------------------

def test_after_cap_all_denied():
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", 100)

    async def run_it():
        # Exhaust the budget
        allowed, _ = await tracker.check_and_reserve("tenant", None, 100)
        assert allowed is True

        # Now all subsequent should be denied
        for _ in range(5):
            allowed2, remaining = await tracker.check_and_reserve("tenant", None, 1)
            assert allowed2 is False
            assert remaining >= 0

    run(run_it())


@given(
    max_tokens=st.integers(min_value=10, max_value=1000),
)
@settings(max_examples=50)
def test_after_exhaustion_denied_property(max_tokens):
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", max_tokens)

    async def run_it():
        # Consume all at once
        allowed, _ = await tracker.check_and_reserve("tenant", None, max_tokens)
        assert allowed is True
        # Next request must be denied
        allowed2, remaining = await tracker.check_and_reserve("tenant", None, 1)
        assert allowed2 is False
        assert remaining == 0

    run(run_it())


# ---------------------------------------------------------------------------
# 4. record_usage adjusts counter (refund on over-estimate)
# ---------------------------------------------------------------------------

def test_record_usage_refund():
    """Reserve 200, actual=150 → 50 tokens refunded → remaining increases."""
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", 1000)

    async def run_it():
        allowed, remaining_after = await tracker.check_and_reserve("tenant", None, 200)
        assert allowed is True
        assert remaining_after == 800  # 1000 - 200

        # actual=150, estimated=200 → delta = 150 - 200 = -50 → refund 50
        await tracker.record_usage("tenant", None, tokens=150, estimated=200)

        snap = await tracker.get_snapshot("tenant", None)
        assert snap["tokens_used"] == 150  # 200 - 50 refunded
        assert snap["max_tokens"] == 1000

    run(run_it())


def test_record_usage_extra_charge():
    """actual > estimated → extra tokens charged."""
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", 1000)

    async def run_it():
        await tracker.check_and_reserve("tenant", None, 100)
        # actual=150, estimated=100 → delta=+50 → more tokens used
        await tracker.record_usage("tenant", None, tokens=150, estimated=100)
        snap = await tracker.get_snapshot("tenant", None)
        assert snap["tokens_used"] == 150

    run(run_it())


# ---------------------------------------------------------------------------
# 5. Period reset — stale period_start triggers reset
# ---------------------------------------------------------------------------

def test_period_reset_daily():
    """Set period_start to yesterday → next check_and_reserve resets tokens_used."""
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", 100)

    async def run_it():
        # Exhaust budget
        await tracker.check_and_reserve("tenant", None, 100)
        snap = await tracker.get_snapshot("tenant", None)
        assert snap["tokens_used"] == 100

        # Manually rewind period_start to yesterday
        key = ("tenant", "")
        state = tracker._states[key]
        state.period_start = datetime.now(timezone.utc) - timedelta(days=1, seconds=1)

        # Next reservation should reset + allow
        allowed, _ = await tracker.check_and_reserve("tenant", None, 50)
        assert allowed is True

        snap2 = await tracker.get_snapshot("tenant", None)
        assert snap2["tokens_used"] == 50  # reset + new reservation

    run(run_it())


def test_period_reset_monthly():
    tracker = make_tracker()
    tracker.configure("tenant", None, "monthly", 500)

    async def run_it():
        await tracker.check_and_reserve("tenant", None, 500)

        key = ("tenant", "")
        state = tracker._states[key]
        # Move to last month
        state.period_start = datetime.now(timezone.utc).replace(day=1) - timedelta(days=1)

        allowed, _ = await tracker.check_and_reserve("tenant", None, 100)
        assert allowed is True

        snap = await tracker.get_snapshot("tenant", None)
        assert snap["tokens_used"] == 100

    run(run_it())


# ---------------------------------------------------------------------------
# 6. Different tenants are isolated
# ---------------------------------------------------------------------------

def test_tenants_isolated():
    tracker = make_tracker()
    tracker.configure("tenant_a", None, "daily", 100)
    tracker.configure("tenant_b", None, "daily", 100)

    async def run_it():
        # Exhaust tenant_a
        allowed_a, _ = await tracker.check_and_reserve("tenant_a", None, 100)
        assert allowed_a is True
        denied_a, _ = await tracker.check_and_reserve("tenant_a", None, 1)
        assert denied_a is False

        # tenant_b unaffected
        allowed_b, _ = await tracker.check_and_reserve("tenant_b", None, 50)
        assert allowed_b is True

    run(run_it())


@given(
    tokens_a=st.integers(min_value=1, max_value=500),
    tokens_b=st.integers(min_value=1, max_value=500),
)
@settings(max_examples=100)
def test_tenants_independent_property(tokens_a, tokens_b):
    tracker = make_tracker()
    tracker.configure("ta", None, "daily", tokens_a)
    tracker.configure("tb", None, "daily", tokens_b)

    async def run_it():
        # Exhaust ta
        await tracker.check_and_reserve("ta", None, tokens_a)
        # tb should still allow up to tokens_b
        allowed, _ = await tracker.check_and_reserve("tb", None, tokens_b)
        assert allowed is True

    run(run_it())


# ---------------------------------------------------------------------------
# 7. User-scoped vs tenant-level isolation
# ---------------------------------------------------------------------------

def test_user_scopes_independent():
    tracker = make_tracker()
    tracker.configure("tenant", "user1", "daily", 100)
    tracker.configure("tenant", "user2", "daily", 100)

    async def run_it():
        # Exhaust user1
        await tracker.check_and_reserve("tenant", "user1", 100)
        denied, _ = await tracker.check_and_reserve("tenant", "user1", 1)
        assert denied is False

        # user2 unaffected
        allowed, _ = await tracker.check_and_reserve("tenant", "user2", 50)
        assert allowed is True

    run(run_it())


def test_user_none_and_user_string_are_different_scopes():
    """(tenant, None) and (tenant, 'alice') are separate keys."""
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", 50)
    tracker.configure("tenant", "alice", "daily", 50)

    async def run_it():
        await tracker.check_and_reserve("tenant", None, 50)
        # alice unaffected
        allowed, _ = await tracker.check_and_reserve("tenant", "alice", 50)
        assert allowed is True

    run(run_it())


# ---------------------------------------------------------------------------
# 8. No budget configured → always allowed (unlimited sentinel)
# ---------------------------------------------------------------------------

@given(
    tenant=st.text(min_size=1, max_size=20),
    tokens=st.integers(min_value=1, max_value=100_000),
)
@settings(max_examples=100)
def test_no_budget_always_allowed(tenant, tokens):
    tracker = make_tracker()  # no configure call

    async def run_it():
        allowed, remaining = await tracker.check_and_reserve(tenant, None, tokens)
        assert allowed is True
        assert remaining == -1

    run(run_it())


# ---------------------------------------------------------------------------
# 9. remove() clears budget → subsequent requests unlimited
# ---------------------------------------------------------------------------

def test_remove_clears_budget():
    tracker = make_tracker()
    tracker.configure("tenant", None, "daily", 10)

    async def run_it():
        # Exhaust
        await tracker.check_and_reserve("tenant", None, 10)
        denied, _ = await tracker.check_and_reserve("tenant", None, 1)
        assert denied is False

        # Remove budget
        tracker.remove("tenant", None)

        # Now no budget → always allowed
        allowed, remaining = await tracker.check_and_reserve("tenant", None, 99999)
        assert allowed is True
        assert remaining == -1

    run(run_it())
