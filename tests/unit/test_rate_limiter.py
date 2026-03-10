"""Unit tests for sliding window rate limiter."""

import time

import pytest

from gateway.pipeline.rate_limiter import SlidingWindowRateLimiter


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_within_limit_allowed():
    """5 requests under limit of 10 — all allowed."""
    rl = SlidingWindowRateLimiter()
    for _ in range(5):
        allowed, remaining = await rl.check("user:model", limit=10, window_seconds=60)
        assert allowed


@pytest.mark.anyio
async def test_exceeds_limit_denied():
    """11th request under limit of 10 — denied."""
    rl = SlidingWindowRateLimiter()
    for i in range(10):
        allowed, _ = await rl.check("user:model", limit=10, window_seconds=60)
        assert allowed
    allowed, remaining = await rl.check("user:model", limit=10, window_seconds=60)
    assert not allowed
    assert remaining == 0


@pytest.mark.anyio
async def test_window_slides():
    """Requests expire after window_seconds."""
    rl = SlidingWindowRateLimiter()
    # Fill to limit with a very short window
    for _ in range(5):
        await rl.check("user:model", limit=5, window_seconds=0.01)

    # Should be denied
    allowed, _ = await rl.check("user:model", limit=5, window_seconds=0.01)
    assert not allowed

    # Wait for window to expire
    time.sleep(0.02)

    # Should be allowed again
    allowed, remaining = await rl.check("user:model", limit=5, window_seconds=0.01)
    assert allowed


@pytest.mark.anyio
async def test_per_user_per_model_isolation():
    """Different keys don't interfere."""
    rl = SlidingWindowRateLimiter()
    # Fill up key A
    for _ in range(3):
        await rl.check("userA:gpt-4", limit=3, window_seconds=60)
    allowed_a, _ = await rl.check("userA:gpt-4", limit=3, window_seconds=60)
    assert not allowed_a

    # Key B should still be allowed
    allowed_b, _ = await rl.check("userB:gpt-4", limit=3, window_seconds=60)
    assert allowed_b

    # Same user, different model
    allowed_c, _ = await rl.check("userA:claude-3", limit=3, window_seconds=60)
    assert allowed_c


@pytest.mark.anyio
async def test_remaining_count_accurate():
    """Remaining count decreases correctly."""
    rl = SlidingWindowRateLimiter()
    _, remaining = await rl.check("user:model", limit=5, window_seconds=60)
    assert remaining == 4
    _, remaining = await rl.check("user:model", limit=5, window_seconds=60)
    assert remaining == 3
    _, remaining = await rl.check("user:model", limit=5, window_seconds=60)
    assert remaining == 2
