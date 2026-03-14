"""Semantic cache — Phase 1: exact-match via SHA-256.

Caches non-streaming LLM responses keyed by (model, prompt_hash).
Phase 2 (embedding-based similarity search) is a future extension point.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    response_body: bytes
    status_code: int
    content_type: str
    created_at: float
    hit_count: int = 0


class SemanticCache:
    """In-memory exact-match response cache with TTL and LRU-style eviction."""

    def __init__(self, max_entries: int = 10000, ttl: int = 3600) -> None:
        self._cache: dict[str, CacheEntry] = {}
        self._max_entries = max_entries
        self._ttl = ttl

    def _key(self, model: str, prompt: str) -> str:
        return hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()

    def get(self, model: str, prompt: str) -> CacheEntry | None:
        """Return cached entry or None if missing/expired."""
        key = self._key(model, prompt)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self._ttl:
            del self._cache[key]
            return None
        entry.hit_count += 1
        return entry

    def put(
        self,
        model: str,
        prompt: str,
        response_body: bytes,
        status_code: int = 200,
        content_type: str = "application/json",
    ) -> None:
        """Store a response in the cache."""
        if len(self._cache) >= self._max_entries:
            # Evict the oldest entry (min created_at)
            oldest = min(self._cache, key=lambda k: self._cache[k].created_at)
            del self._cache[oldest]
        key = self._key(model, prompt)
        self._cache[key] = CacheEntry(
            response_body=response_body,
            status_code=status_code,
            content_type=content_type,
            created_at=time.monotonic(),
        )

    def invalidate(self, model: str, prompt: str) -> bool:
        """Remove a specific entry. Returns True if it existed."""
        key = self._key(model, prompt)
        return self._cache.pop(key, None) is not None

    def clear(self) -> None:
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)

    def stats(self) -> dict:
        total_hits = sum(e.hit_count for e in self._cache.values())
        return {
            "size": self.size,
            "total_hits": total_hits,
            "max_entries": self._max_entries,
            "ttl": self._ttl,
        }
