"""Unit tests for model group weighted load balancer."""

import time

import pytest

from gateway.routing.balancer import Endpoint, LoadBalancer, ModelGroup


def _make_group(pattern="gpt-*", weights=None):
    weights = weights or [7, 3]
    endpoints = [
        Endpoint(url=f"https://api{i}.example.com", api_key=f"sk-{i}", weight=w)
        for i, w in enumerate(weights)
    ]
    return ModelGroup(pattern=pattern, endpoints=endpoints)


def test_weighted_selection_distributes_proportionally():
    """1000 selections with weights 7/3 should land ~70/30 (±10%)."""
    group = _make_group(weights=[7, 3])
    lb = LoadBalancer([group])

    counts = {ep.url: 0 for ep in group.endpoints}
    for _ in range(1000):
        ep = lb.select_endpoint("gpt-4")
        assert ep is not None
        counts[ep.url] += 1

    ratio = counts["https://api0.example.com"] / 1000
    assert 0.55 < ratio < 0.85, f"Expected ~70%, got {ratio:.1%}"


def test_unhealthy_endpoint_skipped():
    """Marking an endpoint unhealthy routes all traffic to the healthy one."""
    group = _make_group(weights=[5, 5])
    lb = LoadBalancer([group])

    lb.mark_unhealthy("gpt-4", "https://api0.example.com", cooldown_seconds=60)

    for _ in range(20):
        ep = lb.select_endpoint("gpt-4")
        assert ep is not None
        assert ep.url == "https://api1.example.com"


def test_cooldown_expires():
    """After cooldown, endpoint becomes available again."""
    group = _make_group(weights=[5, 5])
    lb = LoadBalancer([group])

    lb.mark_unhealthy("gpt-4", "https://api0.example.com", cooldown_seconds=0.0)
    # cooldown_seconds=0 means it should immediately be available
    lb.check_health()

    urls = set()
    for _ in range(50):
        ep = lb.select_endpoint("gpt-4")
        assert ep is not None
        urls.add(ep.url)

    assert "https://api0.example.com" in urls


def test_all_unhealthy_returns_none():
    """When all endpoints are in cooldown, returns None."""
    group = _make_group(weights=[5, 5])
    lb = LoadBalancer([group])

    lb.mark_unhealthy("gpt-4", "https://api0.example.com", cooldown_seconds=60)
    lb.mark_unhealthy("gpt-4", "https://api1.example.com", cooldown_seconds=60)

    assert lb.select_endpoint("gpt-4") is None


def test_no_matching_group_returns_none():
    """Unmatched model_id returns None."""
    group = _make_group(pattern="gpt-*", weights=[5])
    lb = LoadBalancer([group])

    assert lb.select_endpoint("claude-3") is None
