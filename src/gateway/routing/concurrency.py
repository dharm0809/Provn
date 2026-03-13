"""Netflix Gradient2 adaptive concurrency limiter."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class EWMATracker:
    """Exponentially Weighted Moving Average tracker."""

    def __init__(self, alpha: float = 0.1) -> None:
        self._alpha = alpha
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, sample: float) -> float:
        if self._value is None:
            self._value = sample
        else:
            self._value = self._alpha * sample + (1 - self._alpha) * self._value
        return self._value


class ConcurrencyLimiter:
    """Gradient2 adaptive concurrency limiter.

    - Tracks short-term and long-term EWMA of request latency
    - gradient = long_ewma / short_ewma
    - gradient >= 1.0 -> healthy -> additive increase (+1)
    - gradient < 1.0  -> degraded -> multiplicative decrease (*0.9)
    - Bounds: [min_limit, max_limit]
    """

    def __init__(
        self,
        min_limit: int = 5,
        max_limit: int = 100,
        short_alpha: float = 0.5,
        long_alpha: float = 0.1,
    ) -> None:
        self._min = min_limit
        self._max = max_limit
        self._limit: float = float(min_limit)
        self._inflight: int = 0
        self._short_ewma = EWMATracker(alpha=short_alpha)
        self._long_ewma = EWMATracker(alpha=long_alpha)

    @property
    def limit(self) -> int:
        return max(self._min, min(self._max, int(self._limit)))

    @property
    def inflight(self) -> int:
        return self._inflight

    def try_acquire(self) -> bool:
        """Try to acquire a concurrency slot. Returns False if at limit."""
        if self._inflight >= self.limit:
            return False
        self._inflight += 1
        return True

    def release(self, rtt_seconds: float) -> None:
        """Release a slot and update the limit based on observed latency."""
        self._inflight = max(0, self._inflight - 1)

        short_val = self._short_ewma.update(rtt_seconds)
        long_val = self._long_ewma.update(rtt_seconds)

        if short_val > 0:
            gradient = long_val / short_val
        else:
            gradient = 1.0

        if gradient >= 1.0:
            # Healthy: additive increase
            self._limit = min(self._max, self._limit + 1)
        else:
            # Degraded: multiplicative decrease
            self._limit = max(self._min, self._limit * 0.9)

    def snapshot(self) -> dict:
        return {
            "limit": self.limit,
            "inflight": self._inflight,
            "short_ewma": self._short_ewma.value,
            "long_ewma": self._long_ewma.value,
        }
