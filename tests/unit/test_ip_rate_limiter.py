"""Tests for the pre-auth IP rate limiter, including X-Forwarded-For handling.

The trusted_proxies feature gates whether the limiter honours XFF: a request
from an untrusted peer cannot use XFF to dodge a per-IP limit, while a request
arriving via a configured proxy is correctly attributed to the original client.
"""
from __future__ import annotations

from gateway.middleware.ip_rate_limiter import (
    IPRateLimiter,
    _parse_trusted_proxies,
    resolve_client_ip,
)


def test_parse_trusted_proxies_handles_ips_and_cidrs():
    nets = _parse_trusted_proxies("10.0.0.1, 192.168.0.0/16, 2001:db8::/32")
    assert len(nets) == 3


def test_parse_trusted_proxies_skips_invalid_entries():
    nets = _parse_trusted_proxies("10.0.0.1, garbage, 192.168.0.0/16")
    assert len(nets) == 2  # garbage dropped


def test_parse_trusted_proxies_empty_string_is_empty_list():
    assert _parse_trusted_proxies("") == []


def test_resolve_no_trusted_proxies_returns_direct_peer():
    """Default (no trusted_proxies) — XFF is ignored entirely (anti-spoof)."""
    ip = resolve_client_ip(
        direct_peer="203.0.113.5",
        xff_header="evil-spoof, 1.1.1.1",
        trusted_proxies=[],
    )
    assert ip == "203.0.113.5"


def test_resolve_trusted_peer_uses_xff_rightmost_untrusted():
    """When direct peer is trusted, walk XFF right-to-left for the first untrusted hop."""
    nets = _parse_trusted_proxies("10.0.0.0/8")
    ip = resolve_client_ip(
        direct_peer="10.0.0.1",
        xff_header="203.0.113.5, 10.0.0.7, 10.0.0.1",
        trusted_proxies=nets,
    )
    # Rightmost untrusted is 203.0.113.5 (10.0.0.7 and 10.0.0.1 are both 10/8 trusted).
    assert ip == "203.0.113.5"


def test_resolve_untrusted_peer_ignores_xff_even_if_set():
    """Anti-spoof: even with trusted_proxies configured, an untrusted peer's XFF is ignored."""
    nets = _parse_trusted_proxies("10.0.0.0/8")
    ip = resolve_client_ip(
        direct_peer="198.51.100.99",  # NOT in trusted set
        xff_header="1.2.3.4, 5.6.7.8",
        trusted_proxies=nets,
    )
    assert ip == "198.51.100.99"


def test_resolve_trusted_peer_no_xff_returns_direct_peer():
    nets = _parse_trusted_proxies("10.0.0.0/8")
    ip = resolve_client_ip(
        direct_peer="10.0.0.1",
        xff_header=None,
        trusted_proxies=nets,
    )
    assert ip == "10.0.0.1"


def test_resolve_all_xff_entries_trusted_returns_leftmost():
    """If every XFF entry is trusted, fall back to leftmost (closest to origin)."""
    nets = _parse_trusted_proxies("10.0.0.0/8")
    ip = resolve_client_ip(
        direct_peer="10.0.0.1",
        xff_header="10.1.1.1, 10.2.2.2, 10.0.0.1",
        trusted_proxies=nets,
    )
    assert ip == "10.1.1.1"


def test_iprate_limiter_resolve_ip_helper():
    limiter = IPRateLimiter(rpm=10, trusted_proxies="10.0.0.0/8")
    assert limiter.resolve_ip("10.0.0.5", "203.0.113.5") == "203.0.113.5"
    assert limiter.resolve_ip("8.8.8.8", "203.0.113.5") == "8.8.8.8"


def test_iprate_limiter_check_blocks_at_rpm():
    limiter = IPRateLimiter(rpm=3)
    assert limiter.check("1.1.1.1") is True
    assert limiter.check("1.1.1.1") is True
    assert limiter.check("1.1.1.1") is True
    assert limiter.check("1.1.1.1") is False  # 4th in window blocked


def test_iprate_limiter_separate_buckets_per_ip():
    limiter = IPRateLimiter(rpm=2)
    assert limiter.check("1.1.1.1") is True
    assert limiter.check("1.1.1.1") is True
    assert limiter.check("1.1.1.1") is False
    # Different IP gets its own bucket
    assert limiter.check("2.2.2.2") is True


def test_attacker_cannot_spoof_via_xff_when_no_trusted_proxies():
    """SECURITY: an attacker hitting the gateway directly cannot rotate XFF to bypass per-IP limits."""
    limiter = IPRateLimiter(rpm=2, trusted_proxies="")
    direct_peer = "203.0.113.99"  # the attacker's real IP

    # Each "spoofed" XFF gets resolved back to the attacker's true peer IP.
    for spoof in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        ip = limiter.resolve_ip(direct_peer, spoof)
        assert ip == direct_peer

    # And the bucket is bound to direct_peer, so they get blocked at rpm.
    assert limiter.check(direct_peer) is True
    assert limiter.check(direct_peer) is True
    assert limiter.check(direct_peer) is False
