# tests/unit/test_identity_validator.py
"""Tests for identity cross-validation."""
import pytest
from unittest.mock import MagicMock
from gateway.adaptive.identity_validator import DefaultIdentityValidator
from gateway.auth.identity import CallerIdentity


@pytest.fixture
def validator():
    return DefaultIdentityValidator()


def _make_request(headers=None):
    req = MagicMock()
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def test_no_jwt_returns_header_identity(validator):
    header_id = CallerIdentity(user_id="alice", source="header_unverified")
    result = validator.validate(None, header_id, _make_request())
    assert result.valid is True
    assert result.identity.user_id == "alice"
    assert result.source == "header_unverified"


def test_no_jwt_no_header_returns_none(validator):
    result = validator.validate(None, None, _make_request())
    assert result.valid is True
    assert result.identity is None


def test_jwt_wins_over_header(validator):
    jwt_id = CallerIdentity(user_id="bob", email="bob@co.com", source="jwt")
    header_id = CallerIdentity(user_id="alice", source="header_unverified")
    result = validator.validate(jwt_id, header_id, _make_request({"x-user-id": "alice"}))
    assert result.identity.user_id == "bob"
    assert result.source == "jwt_verified"


def test_jwt_header_match_no_warnings(validator):
    jwt_id = CallerIdentity(user_id="bob", source="jwt")
    result = validator.validate(jwt_id, None, _make_request({"x-user-id": "bob"}))
    assert result.valid is True
    assert len(result.warnings) == 0


def test_jwt_header_mismatch_warning(validator):
    jwt_id = CallerIdentity(user_id="bob", source="jwt")
    result = validator.validate(jwt_id, None, _make_request({"x-user-id": "alice"}))
    assert result.valid is False
    assert len(result.warnings) == 1
    assert "alice" in result.warnings[0]
    assert "bob" in result.warnings[0]


def test_jwt_no_header_user_id_no_warning(validator):
    jwt_id = CallerIdentity(user_id="bob", source="jwt")
    result = validator.validate(jwt_id, None, _make_request({}))
    assert result.valid is True
    assert len(result.warnings) == 0


def test_merge_fills_gaps_from_header(validator):
    jwt_id = CallerIdentity(user_id="bob", email="", roles=[], source="jwt")
    header_id = CallerIdentity(user_id="bob", email="bob@co.com",
                                roles=["admin"], team="eng", source="header_unverified")
    result = validator.validate(jwt_id, header_id, _make_request())
    assert result.identity.email == "bob@co.com"
    assert result.identity.roles == ["admin"]
    assert result.identity.team == "eng"
