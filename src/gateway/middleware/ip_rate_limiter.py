"""Pre-auth per-IP rate limiter to prevent brute-force and enumeration."""
from __future__ import annotations

import ipaddress
import logging
import time
from collections import OrderedDict
from typing import Iterable

logger = logging.getLogger(__name__)


def _parse_trusted_proxies(spec: str | Iterable[str]) -> list[ipaddress._BaseNetwork]:
    """Parse a comma-separated string (or iterable) of IPs/CIDRs into networks.

    Bare IPs are treated as /32 (IPv4) or /128 (IPv6). Invalid entries are
    logged and skipped — fail-open to avoid blocking startup on a typo.
    """
    if isinstance(spec, str):
        items = [s.strip() for s in spec.split(",") if s.strip()]
    else:
        items = [str(s).strip() for s in spec if str(s).strip()]

    nets: list[ipaddress._BaseNetwork] = []
    for item in items:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("ip_rate_limiter: ignoring invalid trusted_proxy entry %r", item)
    return nets


def _client_ip_in_networks(ip: str, nets: list[ipaddress._BaseNetwork]) -> bool:
    if not nets:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def resolve_client_ip(
    direct_peer: str,
    xff_header: str | None,
    trusted_proxies: list[ipaddress._BaseNetwork],
) -> str:
    """Return the effective client IP for rate-limiting purposes.

    When ``direct_peer`` is in ``trusted_proxies`` and ``X-Forwarded-For`` is
    present, walk the XFF list right-to-left and return the first entry that
    is NOT a trusted proxy. If every XFF entry is trusted, fall back to the
    leftmost. When the direct peer is NOT trusted, ignore XFF entirely (clients
    can spoof the header otherwise).
    """
    if not trusted_proxies or not _client_ip_in_networks(direct_peer, trusted_proxies):
        return direct_peer
    if not xff_header:
        return direct_peer

    # Walk right-to-left for the first untrusted hop.
    candidates = [c.strip() for c in xff_header.split(",") if c.strip()]
    for candidate in reversed(candidates):
        if not _client_ip_in_networks(candidate, trusted_proxies):
            return candidate
    # All entries trusted — return the leftmost (closest-to-origin).
    return candidates[0] if candidates else direct_peer


class IPRateLimiter:
    """Sliding-window per-IP rate limiter.

    Tracks request timestamps per client IP in an OrderedDict (LRU eviction).
    Returns False from check() when the IP exceeds ``rpm`` requests in the
    last 60 seconds.  Oldest IPs are evicted when ``max_ips`` is reached.

    When ``trusted_proxies`` is non-empty, callers should resolve the client
    IP using :func:`resolve_client_ip` before calling :meth:`check`.
    """

    def __init__(
        self,
        rpm: int = 300,
        max_ips: int = 50_000,
        trusted_proxies: str | Iterable[str] = "",
    ):
        self._rpm = rpm
        self._max_ips = max_ips
        self._windows: OrderedDict[str, list[float]] = OrderedDict()
        self._trusted_proxies: list[ipaddress._BaseNetwork] = _parse_trusted_proxies(trusted_proxies)

    @property
    def trusted_proxies(self) -> list[ipaddress._BaseNetwork]:
        return list(self._trusted_proxies)

    def resolve_ip(self, direct_peer: str, xff_header: str | None) -> str:
        """Resolve the effective client IP using configured trusted_proxies."""
        return resolve_client_ip(direct_peer, xff_header, self._trusted_proxies)

    def check(self, ip: str) -> bool:
        now = time.monotonic()
        window = self._windows.get(ip, [])
        cutoff = now - 60
        window = [t for t in window if t > cutoff]
        if len(window) >= self._rpm:
            return False
        window.append(now)
        self._windows[ip] = window
        self._windows.move_to_end(ip)
        while len(self._windows) > self._max_ips:
            self._windows.popitem(last=False)
        return True
