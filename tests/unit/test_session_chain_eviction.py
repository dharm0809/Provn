"""Tests for O(1) session chain eviction via OrderedDict."""
import pytest
from gateway.pipeline.session_chain import SessionChainTracker


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_eviction_removes_oldest():
    """When over capacity, oldest session is evicted."""
    tracker = SessionChainTracker(max_sessions=3, ttl_seconds=3600)
    for i in range(4):
        await tracker.update(f"s{i}", i, f"hash{i}")
    assert tracker.active_session_count() == 3
    # s0 was oldest, should be evicted — next_chain_values creates fresh state at seq=0
    cv = await tracker.next_chain_values("s0")
    assert cv.sequence_number == 0


@pytest.mark.anyio
async def test_access_refreshes_lru_order():
    """Accessing a session moves it to end, preventing eviction."""
    tracker = SessionChainTracker(max_sessions=3, ttl_seconds=3600)
    await tracker.update("s0", 0, "h0")
    await tracker.update("s1", 0, "h1")
    await tracker.update("s2", 0, "h2")
    # Access s0 — moves it to end AND atomically reserves seq=1
    await tracker.next_chain_values("s0")
    # Add s3 — should evict s1 (now oldest), not s0
    await tracker.update("s3", 0, "h3")
    assert tracker.active_session_count() == 3
    # s0 still alive; previous next_chain_values reserved seq=1, this one gets seq=2
    cv = await tracker.next_chain_values("s0")
    assert cv.sequence_number == 2  # s0 still has state (seq was 0 → 1 → 2)
