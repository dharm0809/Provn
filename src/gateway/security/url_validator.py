"""SSRF protection: validate outbound URLs against private IP blocklist."""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("10.0.0.0/8"),         # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),      # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),     # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),     # link-local / AWS metadata
    ipaddress.ip_network("0.0.0.0/8"),          # "this" network
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]

_ALLOWED_SCHEMES = {"http", "https"}


def validate_outbound_url(url: str, allow_private: bool = False) -> str:
    """Validate a URL is safe for outbound requests.

    Raises ValueError if the URL targets a private/internal network or uses a blocked scheme.
    Returns the URL unchanged if valid.
    """
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Blocked URL scheme '{parsed.scheme}' — only http/https allowed")

    if not parsed.hostname:
        raise ValueError("URL has no hostname")

    if allow_private:
        return url

    # Resolve DNS and check all addresses.
    #
    # NOTE: Pre-resolution validation is fundamentally vulnerable to DNS
    # rebinding — an attacker can serve a public IP at validation time and
    # a private IP at connect time. Defending against rebinding is a
    # validate-at-connect-time concern (pin to the resolved IP for the
    # actual outbound request) and is intentionally out of scope here.
    # What this function MUST do is fail closed: an unresolvable host or
    # other DNS error must NOT slip through as "let the actual request
    # decide". By the time a downstream request runs, other code paths
    # may already have trusted the URL.
    try:
        addrs = socket.getaddrinfo(parsed.hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        logger.warning("URL hostname unresolvable, blocking: %s (%s)", parsed.hostname, exc)
        raise ValueError(
            f"URL hostname '{parsed.hostname}' could not be resolved"
        ) from exc

    for _, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                logger.warning("SSRF blocked: %s resolves to private IP %s", parsed.hostname, ip)
                raise ValueError(f"Blocked: '{parsed.hostname}' resolves to private/internal IP {ip}")

    return url
