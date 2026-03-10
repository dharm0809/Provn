"""Sliding window rate limiter (in-memory and Redis-backed)."""

from __future__ import annotations

import time


class SlidingWindowRateLimiter:
    """In-memory sliding window counter for RPM rate limiting."""

    def __init__(self):
        self._windows: dict[str, list[float]] = {}

    async def check(self, key: str, limit: int, window_seconds: int | float = 60) -> tuple[bool, int]:
        """Check if request is within rate limit.

        Returns (allowed, remaining_count).
        """
        now = time.monotonic()
        cutoff = now - window_seconds

        if key not in self._windows:
            self._windows[key] = []

        # Prune expired entries
        window = self._windows[key]
        self._windows[key] = [t for t in window if t > cutoff]
        window = self._windows[key]

        if len(window) >= limit:
            return False, 0

        window.append(now)
        remaining = limit - len(window)
        return True, remaining

    def reset_time(self, key: str, window_seconds: int | float = 60) -> float:
        """Return Unix timestamp when the oldest entry in the window expires."""
        window = self._windows.get(key, [])
        if not window:
            return time.time() + window_seconds
        oldest = min(window)
        # Convert monotonic to wall clock
        elapsed = time.monotonic() - oldest
        return time.time() + (window_seconds - elapsed)


class RedisRateLimiter:
    """Redis-backed sliding window using sorted sets."""

    def __init__(self, redis_client):
        self._redis = redis_client

    async def check(self, key: str, limit: int, window_seconds: int | float = 60) -> tuple[bool, int]:
        """Atomic check using ZSET + pipeline."""
        now = time.time()
        cutoff = now - window_seconds
        redis_key = f"gateway:ratelimit:{key}"

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(redis_key, 0, cutoff)
        pipe.zcard(redis_key)
        pipe.zadd(redis_key, {str(now): now})
        pipe.expire(redis_key, int(window_seconds) + 1)
        results = await pipe.execute()

        count = results[1]  # zcard result before adding current
        if count >= limit:
            # Remove the entry we just added
            await self._redis.zrem(redis_key, str(now))
            return False, 0

        remaining = limit - count - 1
        return True, max(remaining, 0)
