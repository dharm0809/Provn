"""Tests for SemanticCache (B.4 exact-match tier)."""
import time
import pytest
from gateway.cache.semantic_cache import CacheEntry, SemanticCache


def test_cache_miss():
    cache = SemanticCache()
    assert cache.get("model", "hello") is None


def test_cache_put_and_get():
    cache = SemanticCache()
    cache.put("gpt-4", "hello world", b'{"content":"hi"}')
    entry = cache.get("gpt-4", "hello world")
    assert entry is not None
    assert entry.response_body == b'{"content":"hi"}'


def test_cache_miss_different_model():
    cache = SemanticCache()
    cache.put("gpt-4", "hello", b"response")
    assert cache.get("gpt-3.5", "hello") is None


def test_cache_miss_different_prompt():
    cache = SemanticCache()
    cache.put("model", "hello", b"response")
    assert cache.get("model", "world") is None


def test_cache_ttl_expiry():
    cache = SemanticCache(ttl=60)
    cache.put("model", "hello", b"response")
    # Backdate the entry so it appears expired
    key = cache._key("model", "hello")
    cache._cache[key].created_at = time.monotonic() - 61
    assert cache.get("model", "hello") is None


def test_cache_ttl_zero_expires_immediately():
    cache = SemanticCache(ttl=0)
    cache.put("model", "hello", b"response")
    # With ttl=0, any positive elapsed time triggers expiry
    key = cache._key("model", "hello")
    cache._cache[key].created_at = time.monotonic() - 1
    assert cache.get("model", "hello") is None


def test_cache_eviction():
    cache = SemanticCache(max_entries=2)
    cache.put("m", "prompt1", b"r1")
    cache.put("m", "prompt2", b"r2")
    cache.put("m", "prompt3", b"r3")  # triggers eviction of oldest
    assert cache.size == 2


def test_cache_eviction_removes_oldest():
    cache = SemanticCache(max_entries=2)
    cache.put("m", "prompt1", b"r1")
    # Small sleep to ensure created_at ordering
    key1 = cache._key("m", "prompt1")
    cache._cache[key1].created_at = time.monotonic() - 2
    cache.put("m", "prompt2", b"r2")
    cache.put("m", "prompt3", b"r3")  # evicts prompt1 (oldest)
    assert cache.get("m", "prompt1") is None
    assert cache.get("m", "prompt2") is not None or cache.get("m", "prompt3") is not None


def test_cache_hit_count():
    cache = SemanticCache()
    cache.put("m", "hello", b"response")
    cache.get("m", "hello")
    cache.get("m", "hello")
    key = cache._key("m", "hello")
    assert cache._cache[key].hit_count == 2


def test_invalidate_existing():
    cache = SemanticCache()
    cache.put("m", "hello", b"response")
    assert cache.invalidate("m", "hello") is True
    assert cache.get("m", "hello") is None


def test_invalidate_missing():
    cache = SemanticCache()
    assert cache.invalidate("m", "hello") is False


def test_clear():
    cache = SemanticCache()
    cache.put("m", "a", b"1")
    cache.put("m", "b", b"2")
    cache.clear()
    assert cache.size == 0


def test_stats():
    cache = SemanticCache(max_entries=100, ttl=60)
    cache.put("m", "hello", b"r")
    cache.get("m", "hello")
    stats = cache.stats()
    assert stats["size"] == 1
    assert stats["total_hits"] == 1
    assert stats["max_entries"] == 100
    assert stats["ttl"] == 60


def test_stats_empty():
    cache = SemanticCache(max_entries=50, ttl=30)
    stats = cache.stats()
    assert stats["size"] == 0
    assert stats["total_hits"] == 0


def test_key_uniqueness():
    """Different model+prompt combos must produce different keys."""
    cache = SemanticCache()
    k1 = cache._key("model-a", "prompt")
    k2 = cache._key("model-b", "prompt")
    k3 = cache._key("model-a", "different")
    assert k1 != k2
    assert k1 != k3
    assert k2 != k3


def test_content_type_stored():
    cache = SemanticCache()
    cache.put("m", "p", b"body", status_code=200, content_type="text/event-stream")
    entry = cache.get("m", "p")
    assert entry is not None
    assert entry.content_type == "text/event-stream"
    assert entry.status_code == 200
