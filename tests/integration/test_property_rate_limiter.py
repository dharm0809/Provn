"""Phase C3: Property-based tests for SlidingWindowRateLimiter invariants."""

from __future__ import annotations

import asyncio
import time
import sys
import os

from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from gateway.pipeline.rate_limiter import SlidingWindowRateLimiter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fire_n(limiter, key, n, limit, window=60.0):
    """Fire n requests synchronously, return list of (allowed, remaining)."""
    results = []

    async def _go():
        for _ in range(n):
            r = await limiter.check(key, limit, window_seconds=window)
            results.append(r)

    asyncio.run(_go())
    return results


# ---------------------------------------------------------------------------
# Test 1: Cap invariant
# ---------------------------------------------------------------------------

def test_cap_invariant():
    lim = SlidingWindowRateLimiter()
    limit = 10
    n = limit + 15
    results = _fire_n(lim, "key-cap", n, limit, window=60.0)
    allowed = [r for r in results if r[0]]
    denied = [r for r in results if not r[0]]
    assert len(allowed) == limit
    assert len(denied) == n - limit


# ---------------------------------------------------------------------------
# Test 2: Remaining count
# ---------------------------------------------------------------------------

def test_remaining_count():
    lim = SlidingWindowRateLimiter()
    limit = 5

    async def _go():
        results = []
        for _ in range(limit):
            r = await lim.check("key-rem", limit, window_seconds=60.0)
            results.append(r)
        return results

    results = asyncio.run(_go())
    for i, (allowed, remaining) in enumerate(results):
        assert allowed
        assert remaining == limit - (i + 1)


# ---------------------------------------------------------------------------
# Test 3: Different keys are isolated
# ---------------------------------------------------------------------------

def test_key_isolation():
    lim = SlidingWindowRateLimiter()
    limit = 3
    results_a = _fire_n(lim, "key-iso-a", limit + 2, limit, window=60.0)
    results_b = _fire_n(lim, "key-iso-b", limit + 2, limit, window=60.0)

    # key-a: first `limit` allowed, rest denied
    assert sum(1 for r in results_a if r[0]) == limit
    # key-b: also first `limit` allowed (independent)
    assert sum(1 for r in results_b if r[0]) == limit


# ---------------------------------------------------------------------------
# Test 4: After window expires, limit resets
# ---------------------------------------------------------------------------

def test_window_reset():
    lim = SlidingWindowRateLimiter()
    limit = 3
    window = 0.1  # 100ms

    async def _go():
        # Fill up the window
        for _ in range(limit):
            allowed, _ = await lim.check("key-reset", limit, window_seconds=window)
            assert allowed

        # Next should be denied
        allowed, _ = await lim.check("key-reset", limit, window_seconds=window)
        assert not allowed

        # Sleep past the window
        await asyncio.sleep(window + 0.05)

        # Should be allowed again
        allowed, _ = await lim.check("key-reset", limit, window_seconds=window)
        assert allowed

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Test 5: Property — exactly limit allowed, extra denied
# ---------------------------------------------------------------------------

@given(
    limit=st.integers(min_value=1, max_value=20),
    extra=st.integers(min_value=1, max_value=10),
)
@h_settings(max_examples=40, deadline=5000)
def test_property_cap(limit, extra):
    lim = SlidingWindowRateLimiter()
    key = f"prop-{limit}-{extra}"
    n = limit + extra
    results = _fire_n(lim, key, n, limit, window=60.0)
    allowed = sum(1 for r in results if r[0])
    denied = sum(1 for r in results if not r[0])
    assert allowed == limit
    assert denied == extra


# ---------------------------------------------------------------------------
# Test 6: Limit=1
# ---------------------------------------------------------------------------

def test_limit_one():
    lim = SlidingWindowRateLimiter()
    results = _fire_n(lim, "key-limit1", 5, 1, window=60.0)
    assert results[0][0] is True   # first allowed
    for r in results[1:]:
        assert r[0] is False        # all others denied


# ---------------------------------------------------------------------------
# Test 7: Empty window always allows first request
# ---------------------------------------------------------------------------

def test_empty_window_first_request_allowed():
    lim = SlidingWindowRateLimiter()
    for i in range(10):
        result = _fire_n(lim, f"fresh-key-{i}", 1, limit=5, window=60.0)
        assert result[0][0] is True
