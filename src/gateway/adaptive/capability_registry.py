# src/gateway/adaptive/capability_registry.py
"""Model capability registry with TTL-based re-probing.

Replaces the simple _model_capabilities dict in orchestrator.py with
a richer registry that supports TTL expiry, model type classification,
per-model timeouts, and optional persistence to the control plane store.
"""
from __future__ import annotations

import logging
import time
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)


class ModelCapability(NamedTuple):
    """Cached capabilities for a single model."""
    model_id: str
    provider: str = ""
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    model_type: str = "chat"  # chat, reasoning, embedding, code
    probed_at: float = 0.0
    probe_count: int = 0
    # Adaptive timeout: observed latencies (last N requests)
    observed_latencies: tuple[float, ...] = ()


class CapabilityRegistry:
    """Model capability cache with TTL and optional persistence."""

    def __init__(self, ttl_seconds: int = 86400, control_store: Any = None):
        self._cache: dict[str, ModelCapability] = {}
        self._ttl = ttl_seconds
        self._store = control_store

    def supports_tools(self, model_id: str) -> bool | None:
        cap = self._cache.get(model_id)
        if cap is None:
            return None
        if self._is_stale(cap):
            return None
        return cap.supports_tools

    def record(self, model_id: str, **kwargs: Any) -> None:
        existing = self._cache.get(model_id)
        if existing:
            updates = {k: v for k, v in kwargs.items() if v is not None}
            updated = existing._replace(
                probed_at=time.time(),
                probe_count=existing.probe_count + 1,
                **updates)
        else:
            updated = ModelCapability(
                model_id=model_id,
                probed_at=time.time(),
                probe_count=1,
                **{k: v for k, v in kwargs.items() if v is not None})
        self._cache[model_id] = updated
        logger.info("Model capability recorded: %s = %s", model_id, dict(updated._asdict()))

    def record_latency(self, model_id: str, latency_seconds: float) -> None:
        """Record an observed request latency for adaptive timeout calculation."""
        cap = self._cache.get(model_id)
        if not cap:
            return
        # Keep last 20 observations
        latencies = cap.observed_latencies[-19:] + (latency_seconds,)
        self._cache[model_id] = cap._replace(observed_latencies=latencies)

    def get_timeout(self, model_id: str, default: float = 120.0) -> float:
        """Adaptive timeout: P95 of observed latencies * 2.5, with floor and ceiling.

        - First request (no data): use generous default (model may need to load)
        - After 3+ observations: adapt to actual model speed
        - Fast model (3B, 2s avg) → ~10s timeout
        - Slow model (14B CPU, 40s avg) → ~120s timeout
        - Reasoning model: 2x multiplier on top
        """
        cap = self._cache.get(model_id)
        if not cap or len(cap.observed_latencies) < 3:
            # Not enough data — use generous default for cold start
            if cap and cap.model_type == "reasoning":
                return default * 2.0
            return default

        latencies = sorted(cap.observed_latencies)
        p95_idx = max(0, int(len(latencies) * 0.95) - 1)
        p95 = latencies[p95_idx]

        # Timeout = P95 * 2.5 (headroom for variance)
        adaptive = p95 * 2.5

        # Model type multiplier
        if cap.model_type == "reasoning":
            adaptive *= 1.5

        # Floor: never below 10s, ceiling: never above 300s
        return max(10.0, min(300.0, adaptive))

    def get_stale_models(self) -> list[str]:
        return [mid for mid, cap in self._cache.items() if self._is_stale(cap)]

    def mark_for_reprobe(self, model_id: str) -> None:
        cap = self._cache.get(model_id)
        if cap:
            self._cache[model_id] = cap._replace(probed_at=0)

    def all_capabilities(self) -> dict[str, dict[str, Any]]:
        return {mid: dict(cap._asdict()) for mid, cap in self._cache.items()}

    def _is_stale(self, cap: ModelCapability) -> bool:
        return (time.time() - cap.probed_at) > self._ttl
