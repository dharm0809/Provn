"""Tests for the per-request tenant resolution helper in orchestrator.

`_resolve_tenant(request)` is the single source for tenant_id used by
caller-driven caches (semantic, attestation, response analyzer). It must:
  - prefer `request.state.caller_identity.tenant_id` when set
  - fall back to `settings.gateway_tenant_id` otherwise
  - finally fall back to the literal "default" so cache keys are non-empty
"""

from __future__ import annotations

import types

import pytest

from gateway.auth.identity import CallerIdentity
from gateway.config import get_settings
from gateway.pipeline.orchestrator import _resolve_tenant


class _State:
    pass


class _StubRequest:
    def __init__(self, *, identity: CallerIdentity | None = None):
        self.state = _State()
        if identity is not None:
            self.state.caller_identity = identity


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestResolveTenant:
    def test_prefers_caller_identity_tenant(self, monkeypatch):
        monkeypatch.setenv("WALACOR_GATEWAY_TENANT_ID", "settings-tenant")
        get_settings.cache_clear()
        req = _StubRequest(identity=CallerIdentity(user_id="alice", tenant_id="caller-tenant"))
        assert _resolve_tenant(req) == "caller-tenant"

    def test_falls_back_to_settings_when_identity_missing(self, monkeypatch):
        monkeypatch.setenv("WALACOR_GATEWAY_TENANT_ID", "settings-tenant")
        get_settings.cache_clear()
        req = _StubRequest()
        assert _resolve_tenant(req) == "settings-tenant"

    def test_falls_back_to_settings_when_caller_tenant_none(self, monkeypatch):
        monkeypatch.setenv("WALACOR_GATEWAY_TENANT_ID", "settings-tenant")
        get_settings.cache_clear()
        req = _StubRequest(identity=CallerIdentity(user_id="alice", tenant_id=None))
        assert _resolve_tenant(req) == "settings-tenant"

    def test_falls_back_to_settings_when_caller_tenant_empty(self, monkeypatch):
        monkeypatch.setenv("WALACOR_GATEWAY_TENANT_ID", "settings-tenant")
        get_settings.cache_clear()
        # Empty string from header path is treated as missing.
        req = _StubRequest(identity=CallerIdentity(user_id="alice", tenant_id=""))
        assert _resolve_tenant(req) == "settings-tenant"

    def test_default_when_settings_empty(self, monkeypatch):
        monkeypatch.setenv("WALACOR_GATEWAY_TENANT_ID", "")
        get_settings.cache_clear()
        req = _StubRequest()
        assert _resolve_tenant(req) == "default"

    def test_none_request_returns_settings_or_default(self, monkeypatch):
        monkeypatch.setenv("WALACOR_GATEWAY_TENANT_ID", "global")
        get_settings.cache_clear()
        assert _resolve_tenant(None) == "global"


class TestSemanticCacheTenantKey:
    """End-to-end: caller-A vs caller-B with the same prompt must not cross."""

    def test_different_callers_get_different_buckets(self):
        from gateway.cache.semantic_cache import SemanticCache

        cache = SemanticCache(max_entries=100, ttl=60)
        cache.put("m", "hello", b'{"r": "a"}', tenant_id="tenant-a")
        # Same prompt, different tenant: must miss
        assert cache.get("m", "hello", tenant_id="tenant-b") is None
        # Same tenant: must hit
        hit = cache.get("m", "hello", tenant_id="tenant-a")
        assert hit is not None and hit.response_body == b'{"r": "a"}'
