"""Tests for tenant_id plumbing on CallerIdentity.

Covers JWT-claim → CallerIdentity.tenant_id, fallbacks when the configured
claim is missing, header-driven extraction, and the API-key gap (no tenant
binding because the API-key store has no tenant column).
"""

from __future__ import annotations

import pytest

from gateway.auth.identity import CallerIdentity, resolve_identity_from_headers
from gateway.auth.jwt_auth import _jwks_cache, validate_jwt


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    _jwks_cache.clear()
    yield
    _jwks_cache.clear()


def _make_hs256_token(payload: dict, secret: str = "test-secret") -> str:
    jwt = pytest.importorskip("jwt")
    return jwt.encode(payload, secret, algorithm="HS256")


class TestCallerIdentityField:
    def test_default_tenant_is_none(self):
        ci = CallerIdentity(user_id="alice")
        assert ci.tenant_id is None

    def test_tenant_id_immutable(self):
        ci = CallerIdentity(user_id="alice", tenant_id="t-1")
        with pytest.raises(AttributeError):
            ci.tenant_id = "t-2"  # type: ignore[misc]


class TestJWTTenantClaim:
    def test_default_claim_tenant_id(self):
        token = _make_hs256_token({"sub": "alice", "tenant_id": "acme"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.tenant_id == "acme"

    def test_custom_claim_name(self):
        token = _make_hs256_token({"sub": "alice", "org_uuid": "org-123"})
        identity = validate_jwt(
            token, secret="test-secret", algorithms=["HS256"],
            tenant_claim="org_uuid",
        )
        assert identity is not None
        assert identity.tenant_id == "org-123"

    def test_missing_claim_returns_none(self):
        token = _make_hs256_token({"sub": "alice"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.tenant_id is None

    def test_fallback_to_tenant_when_configured_missing(self):
        # Configured claim 'tenant_id' missing, but 'tenant' present
        token = _make_hs256_token({"sub": "alice", "tenant": "fallback-tenant"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.tenant_id == "fallback-tenant"

    def test_fallback_to_org(self):
        token = _make_hs256_token({"sub": "alice", "org": "org-from-fallback"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.tenant_id == "org-from-fallback"

    def test_fallback_to_org_id(self):
        token = _make_hs256_token({"sub": "alice", "org_id": "oid-7"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.tenant_id == "oid-7"

    def test_configured_claim_takes_priority_over_fallbacks(self):
        token = _make_hs256_token({
            "sub": "alice",
            "tenant_id": "primary",
            "org": "fallback-should-not-win",
        })
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.tenant_id == "primary"

    def test_empty_string_claim_treated_as_missing(self):
        token = _make_hs256_token({"sub": "alice", "tenant_id": ""})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.tenant_id is None

    def test_non_string_tenant_coerced(self):
        # Numeric tenant ids must still be strings on CallerIdentity
        token = _make_hs256_token({"sub": "alice", "tenant_id": 42})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.tenant_id == "42"


class _StubRequest:
    def __init__(self, headers: dict, client_host: str | None = "1.2.3.4"):
        self.headers = headers

        class _Client:
            host = client_host

        self.client = _Client() if client_host else None


class TestHeaderTenantExtraction:
    def test_x_tenant_id_header(self):
        req = _StubRequest({"x-user-id": "alice", "x-tenant-id": "tenant-from-header"})
        ident = resolve_identity_from_headers(req)
        assert ident is not None
        assert ident.tenant_id == "tenant-from-header"

    def test_no_tenant_header_returns_none(self):
        req = _StubRequest({"x-user-id": "alice"})
        ident = resolve_identity_from_headers(req)
        assert ident is not None
        assert ident.tenant_id is None

    def test_body_metadata_tenant_id(self):
        req = _StubRequest({"x-user-id": "alice"})
        ident = resolve_identity_from_headers(req, body_metadata={"tenant_id": "from-body"})
        assert ident is not None
        assert ident.tenant_id == "from-body"

    def test_header_takes_priority_over_body(self):
        req = _StubRequest({"x-user-id": "alice", "x-tenant-id": "from-header"})
        ident = resolve_identity_from_headers(req, body_metadata={"tenant_id": "from-body"})
        assert ident is not None
        assert ident.tenant_id == "from-header"


class TestAPIKeyTenantGap:
    """API-key auth has no tenant binding today.

    The `key_policy_assignments` and `key_tool_permissions` tables key on
    `api_key_hash` only — there is no tenant column. CallerIdentity built
    from header-based auth therefore has tenant_id=None unless the caller
    supplies an X-Tenant-Id header. Documented as a deferred follow-up.
    """

    def test_resolve_identity_without_tenant_header_yields_none(self):
        req = _StubRequest({"x-user-id": "alice"})
        ident = resolve_identity_from_headers(req)
        assert ident is not None
        assert ident.tenant_id is None
