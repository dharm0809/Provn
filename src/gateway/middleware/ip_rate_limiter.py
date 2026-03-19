"""Pre-auth per-IP rate limiter to prevent brute-force and enumeration."""
from __future__ import annotations

import time
from collections import OrderedDict


class IPRateLimiter:
    """Sliding-window per-IP rate limiter.

    Tracks request timestamps per client IP in an OrderedDict (LRU eviction).
    Returns False from check() when the IP exceeds ``rpm`` requests in the
    last 60 seconds.  Oldest IPs are evicted when ``max_ips`` is reached.
    """

    def __init__(self, rpm: int = 300, max_ips: int = 50_000):
        self._rpm = rpm
        self._max_ips = max_ips
        self._windows: OrderedDict[str, list[float]] = OrderedDict()

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
