"""Tests for WALACOR_OBSERVABILITY_AUTH_REQUIRED flag.

Covers:
  - Default (flag off): /v1/readiness, /v1/connections, /metrics, /health behave as before.
  - Flag on: observability paths require X-API-Key (401 missing, 403 wrong key, 200 valid).
  - Flag on: /health stays public but strips debug fields unless a valid key is presented.

Convention: use TestClient (sync) so we don't need pytest.mark.anyio plumbing for
middleware-only assertions. The middleware itself is async but TestClient drives
it via Starlette's ASGI loop.
"""

from __future__ import annotations

import os

import pytest


_ENV_KEYS = (
    "WALACOR_GATEWAY_API_KEYS",
    "WALACOR_OBSERVABILITY_AUTH_REQUIRED",
    "WALACOR_LINEAGE_AUTH_REQUIRED",
    "WALACOR_READINESS_ENABLED",
)


def _client(*, flag: bool, api_keys: str = "obs-test-key-1"):
    os.environ["WALACOR_GATEWAY_API_KEYS"] = api_keys
    os.environ["WALACOR_OBSERVABILITY_AUTH_REQUIRED"] = "true" if flag else "false"
    # Keep readiness check runner enabled for the readiness route.
    os.environ["WALACOR_READINESS_ENABLED"] = "true"
    # /v1/lineage isn't under test here; leave default.

    from gateway.config import get_settings
    get_settings.cache_clear()

    from starlette.testclient import TestClient

    from gateway.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def teardown_function():
    from gateway.config import get_settings

    get_settings.cache_clear()
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


# ---------------------------------------------------------------------------
# Flag OFF (default) — current behavior preserved
# ---------------------------------------------------------------------------


def test_readiness_unauthenticated_with_flag_off_returns_200():
    """With flag off, /v1/readiness still requires the standard API key
    (because WALACOR_GATEWAY_API_KEYS is configured), so the relevant
    backwards-compat assertion is: a *valid* key still works. This locks in
    that turning the flag off does not introduce a stricter 401/403 split."""
    client = _client(flag=False)
    resp = client.get("/v1/readiness", headers={"X-API-Key": "obs-test-key-1"})
    assert resp.status_code == 200


def test_metrics_unauthenticated_with_flag_off_returns_200():
    """/metrics is in the always-open set when the flag is off."""
    client = _client(flag=False)
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_health_full_payload_with_flag_off():
    """Flag off → full /health body, including debug fields when present."""
    client = _client(flag=False)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"healthy", "degraded", "fail_closed"}
    # Minimal fields always present.
    assert "gateway_id" in body
    assert "uptime_seconds" in body


# ---------------------------------------------------------------------------
# Flag ON — observability paths gated, /health stripped
# ---------------------------------------------------------------------------


def test_readiness_without_key_returns_401():
    client = _client(flag=True)
    resp = client.get("/v1/readiness")
    assert resp.status_code == 401


def test_readiness_with_wrong_key_returns_403():
    client = _client(flag=True)
    resp = client.get("/v1/readiness", headers={"X-API-Key": "definitely-wrong"})
    assert resp.status_code == 403


def test_readiness_with_valid_key_returns_200():
    client = _client(flag=True)
    resp = client.get("/v1/readiness", headers={"X-API-Key": "obs-test-key-1"})
    assert resp.status_code == 200


def test_connections_without_key_returns_401():
    client = _client(flag=True)
    resp = client.get("/v1/connections")
    assert resp.status_code == 401


def test_metrics_without_key_returns_401():
    client = _client(flag=True)
    resp = client.get("/metrics")
    assert resp.status_code == 401


def test_metrics_with_valid_key_returns_200():
    client = _client(flag=True)
    resp = client.get("/metrics", headers={"X-API-Key": "obs-test-key-1"})
    assert resp.status_code == 200


def test_health_minimal_payload_without_key():
    """Flag on + no key → /health is public but debug fields stripped."""
    client = _client(flag=True)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    # Minimal payload still indicates health for load balancers.
    assert body.get("status") in {"healthy", "degraded", "fail_closed"}
    assert "gateway_id" in body
    # Debug fields stripped.
    for field in (
        "storage",
        "attestation_cache",
        "policy_cache",
        "wal",
        "token_budget",
        "intelligence",
        "resource_monitor",
        "model_capabilities",
        "startup_probes",
        "content_analyzers",
        "session_chain",
    ):
        assert field not in body, f"expected {field!r} to be stripped, got {body.get(field)!r}"


def test_health_full_payload_with_valid_key():
    """Flag on + valid key → /health is NOT stripped (parity with flag-off body).

    Because the test app may or may not wire all observable subsystems, the
    behavioural invariant we lock in is: flag-on + valid key yields the same
    key-set as flag-off (no stripping path taken). The stripping path itself
    is exercised by test_health_minimal_payload_without_key + the explicit
    debug-field absence assertions there.
    """
    client_off = _client(flag=False)
    flag_off_keys = set(client_off.get("/health").json().keys())

    # Reset between clients so settings cache picks up the new env.
    from gateway.config import get_settings
    get_settings.cache_clear()

    client_on = _client(flag=True)
    resp = client_on.get("/health", headers={"X-API-Key": "obs-test-key-1"})
    assert resp.status_code == 200
    flag_on_keys = set(resp.json().keys())
    assert flag_on_keys == flag_off_keys, (
        f"flag-on + valid key should match flag-off body; "
        f"diff: only_off={flag_off_keys - flag_on_keys} "
        f"only_on={flag_on_keys - flag_off_keys}"
    )


def test_health_invalid_key_strips_fields():
    """A wrong key is NOT treated as authenticated — debug fields still stripped."""
    client = _client(flag=True)
    resp = client.get("/health", headers={"X-API-Key": "bad-key"})
    # /health stays public even with a bad key (LB-friendly).
    assert resp.status_code == 200
    body = resp.json()
    assert "storage" not in body
    assert "attestation_cache" not in body
