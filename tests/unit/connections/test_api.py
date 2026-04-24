"""Endpoint-level tests for GET /v1/connections."""

from __future__ import annotations

import os

import pytest


TILE_ORDER = (
    "providers",
    "walacor_delivery",
    "analyzers",
    "tool_loop",
    "model_capabilities",
    "control_plane",
    "auth",
    "readiness",
    "streaming",
    "intelligence_worker",
)


def _make_client(enabled: bool = True):
    os.environ["WALACOR_GATEWAY_API_KEYS"] = "test-key-1"
    os.environ["WALACOR_LINEAGE_AUTH_REQUIRED"] = "true"
    os.environ["WALACOR_CONNECTIONS_ENABLED"] = "true" if enabled else "false"

    from gateway.config import get_settings
    get_settings.cache_clear()

    # clear endpoint cache so tests don't bleed into each other
    from gateway.connections.api import _reset_cache_for_tests
    _reset_cache_for_tests()

    from starlette.testclient import TestClient
    from gateway.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def teardown_function():
    from gateway.config import get_settings
    get_settings.cache_clear()
    for k in (
        "WALACOR_GATEWAY_API_KEYS",
        "WALACOR_LINEAGE_AUTH_REQUIRED",
        "WALACOR_CONNECTIONS_ENABLED",
    ):
        os.environ.pop(k, None)


def test_connections_requires_auth():
    client = _make_client()
    resp = client.get("/v1/connections")
    assert resp.status_code == 401


def test_connections_envelope_has_ten_tiles_in_order():
    client = _make_client()
    resp = client.get("/v1/connections", headers={"X-API-Key": "test-key-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {"generated_at", "ttl_seconds", "overall_status", "tiles", "events"}
    assert body["ttl_seconds"] == 3
    assert body["overall_status"] in ("green", "amber", "red")
    tiles = body["tiles"]
    assert len(tiles) == 10
    assert [t["id"] for t in tiles] == list(TILE_ORDER)
    for tile in tiles:
        assert set(tile.keys()) >= {"id", "status", "headline", "subline", "last_change_ts", "detail"}
        assert tile["status"] in ("green", "amber", "red", "unknown")
        assert len(tile["headline"]) <= 60
        assert len(tile["subline"]) <= 80


def test_connections_disabled_returns_503():
    client = _make_client(enabled=False)
    resp = client.get("/v1/connections", headers={"X-API-Key": "test-key-1"})
    assert resp.status_code == 503


def test_connections_cached_within_ttl():
    """Two rapid calls return the same generated_at — cache TTL singleflight."""
    client = _make_client()
    r1 = client.get("/v1/connections", headers={"X-API-Key": "test-key-1"})
    r2 = client.get("/v1/connections", headers={"X-API-Key": "test-key-1"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["generated_at"] == r2.json()["generated_at"]
