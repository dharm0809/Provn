"""EWMA-based latency anomaly detection per provider."""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


class _EWMAState:
    """Exponentially weighted moving average + variance tracker."""

    __slots__ = ("mean", "var", "count", "_alpha")

    def __init__(self, alpha: float = 0.3) -> None:
        self._alpha = alpha
        self.mean = 0.0
        self.var = 0.0
        self.count = 0

    def update(self, value: float) -> None:
        self.count += 1
        if self.count == 1:
            self.mean = value
            self.var = 0.0
            return
        diff = value - self.mean
        self.mean += self._alpha * diff
        self.var = (1 - self._alpha) * (self.var + self._alpha * diff * diff)

    @property
    def stddev(self) -> float:
        return math.sqrt(self.var) if self.var > 0 else 0.0


class LatencyAnomalyDetector:
    """Per-provider latency anomaly detector using EWMA + 3-sigma rule.

    After a warm-up period (min_samples), any latency exceeding
    mean + sigma_threshold * stddev is flagged as anomalous.
    """

    def __init__(
        self,
        alpha: float = 0.3,
        sigma_threshold: float = 3.0,
        min_samples: int = 10,
    ) -> None:
        self._alpha = alpha
        self._sigma_threshold = sigma_threshold
        self._min_samples = min_samples
        self._states: dict[str, _EWMAState] = {}

    def record(self, provider: str, latency: float) -> bool:
        """Record a latency observation. Returns True if anomalous."""
        state = self._states.get(provider)
        if state is None:
            state = _EWMAState(alpha=self._alpha)
            self._states[provider] = state

        is_anomaly = False
        if state.count >= self._min_samples:
            sd = state.stddev
            if sd > 0:
                threshold = state.mean + self._sigma_threshold * sd
                if latency > threshold:
                    is_anomaly = True
            else:
                # Zero variance (constant latencies) — any meaningful deviation is anomalous.
                # Use 10% of the mean as a minimum noise floor.
                min_band = max(state.mean * 0.1, 1e-6)
                threshold = state.mean + self._sigma_threshold * min_band
                if latency > threshold:
                    is_anomaly = True
            if is_anomaly:
                logger.warning(
                    "Latency anomaly for %s: %.3fs (mean=%.3fs, stddev=%.3fs, threshold=%.3fs)",
                    provider, latency, state.mean, sd, threshold,
                )

        state.update(latency)
        return is_anomaly

    def get_stats(self, provider: str) -> dict | None:
        """Return current EWMA stats for a provider."""
        state = self._states.get(provider)
        if state is None:
            return None
        return {
            "mean": round(state.mean, 6),
            "stddev": round(state.stddev, 6),
            "count": state.count,
            "threshold": round(state.mean + self._sigma_threshold * state.stddev, 6),
        }


# Module-level singleton — used by orchestrator
latency_detector = LatencyAnomalyDetector()
