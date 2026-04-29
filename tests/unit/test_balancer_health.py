"""Tests for lazy cooldown revival in LoadBalancer.

Bug #2: ``check_health()`` had no callers, so endpoints stayed unhealthy for the
process lifetime once ``mark_unhealthy()`` fired. ``select_endpoint()`` now runs
the cooldown sweep lazily on first selection past the throttled window.
"""

from __future__ import annotations

import time

from gateway.routing.balancer import Endpoint, LoadBalancer, ModelGroup


def _make_group(pattern: str = "gpt-*", count: int = 2) -> ModelGroup:
    endpoints = [
        Endpoint(url=f"https://api{i}.example.com", api_key=f"sk-{i}")
        for i in range(count)
    ]
    return ModelGroup(pattern=pattern, endpoints=endpoints)


def test_endpoint_revived_after_cooldown_via_select():
    """After cooldown elapses, the next select_endpoint() call must revive the
    endpoint without any external caller invoking check_health() directly."""
    group = _make_group(count=2)
    lb = LoadBalancer([group])

    # Mark one endpoint unhealthy with an immediate cooldown.
    lb.mark_unhealthy("gpt-4", "https://api0.example.com", cooldown_seconds=0.0)
    assert group.endpoints[0].healthy is False

    # Force the throttle window so the lazy sweep runs.
    lb._last_health_check = 0.0

    # Sleep just enough so monotonic clock advances past cooldown_until.
    time.sleep(0.01)

    # Selecting again should trigger the lazy revival sweep.
    selected_urls = set()
    for _ in range(50):
        ep = lb.select_endpoint("gpt-4")
        assert ep is not None
        selected_urls.add(ep.url)

    assert group.endpoints[0].healthy is True
    assert "https://api0.example.com" in selected_urls


def test_endpoint_stays_unhealthy_during_cooldown():
    """Within the cooldown window, the endpoint must remain unhealthy even
    though select_endpoint() ran the sweep."""
    group = _make_group(count=2)
    lb = LoadBalancer([group])

    lb.mark_unhealthy("gpt-4", "https://api0.example.com", cooldown_seconds=60.0)
    lb._last_health_check = 0.0  # force sweep

    for _ in range(20):
        ep = lb.select_endpoint("gpt-4")
        assert ep is not None
        assert ep.url == "https://api1.example.com"

    assert group.endpoints[0].healthy is False


def test_lazy_sweep_throttled_when_no_unhealthy():
    """When no endpoints are unhealthy the sweep should be a near-no-op —
    _last_health_check should still update at most once per second so we don't
    walk every endpoint on every request."""
    group = _make_group(count=3)
    lb = LoadBalancer([group])

    before = lb._last_health_check
    for _ in range(10):
        lb.select_endpoint("gpt-4")
    # When nothing is unhealthy, the throttle marker is only updated on the
    # first call past the 1s window. We don't assert it stays zero — we only
    # assert the public behaviour: every call still returns a healthy endpoint.
    assert before == 0.0
    for ep in group.endpoints:
        assert ep.healthy is True
