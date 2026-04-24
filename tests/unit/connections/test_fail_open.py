"""Fail-open behavior: a raising builder becomes an unknown tile; endpoint still 200."""

from __future__ import annotations

import os

import pytest


def _make_client():
    os.environ["WALACOR_GATEWAY_API_KEYS"] = "test-key-1"
    os.environ["WALACOR_CONNECTIONS_ENABLED"] = "true"
    from gateway.config import get_settings
    get_settings.cache_clear()
    from gateway.connections.api import _reset_cache_for_tests
    _reset_cache_for_tests()
    from starlette.testclient import TestClient
    from gateway.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def teardown_function():
    from gateway.config import get_settings
    get_settings.cache_clear()
    for k in ("WALACOR_GATEWAY_API_KEYS", "WALACOR_CONNECTIONS_ENABLED"):
        os.environ.pop(k, None)


def test_endpoint_returns_200_and_marks_tile_unknown(monkeypatch):
    def _boom(ctx):
        raise RuntimeError("simulated probe failure")

    monkeypatch.setattr(
        "gateway.connections.builder.build_providers_tile", _boom
    )
    # _safe_build resolves the builder from the dict — patch there too
    from gateway.connections import builder as B
    monkeypatch.setitem(B._SYNC_BUILDERS, "providers", _boom)

    client = _make_client()
    resp = client.get("/v1/connections", headers={"X-API-Key": "test-key-1"})
    assert resp.status_code == 200
    body = resp.json()
    providers = next(t for t in body["tiles"] if t["id"] == "providers")
    assert providers["status"] == "unknown"
    assert providers["headline"] == "probe failed"
    assert "simulated probe failure" in providers["detail"]["error"]
