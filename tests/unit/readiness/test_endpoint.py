"""Endpoint auth and shape tests for GET /v1/readiness."""

from __future__ import annotations

import json
import os
import types

import pytest


def _make_test_client(api_keys="test-key-1"):
    os.environ["WALACOR_GATEWAY_API_KEYS"] = api_keys
    os.environ["WALACOR_LINEAGE_AUTH_REQUIRED"] = "true"
    os.environ["WALACOR_READINESS_ENABLED"] = "true"

    from gateway.config import get_settings
    get_settings.cache_clear()

    from starlette.testclient import TestClient
    from gateway.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def teardown_function():
    from gateway.config import get_settings
    get_settings.cache_clear()
    for k in ("WALACOR_GATEWAY_API_KEYS", "WALACOR_LINEAGE_AUTH_REQUIRED", "WALACOR_READINESS_ENABLED"):
        os.environ.pop(k, None)


def test_readiness_requires_auth():
    client = _make_test_client()
    resp = client.get("/v1/readiness")
    assert resp.status_code == 401


def test_readiness_with_key_returns_200():
    client = _make_test_client()
    resp = client.get("/v1/readiness", headers={"X-API-Key": "test-key-1"})
    assert resp.status_code == 200


def test_readiness_shape_stable():
    client = _make_test_client()
    resp = client.get("/v1/readiness", headers={"X-API-Key": "test-key-1"})
    assert resp.status_code == 200
    body = resp.json()

    assert "status" in body
    assert body["status"] in ("ready", "degraded", "unready")
    assert "generated_at" in body
    assert "cache_age_s" in body
    assert "gateway_id" in body
    assert "summary" in body
    assert set(body["summary"].keys()) >= {"green", "amber", "red", "total"}
    assert "checks" in body
    assert isinstance(body["checks"], list)

    for check in body["checks"]:
        assert "id" in check
        assert "name" in check
        assert "category" in check
        assert "severity" in check
        assert check["status"] in ("green", "amber", "red")
        assert "detail" in check
        assert "evidence" in check
        assert "elapsed_ms" in check


def test_readiness_disabled_returns_503():
    os.environ["WALACOR_READINESS_ENABLED"] = "false"
    os.environ["WALACOR_GATEWAY_API_KEYS"] = "key1"
    from gateway.config import get_settings
    get_settings.cache_clear()

    from starlette.testclient import TestClient
    from gateway.main import create_app
    client = TestClient(create_app(), raise_server_exceptions=False)
    resp = client.get("/v1/readiness", headers={"X-API-Key": "key1"})
    assert resp.status_code == 503


def test_lineage_returns_401_when_auth_required():
    client = _make_test_client()
    resp = client.get("/v1/lineage/sessions")
    assert resp.status_code == 401


def test_lineage_returns_non_401_with_key():
    """With a valid API key, auth passes — any non-401 response confirms auth was accepted."""
    client = _make_test_client()
    resp = client.get("/v1/lineage/sessions", headers={"X-API-Key": "test-key-1"})
    assert resp.status_code != 401, f"Expected auth to pass, got 401"


def test_lineage_open_when_auth_not_required():
    """When lineage_auth_required=False, /v1/lineage/* does not require an API key."""
    os.environ["WALACOR_GATEWAY_API_KEYS"] = "key1"
    os.environ["WALACOR_LINEAGE_AUTH_REQUIRED"] = "false"
    from gateway.config import get_settings
    get_settings.cache_clear()

    from starlette.testclient import TestClient
    from gateway.main import create_app
    client = TestClient(create_app(), raise_server_exceptions=False)
    resp = client.get("/v1/lineage/sessions")
    assert resp.status_code != 401, f"Expected lineage to be open (no auth), got 401"
