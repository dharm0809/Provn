"""Tests for multi-header session ID resolution."""
from unittest.mock import MagicMock


def _make_request(headers: dict) -> MagicMock:
    """Create a mock request with given headers (lowercased keys)."""
    req = MagicMock()
    req.headers = {k.lower(): v for k, v in headers.items()}
    return req


def test_resolves_primary_header():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({"X-Session-ID": "session-abc"})
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result == "session-abc"


def test_resolves_fallback_header():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({"X-OpenWebUI-Chat-Id": "chat-xyz"})
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result == "chat-xyz"


def test_primary_takes_precedence_over_fallback():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({
        "X-Session-ID": "primary",
        "X-OpenWebUI-Chat-Id": "fallback",
    })
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result == "primary"


def test_generates_uuid_when_no_header_matches():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({})
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result  # non-empty
    assert len(result) == 36  # UUID format


def test_empty_header_value_treated_as_missing():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({"X-Session-ID": "", "X-OpenWebUI-Chat-Id": "chat-123"})
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result == "chat-123"


def test_single_header_name_list():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({"X-Session-ID": "only-one"})
    result = resolve_session_id(req, ["X-Session-ID"])
    assert result == "only-one"
