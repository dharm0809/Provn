"""Tests for JWT authentication module."""

from __future__ import annotations

import time
import types

import pytest

from gateway.auth.jwt_auth import (
    JWTConfigurationError,
    _jwks_cache,
    assert_jwt_runtime_config,
    validate_jwt,
)


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    """Clear JWKS cache between tests."""
    _jwks_cache.clear()
    yield
    _jwks_cache.clear()


def _make_hs256_token(payload: dict, secret: str = "test-secret") -> str:
    """Helper to create an HS256 JWT."""
    jwt = pytest.importorskip("jwt")
    return jwt.encode(payload, secret, algorithm="HS256")


class TestValidateJWT:
    def test_valid_hs256(self):
        token = _make_hs256_token({"sub": "alice", "email": "alice@co.com", "roles": ["admin"], "team": "eng"})
        identity = validate_jwt(
            token, secret="test-secret", algorithms=["HS256"],
            user_claim="sub", email_claim="email", roles_claim="roles", team_claim="team",
        )
        assert identity is not None
        assert identity.user_id == "alice"
        assert identity.email == "alice@co.com"
        assert identity.roles == ["admin"]
        assert identity.team == "eng"
        assert identity.source == "jwt"

    def test_expired_token(self):
        token = _make_hs256_token({"sub": "alice", "exp": int(time.time()) - 3600})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is None

    def test_wrong_secret(self):
        token = _make_hs256_token({"sub": "alice"}, secret="correct-secret")
        identity = validate_jwt(token, secret="wrong-secret", algorithms=["HS256"])
        assert identity is None

    def test_wrong_issuer(self):
        token = _make_hs256_token({"sub": "alice", "iss": "real-issuer"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"], issuer="expected-issuer")
        assert identity is None

    def test_wrong_audience(self):
        token = _make_hs256_token({"sub": "alice", "aud": "other-app"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"], audience="my-app")
        assert identity is None

    def test_missing_user_claim(self):
        token = _make_hs256_token({"email": "alice@co.com"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is None

    def test_custom_claims(self):
        token = _make_hs256_token({
            "user_id": "bob",
            "mail": "bob@co.com",
            "groups": ["viewer", "editor"],
            "department": "sales",
        })
        identity = validate_jwt(
            token, secret="test-secret", algorithms=["HS256"],
            user_claim="user_id", email_claim="mail",
            roles_claim="groups", team_claim="department",
        )
        assert identity is not None
        assert identity.user_id == "bob"
        assert identity.email == "bob@co.com"
        assert identity.roles == ["viewer", "editor"]
        assert identity.team == "sales"

    def test_empty_token(self):
        identity = validate_jwt("", secret="test-secret", algorithms=["HS256"])
        assert identity is None

    def test_no_config(self):
        """No secret or JWKS URL configured."""
        token = _make_hs256_token({"sub": "alice"})
        identity = validate_jwt(token, algorithms=["HS256"])
        assert identity is None

    def test_roles_as_csv_string(self):
        token = _make_hs256_token({"sub": "alice", "roles": "admin,editor"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        assert identity.roles == ["admin", "editor"]

    def test_frozen_identity(self):
        token = _make_hs256_token({"sub": "alice"})
        identity = validate_jwt(token, secret="test-secret", algorithms=["HS256"])
        assert identity is not None
        with pytest.raises(AttributeError):
            identity.user_id = "bob"  # type: ignore[misc]


def _settings(**kw):
    """Build a stand-in for the pydantic Settings object used by assert_jwt_runtime_config."""
    defaults = dict(
        auth_mode="api_key",
        jwt_secret="",
        jwt_jwks_url="",
        jwt_issuer="",
        jwt_audience="",
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


class TestAssertJWTRuntimeConfig:
    def test_api_key_mode_skips_check(self):
        # In api_key mode, JWT is never consulted — empty config is fine.
        assert_jwt_runtime_config(_settings(auth_mode="api_key"))

    def test_jwt_mode_missing_iss_raises(self):
        with pytest.raises(JWTConfigurationError) as exc_info:
            assert_jwt_runtime_config(_settings(
                auth_mode="jwt",
                jwt_secret="x" * 32,
                jwt_audience="my-app",
                # jwt_issuer missing
            ))
        assert "ISSUER" in str(exc_info.value).upper()

    def test_jwt_mode_missing_aud_raises(self):
        with pytest.raises(JWTConfigurationError) as exc_info:
            assert_jwt_runtime_config(_settings(
                auth_mode="jwt",
                jwt_secret="x" * 32,
                jwt_issuer="https://idp.example.com",
                # jwt_audience missing
            ))
        assert "AUDIENCE" in str(exc_info.value).upper()

    def test_jwt_mode_missing_secret_and_jwks_raises(self):
        with pytest.raises(JWTConfigurationError):
            assert_jwt_runtime_config(_settings(
                auth_mode="jwt",
                jwt_issuer="https://idp.example.com",
                jwt_audience="my-app",
            ))

    def test_jwt_mode_fully_configured_passes(self):
        assert_jwt_runtime_config(_settings(
            auth_mode="jwt",
            jwt_secret="x" * 32,
            jwt_issuer="https://idp.example.com",
            jwt_audience="my-app",
        ))

    def test_both_mode_requires_full_config(self):
        with pytest.raises(JWTConfigurationError):
            assert_jwt_runtime_config(_settings(
                auth_mode="both",
                jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
                # iss/aud missing
            ))

    def test_both_mode_fully_configured_passes(self):
        assert_jwt_runtime_config(_settings(
            auth_mode="both",
            jwt_jwks_url="https://idp.example.com/.well-known/jwks.json",
            jwt_issuer="https://idp.example.com",
            jwt_audience="my-app",
        ))


class TestMissingIssuerWarning:
    def test_missing_issuer_logs_warning(self, caplog):
        """When iss is unset, validate_jwt logs a WARNING (not debug)."""
        token = _make_hs256_token({"sub": "alice"})
        with caplog.at_level("WARNING", logger="gateway.auth.jwt_auth"):
            validate_jwt(token, secret="test-secret", algorithms=["HS256"], audience="my-app")
        # At least one WARNING about no issuer
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("issuer" in r.message.lower() for r in warnings), (
            f"Expected a WARNING about missing issuer, got {[r.message for r in warnings]}"
        )

    def test_missing_audience_logs_warning(self, caplog):
        """When aud is unset, validate_jwt logs a WARNING (not debug)."""
        token = _make_hs256_token({"sub": "alice"})
        with caplog.at_level("WARNING", logger="gateway.auth.jwt_auth"):
            validate_jwt(token, secret="test-secret", algorithms=["HS256"], issuer="https://idp.example.com")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("audience" in r.message.lower() for r in warnings), (
            f"Expected a WARNING about missing audience, got {[r.message for r in warnings]}"
        )
