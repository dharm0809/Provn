"""Tests for caller identity resolution from headers."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from gateway.auth.identity import CallerIdentity, resolve_identity_from_headers


def _make_request(headers: dict[str, str] | None = None) -> Request:
    """Create a minimal Starlette Request with the given headers."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


class TestCallerIdentity:
    def test_frozen(self):
        identity = CallerIdentity(user_id="alice", email="alice@co.com")
        with pytest.raises(AttributeError):
            identity.user_id = "bob"  # type: ignore[misc]

    def test_defaults(self):
        identity = CallerIdentity(user_id="alice")
        assert identity.email == ""
        assert identity.roles == []
        assert identity.team is None
        assert identity.source == "header_unverified"


class TestResolveIdentityFromHeaders:
    def test_full_headers(self):
        request = _make_request({
            "x-user-id": "alice",
            "x-team-id": "engineering",
            "x-user-roles": "admin, viewer",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id == "alice"
        assert identity.team == "engineering"
        assert identity.roles == ["admin", "viewer"]
        assert identity.source == "header_unverified"

    def test_user_only(self):
        request = _make_request({"x-user-id": "bob"})
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id == "bob"
        assert identity.team is None
        assert identity.roles == []

    def test_missing_user_id(self):
        """No X-User-Id: falls back to anonymous identity so every
        request still has an audit trail (Phase 21: NEVER returns None).
        Team header still surfaces in the returned identity."""
        request = _make_request({"x-team-id": "eng"})
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id.startswith("anonymous")
        assert identity.source == "anonymous"
        assert identity.team == "eng"

    def test_empty_headers(self):
        """No headers at all: anonymous fallback, team=None."""
        request = _make_request({})
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id.startswith("anonymous")
        assert identity.source == "anonymous"
        assert identity.team is None

    def test_whitespace_user_id(self):
        """Whitespace-only X-User-Id is treated as missing → anonymous
        fallback, never surfaced as a real identity."""
        request = _make_request({"x-user-id": "  "})
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id.startswith("anonymous")
        assert identity.source == "anonymous"

    def test_openwebui_user_name_fallback(self):
        request = _make_request({"x-openwebui-user-name": "alice"})
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id == "alice"

    def test_openwebui_user_id_fallback(self):
        request = _make_request({"x-openwebui-user-id": "uuid-123"})
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id == "uuid-123"

    def test_generic_header_takes_precedence_over_openwebui(self):
        request = _make_request({
            "x-user-id": "generic-alice",
            "x-openwebui-user-name": "owui-alice",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id == "generic-alice"

    def test_openwebui_email_fallback(self):
        request = _make_request({
            "x-openwebui-user-name": "alice",
            "x-openwebui-user-email": "alice@example.com",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.email == "alice@example.com"

    def test_generic_email_takes_precedence(self):
        request = _make_request({
            "x-user-id": "alice",
            "x-user-email": "generic@co.com",
            "x-openwebui-user-email": "owui@co.com",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.email == "generic@co.com"

    def test_openwebui_role_as_roles_list(self):
        request = _make_request({
            "x-openwebui-user-name": "bob",
            "x-openwebui-user-role": "admin",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.roles == ["admin"]

    def test_generic_roles_takes_precedence(self):
        request = _make_request({
            "x-user-id": "alice",
            "x-user-roles": "editor, viewer",
            "x-openwebui-user-role": "admin",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.roles == ["editor", "viewer"]
