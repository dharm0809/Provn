# src/gateway/adaptive/resource_monitor.py
"""Runtime resource monitoring — disk, connections, provider health.

Tracks provider error rates using a sliding window and implements
LiteLLM-style cooldown when failure rates exceed thresholds.
"""
from __future__ import annotations

import logging
import shutil
import time
from collections import defaultdict, deque
from typing import Any

from gateway.adaptive.interfaces import ResourceMonitor, ResourceStatus

logger = logging.getLogger(__name__)


class DefaultResourceMonitor(ResourceMonitor):
    """Monitors disk space and provider error rates."""

    def __init__(self, wal_path: str, min_free_pct: float = 5.0,
                 window_seconds: float = 60.0, cooldown_seconds: float = 30.0,
                 failure_threshold: float = 0.5, min_samples: int = 3):
        self._wal_path = wal_path
        self._min_free_pct = min_free_pct
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._failure_threshold = failure_threshold
        self._min_samples = min_samples
        self._provider_results: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=100))
        self._active_requests = 0

    async def check(self) -> ResourceStatus:
        try:
            usage = shutil.disk_usage(self._wal_path)
            free_pct = round((usage.free / usage.total) * 100, 1)
            healthy = free_pct > self._min_free_pct
        except OSError:
            free_pct = 0.0
            healthy = False

        return ResourceStatus(
            disk_free_pct=free_pct,
            disk_healthy=healthy,
            active_requests=self._active_requests,
            provider_error_rates=self._get_error_rates())

    def record_provider_result(self, provider: str, success: bool) -> None:
        self._provider_results[provider].append((time.time(), success))

    def get_provider_cooldown(self, provider: str) -> float | None:
        results = self._provider_results.get(provider)
        if not results:
            return None
        cutoff = time.time() - self._window_seconds
        recent = [(t, ok) for t, ok in results if t > cutoff]
        if len(recent) < self._min_samples:
            return None
        fail_count = sum(1 for _, ok in recent if not ok)
        fail_rate = fail_count / len(recent)
        if fail_rate > self._failure_threshold:
            return self._cooldown_seconds
        return None

    def increment_active(self) -> None:
        self._active_requests += 1

    def decrement_active(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)

    def _get_error_rates(self) -> dict[str, float]:
        rates = {}
        cutoff = time.time() - self._window_seconds
        for provider, results in self._provider_results.items():
            recent = [(t, ok) for t, ok in results if t > cutoff]
            if recent:
                rates[provider] = round(
                    sum(1 for _, ok in recent if not ok) / len(recent), 2)
        return rates
