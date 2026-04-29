"""Unit tests for SSRF URL validator (#23 fail-closed on gaierror)."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from gateway.security.url_validator import validate_outbound_url


def test_blocks_unknown_scheme():
    with pytest.raises(ValueError, match="scheme"):
        validate_outbound_url("file:///etc/passwd")


def test_blocks_no_hostname():
    with pytest.raises(ValueError, match="hostname"):
        validate_outbound_url("http:///path")


def test_unresolvable_host_fails_closed():
    """gaierror must raise ValueError, NOT pass through silently.

    Pre-fix the validator swallowed gaierror and returned the URL — fail-open
    on DNS errors is a known SSRF / DNS-rebinding hole. Now it must fail
    closed so the caller sees an explicit ValueError.
    """
    with patch("gateway.security.url_validator.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
        with pytest.raises(ValueError, match="could not be resolved"):
            validate_outbound_url("https://this-host-does-not-exist.invalid/path")


def test_blocks_loopback_ipv4():
    with patch(
        "gateway.security.url_validator.socket.getaddrinfo",
        return_value=[(0, 0, 0, "", ("127.0.0.1", 0))],
    ):
        with pytest.raises(ValueError, match="private/internal"):
            validate_outbound_url("https://localhost-alias.example.com/foo")


def test_blocks_aws_metadata_ip():
    with patch(
        "gateway.security.url_validator.socket.getaddrinfo",
        return_value=[(0, 0, 0, "", ("169.254.169.254", 0))],
    ):
        with pytest.raises(ValueError, match="private/internal"):
            validate_outbound_url("https://metadata.example.com/")


def test_allows_public_ip():
    with patch(
        "gateway.security.url_validator.socket.getaddrinfo",
        return_value=[(0, 0, 0, "", ("8.8.8.8", 0))],
    ):
        result = validate_outbound_url("https://dns.google/")
        assert result == "https://dns.google/"


def test_allow_private_bypass():
    """allow_private=True skips DNS resolution entirely (used for trusted internal calls)."""
    # Even an unresolvable host is fine when allow_private=True because we
    # don't reach the DNS branch.
    result = validate_outbound_url("https://internal.svc.local/", allow_private=True)
    assert result == "https://internal.svc.local/"
