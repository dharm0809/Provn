"""Power-of-Two-Choices (P2C) load balancer for model group endpoints."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch


@dataclass
class Endpoint:
    url: str
    api_key: str
    weight: float = 1.0
    healthy: bool = True
    cooldown_until: float = 0.0
    outstanding: int = 0


@dataclass
class ModelGroup:
    pattern: str
    endpoints: list[Endpoint] = field(default_factory=list)


class LoadBalancer:
    """Power-of-Two-Choices selection across model group endpoints with health tracking.

    When 2+ healthy endpoints exist, samples two at random and picks the one
    with fewer outstanding (in-flight) requests.  Falls back to direct return
    when only one healthy endpoint remains.
    """

    def __init__(self, groups: list[ModelGroup]):
        self._groups = groups
        # Track when we last ran the cooldown sweep so we don't busy-loop on every
        # request. Cheap O(N) walk of all endpoints, but only needed when at least
        # one is currently in cooldown.
        self._last_health_check: float = 0.0

    def select_endpoint(self, model_id: str) -> Endpoint | None:
        """P2C selection from healthy endpoints matching *model_id*.

        Lazily re-enables endpoints whose cooldown window has elapsed before
        selection. This replaces the missing background health-check loop —
        any caller of ``select_endpoint`` will now revive cooled-down peers
        on the first selection past their ``cooldown_until``.
        """
        # Lazy revival: if any endpoint is currently unhealthy and the soonest
        # cooldown has already passed, run check_health() before selecting.
        # Cheap fast-path when nothing is in cooldown.
        now = time.monotonic()
        if now >= self._last_health_check + 1.0:
            # Throttle the sweep itself to once per second to avoid scanning
            # every group on every request under load. Granularity within 1s
            # is irrelevant for cooldown windows that default to 30s.
            for group in self._groups:
                for ep in group.endpoints:
                    if not ep.healthy:
                        self.check_health()
                        break
                else:
                    continue
                break
            self._last_health_check = now

        for group in self._groups:
            if not fnmatch(model_id.lower(), group.pattern.lower()):
                continue
            healthy = [ep for ep in group.endpoints if ep.healthy]
            if not healthy:
                return None
            if len(healthy) == 1:
                return healthy[0]
            a, b = random.sample(healthy, 2)
            return a if a.outstanding <= b.outstanding else b
        return None

    # ------------------------------------------------------------------
    # Outstanding request tracking
    # ------------------------------------------------------------------

    def increment_outstanding(self, endpoint: Endpoint) -> None:
        """Record that a new in-flight request has been dispatched to *endpoint*."""
        endpoint.outstanding += 1

    def decrement_outstanding(self, endpoint: Endpoint) -> None:
        """Record that an in-flight request to *endpoint* has completed."""
        endpoint.outstanding = max(0, endpoint.outstanding - 1)

    def mark_unhealthy(self, model_id: str, endpoint_url: str, cooldown_seconds: float = 30.0):
        """Mark endpoint as unhealthy with cooldown."""
        for group in self._groups:
            if not fnmatch(model_id.lower(), group.pattern.lower()):
                continue
            for ep in group.endpoints:
                if ep.url == endpoint_url:
                    ep.healthy = False
                    ep.cooldown_until = time.monotonic() + cooldown_seconds
                    return

    def check_health(self):
        """Re-enable endpoints past their cooldown."""
        now = time.monotonic()
        for group in self._groups:
            for ep in group.endpoints:
                if not ep.healthy and now >= ep.cooldown_until:
                    ep.healthy = True
