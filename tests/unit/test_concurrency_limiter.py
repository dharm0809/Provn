"""Tests for Gradient2 adaptive concurrency limiter."""

from gateway.routing.concurrency import ConcurrencyLimiter, EWMATracker


def test_ewma_first_sample():
    t = EWMATracker(alpha=0.5)
    assert t.value is None
    val = t.update(10.0)
    assert val == 10.0


def test_ewma_convergence():
    t = EWMATracker(alpha=0.5)
    t.update(10.0)
    t.update(20.0)
    assert t.value == 15.0  # 0.5*20 + 0.5*10


def test_try_acquire_respects_limit():
    lim = ConcurrencyLimiter(min_limit=2, max_limit=2)
    assert lim.try_acquire() is True
    assert lim.try_acquire() is True
    assert lim.try_acquire() is False  # at limit


def test_release_frees_slot():
    lim = ConcurrencyLimiter(min_limit=1, max_limit=1)
    assert lim.try_acquire() is True
    assert lim.try_acquire() is False
    lim.release(0.1)
    assert lim.try_acquire() is True


def test_release_adjusts_limit_healthy():
    """Stable latency -> gradient >= 1 -> additive increase."""
    lim = ConcurrencyLimiter(min_limit=5, max_limit=100)
    lim.try_acquire()
    # Feed consistent latency (gradient ~= 1.0 -> healthy -> increase)
    for _ in range(10):
        lim.release(0.1)
    assert lim.limit > 5  # should have increased


def test_release_adjusts_limit_degraded():
    """Sudden latency spike -> gradient < 1 -> multiplicative decrease."""
    lim = ConcurrencyLimiter(min_limit=5, max_limit=100, short_alpha=0.9, long_alpha=0.01)
    # Build up a stable long EWMA
    lim.try_acquire()
    for _ in range(50):
        lim.release(0.1)
    initial_limit = lim.limit
    # Sudden spike: short EWMA jumps, long stays low -> gradient < 1
    for _ in range(5):
        lim.try_acquire()
        lim.release(10.0)  # 100x spike
    assert lim.limit < initial_limit  # should have decreased


def test_min_max_bounds():
    lim = ConcurrencyLimiter(min_limit=3, max_limit=10)
    # Force many healthy releases to push limit up
    for _ in range(100):
        lim.try_acquire()
        lim.release(0.1)
    assert lim.limit <= 10
    assert lim.limit >= 3


def test_snapshot():
    lim = ConcurrencyLimiter(min_limit=5, max_limit=50)
    snap = lim.snapshot()
    assert "limit" in snap
    assert "inflight" in snap
    assert snap["inflight"] == 0


def test_inflight_never_negative():
    """Release without acquire should not go below zero."""
    lim = ConcurrencyLimiter(min_limit=5, max_limit=50)
    lim.release(0.1)
    assert lim.inflight == 0


def test_snapshot_after_activity():
    """Snapshot reflects EWMA values after updates."""
    lim = ConcurrencyLimiter(min_limit=5, max_limit=50)
    lim.try_acquire()
    lim.release(0.5)
    snap = lim.snapshot()
    assert snap["short_ewma"] is not None
    assert snap["long_ewma"] is not None
    assert snap["short_ewma"] == 0.5
    assert snap["long_ewma"] == 0.5
