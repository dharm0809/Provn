"""Security batch: SEC-03…SEC-07 green+red path tests."""

from __future__ import annotations

import asyncio
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    return types.SimpleNamespace(wal_writer=None, walacor_client=None)


def _settings(**kw):
    defaults = dict(
        auth_mode="api_key", jwt_secret="", jwt_jwks_url="", jwt_issuer="", jwt_audience="",
        enforcement_mode="enforced", skip_governance=False,
        gateway_tenant_id="t-1", gateway_host="127.0.0.1",
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


# ─── SEC-03 ───────────────────────────────────────────────────────────────────

def test_sec03_green_api_key_mode(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.security.get_settings", lambda: _settings())
    from gateway.readiness.checks.security import _Sec03JwtIssuerAudience
    assert _run(_Sec03JwtIssuerAudience().run(_ctx())).status == "green"


def test_sec03_red_missing(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.security.get_settings",
                        lambda: _settings(auth_mode="jwt", jwt_issuer="", jwt_audience=""))
    from gateway.readiness.checks.security import _Sec03JwtIssuerAudience
    r = _run(_Sec03JwtIssuerAudience().run(_ctx()))
    assert r.status == "red"
    assert "jwt_issuer" in r.detail and "jwt_audience" in r.detail


# ─── SEC-04 ───────────────────────────────────────────────────────────────────

def test_sec04_green_with_jwks(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: _settings(auth_mode="jwt", jwt_jwks_url="https://example.com/jwks"),
    )
    from gateway.readiness.checks.security import _Sec04JwtKeyMaterial
    assert _run(_Sec04JwtKeyMaterial().run(_ctx())).status == "green"


def test_sec04_red_no_material(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: _settings(auth_mode="jwt", jwt_secret="short", jwt_jwks_url=""),
    )
    from gateway.readiness.checks.security import _Sec04JwtKeyMaterial
    assert _run(_Sec04JwtKeyMaterial().run(_ctx())).status == "red"


# ─── SEC-05 ───────────────────────────────────────────────────────────────────

def test_sec05_green_no_jwks_configured(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.security.get_settings", lambda: _settings())
    from gateway.readiness.checks.security import _Sec05JwksReachable
    assert _run(_Sec05JwksReachable().run(_ctx())).status == "green"


def test_sec05_red_unreachable(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: _settings(auth_mode="jwt", jwt_jwks_url="http://nonexistent.invalid/jwks"),
    )
    from gateway.readiness.checks.security import _Sec05JwksReachable
    assert _run(_Sec05JwksReachable().run(_ctx())).status == "red"


# ─── SEC-06 ───────────────────────────────────────────────────────────────────

def test_sec06_green_enforced(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.security.get_settings", lambda: _settings())
    from gateway.readiness.checks.security import _Sec06EnforcementMode
    assert _run(_Sec06EnforcementMode().run(_ctx())).status == "green"


def test_sec06_amber_audit_only(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.security.get_settings",
                        lambda: _settings(enforcement_mode="audit_only"))
    from gateway.readiness.checks.security import _Sec06EnforcementMode
    assert _run(_Sec06EnforcementMode().run(_ctx())).status == "amber"


# ─── SEC-07 ───────────────────────────────────────────────────────────────────

def test_sec07_green_tenant_set(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.security.get_settings", lambda: _settings())
    from gateway.readiness.checks.security import _Sec07TenantIdSet
    assert _run(_Sec07TenantIdSet().run(_ctx())).status == "green"


def test_sec07_amber_dev_empty(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.security.get_settings",
                        lambda: _settings(gateway_tenant_id="", gateway_host="127.0.0.1"))
    from gateway.readiness.checks.security import _Sec07TenantIdSet
    assert _run(_Sec07TenantIdSet().run(_ctx())).status == "amber"


def test_sec07_red_prod_empty(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.security.get_settings",
                        lambda: _settings(gateway_tenant_id="", gateway_host="10.0.1.5"))
    from gateway.readiness.checks.security import _Sec07TenantIdSet
    assert _run(_Sec07TenantIdSet().run(_ctx())).status == "red"
