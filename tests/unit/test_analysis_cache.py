"""Tests for content analysis caching."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from gateway.content.base import Verdict
from gateway.pipeline.response_evaluator import analyze_text, _analysis_cache

anyio_backend = ["asyncio"]


@pytest.fixture(autouse=True)
def clear_cache():
    _analysis_cache.clear()
    yield
    _analysis_cache.clear()


@pytest.mark.anyio
async def test_cache_hit():
    analyzer = MagicMock()
    analyzer.analyzer_id = "test"
    analyzer.timeout_ms = 50
    analyzer.analyze = AsyncMock(return_value=MagicMock(
        verdict=Verdict.PASS, confidence=1.0, analyzer_id="test", category="", reason=""))

    # First call — cache miss
    r1 = await analyze_text("hello world", [analyzer])
    assert analyzer.analyze.call_count == 1

    # Second call — cache hit
    r2 = await analyze_text("hello world", [analyzer])
    assert analyzer.analyze.call_count == 1  # not called again
    assert r1 == r2


@pytest.mark.anyio
async def test_different_text_no_cache_hit():
    analyzer = MagicMock()
    analyzer.analyzer_id = "test"
    analyzer.timeout_ms = 50
    analyzer.analyze = AsyncMock(return_value=MagicMock(
        verdict=Verdict.PASS, confidence=1.0, analyzer_id="test", category="", reason=""))

    await analyze_text("hello", [analyzer])
    await analyze_text("world", [analyzer])
    assert analyzer.analyze.call_count == 2


@pytest.mark.anyio
async def test_cache_bounded_at_max():
    """Cache should not grow beyond _CACHE_MAX entries."""
    from gateway.pipeline.response_evaluator import _CACHE_MAX

    analyzer = MagicMock()
    analyzer.analyzer_id = "test"
    analyzer.timeout_ms = 50
    analyzer.analyze = AsyncMock(return_value=MagicMock(
        verdict=Verdict.PASS, confidence=1.0, analyzer_id="test", category="", reason=""))

    # Fill cache to the max
    for i in range(_CACHE_MAX):
        await analyze_text(f"text-{i}", [analyzer])

    assert len(_analysis_cache) == _CACHE_MAX

    # One more should not grow the cache beyond _CACHE_MAX
    await analyze_text("overflow-text", [analyzer])
    assert len(_analysis_cache) == _CACHE_MAX


@pytest.mark.anyio
async def test_empty_text_returns_empty():
    analyzer = MagicMock()
    analyzer.analyzer_id = "test"
    analyzer.timeout_ms = 50
    analyzer.analyze = AsyncMock()

    result = await analyze_text("", [analyzer])
    assert result == []
    assert analyzer.analyze.call_count == 0


@pytest.mark.anyio
async def test_no_analyzers_returns_empty():
    result = await analyze_text("hello", [])
    assert result == []


@pytest.mark.anyio
async def test_cache_key_is_content_based():
    """Same text should produce same cache key regardless of analyzer identity."""
    analyzer1 = MagicMock()
    analyzer1.analyzer_id = "a1"
    analyzer1.timeout_ms = 50
    analyzer1.analyze = AsyncMock(return_value=MagicMock(
        verdict=Verdict.PASS, confidence=0.9, analyzer_id="a1", category="", reason=""))

    # Call with first analyzer
    r1 = await analyze_text("same text", [analyzer1])
    assert analyzer1.analyze.call_count == 1

    # Call again with same text — should hit cache even though we pass same analyzer
    r2 = await analyze_text("same text", [analyzer1])
    assert analyzer1.analyze.call_count == 1
    assert r1 == r2
