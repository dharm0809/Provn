"""Tests for env-driven per-API-key tenant binding.

Covers the ``WALACOR_GATEWAY_API_KEYS=key:tenant,...`` extension that lets
multi-tenant deployments bind tenants to keys without running the embedded
control plane.

Layers exercised:
1. ``parse_api_keys_with_tenants`` — pure-function parser, all edge cases.
2. ``Settings.api_keys_list`` / ``Settings.api_keys_tenant_map`` — the
   properties that drive auth and the binding overlay.
3. ``_apply_api_key_tenant_binding`` — the middleware overlay, including the
   precedence rule (DB binding wins over env binding for the same key).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.auth.api_key import parse_api_keys_with_tenants
from gateway.auth.identity import CallerIdentity
from gateway.config import Settings, get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRequestState:
    pass


class _FakeMWRequest:
    """Stand-in for starlette.Request used by ``_apply_api_key_tenant_binding``."""

    def __init__(self, raw_key: str | None):
        headers: dict[str, str] = {}
        if raw_key:
            headers["Authorization"] = f"Bearer {raw_key}"
        self.headers = headers
        self.state = _FakeRequestState()


def _settings(raw: str) -> Settings:
    return Settings(gateway_api_keys=raw)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# parse_api_keys_with_tenants
# ---------------------------------------------------------------------------


class TestParseApiKeysWithTenants:
    def test_plain_keys_only(self):
        keys, tmap = parse_api_keys_with_tenants(["k1", "k2", "k3"])
        assert keys == ["k1", "k2", "k3"]
        assert tmap == {}

    def test_all_bound(self):
        keys, tmap = parse_api_keys_with_tenants(["k1:tA", "k2:tB"])
        assert keys == ["k1", "k2"]
        assert tmap == {"k1": "tA", "k2": "tB"}

    def test_mixed_bound_and_unbound(self):
        keys, tmap = parse_api_keys_with_tenants(["k1:tA", "k2:tB", "k3"])
        assert keys == ["k1", "k2", "k3"]
        assert tmap == {"k1": "tA", "k2": "tB"}
        assert "k3" not in tmap

    def test_empty_tenant_after_colon_treated_as_unbound(self, caplog):
        keys, tmap = parse_api_keys_with_tenants(["k1:"])
        assert keys == ["k1"]
        assert tmap == {}
        # Should log a warning so misconfig is visible.
        assert any(
            "empty tenant" in rec.message.lower() for rec in caplog.records
        )

    def test_empty_key_before_colon_drops_entry(self, caplog):
        keys, tmap = parse_api_keys_with_tenants([":tenantA"])
        assert keys == []
        assert tmap == {}
        assert any(
            "empty key" in rec.message.lower() for rec in caplog.records
        )

    def test_multiple_colons_split_on_first(self):
        # ``key:t:e:nant`` -> key="key", tenant="t:e:nant".
        # Tenants don't usually contain ``:`` but the format is positional —
        # the parser doesn't reject it.
        keys, tmap = parse_api_keys_with_tenants(["key:t:e:nant"])
        assert keys == ["key"]
        assert tmap == {"key": "t:e:nant"}

    def test_whitespace_stripped(self):
        keys, tmap = parse_api_keys_with_tenants(["  k1 : tA  ", "  k2  "])
        assert keys == ["k1", "k2"]
        assert tmap == {"k1": "tA"}

    def test_empty_entries_skipped(self):
        keys, tmap = parse_api_keys_with_tenants(["", "  ", "k1:tA"])
        assert keys == ["k1"]
        assert tmap == {"k1": "tA"}

    def test_wgk_style_key_with_tenant(self):
        """``wgk-{hex}`` keys still parse cleanly with the colon split."""
        keys, tmap = parse_api_keys_with_tenants(["wgk-abc123:acme-corp"])
        assert keys == ["wgk-abc123"]
        assert tmap == {"wgk-abc123": "acme-corp"}


# ---------------------------------------------------------------------------
# Settings properties
# ---------------------------------------------------------------------------


class TestSettingsProperties:
    def test_plain_keys_backward_compat(self):
        s = _settings("k1,k2,k3")
        assert s.api_keys_list == ["k1", "k2", "k3"]
        assert s.api_keys_tenant_map == {}

    def test_mixed_format(self):
        s = _settings("k1:tA,k2:tB,k3")
        assert s.api_keys_list == ["k1", "k2", "k3"]
        assert s.api_keys_tenant_map == {"k1": "tA", "k2": "tB"}

    def test_whitespace_around_entries(self):
        s = _settings("  k1:tA , k2 ")
        assert s.api_keys_list == ["k1", "k2"]
        assert s.api_keys_tenant_map == {"k1": "tA"}

    def test_empty_setting_yields_empty(self):
        s = _settings("")
        assert s.api_keys_list == []
        assert s.api_keys_tenant_map == {}

    def test_empty_tenant_after_colon_drops_binding(self):
        s = _settings("k1:")
        assert s.api_keys_list == ["k1"]
        assert s.api_keys_tenant_map == {}


# ---------------------------------------------------------------------------
# Middleware overlay — env path + precedence over DB
# ---------------------------------------------------------------------------


class TestEnvBindingMiddleware:
    def test_env_binding_applied_when_no_db(self, monkeypatch):
        from gateway.main import _apply_api_key_tenant_binding

        monkeypatch.setenv(
            "WALACOR_GATEWAY_API_KEYS", "sk-env-key:tenant-env"
        )
        get_settings.cache_clear()

        request = _FakeMWRequest("sk-env-key")
        request.state.caller_identity = CallerIdentity(user_id="alice")

        # No control plane in this deployment.
        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=None)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id == "tenant-env"
        assert request.state.caller_identity.source == "api_key_tenant_binding"
        assert request.state.caller_identity.user_id == "alice"

    def test_env_binding_applied_when_db_has_no_row(self, monkeypatch):
        """DB knows nothing about this key → env binding is the fallback."""
        from gateway.main import _apply_api_key_tenant_binding

        monkeypatch.setenv(
            "WALACOR_GATEWAY_API_KEYS", "sk-env-key:tenant-env"
        )
        get_settings.cache_clear()

        request = _FakeMWRequest("sk-env-key")
        request.state.caller_identity = CallerIdentity(user_id="alice")

        # Control plane is present but returns None for this key hash.
        fake_store = SimpleNamespace(get_key_tenant=lambda kh: None)
        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=fake_store)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id == "tenant-env"

    def test_db_binding_wins_over_env_binding(self, monkeypatch):
        """Same key bound differently in env vs DB — DB wins."""
        from gateway.main import _apply_api_key_tenant_binding

        monkeypatch.setenv(
            "WALACOR_GATEWAY_API_KEYS", "sk-shared:env-tenant"
        )
        get_settings.cache_clear()

        request = _FakeMWRequest("sk-shared")
        request.state.caller_identity = CallerIdentity(user_id="alice")

        fake_store = SimpleNamespace(get_key_tenant=lambda kh: "db-tenant")
        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=fake_store)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id == "db-tenant"

    def test_plain_env_key_leaves_tenant_none(self, monkeypatch):
        """Backward compat: plain key list → no env binding to apply."""
        from gateway.main import _apply_api_key_tenant_binding

        monkeypatch.setenv("WALACOR_GATEWAY_API_KEYS", "sk-plain,sk-other")
        get_settings.cache_clear()

        request = _FakeMWRequest("sk-plain")
        request.state.caller_identity = CallerIdentity(user_id="alice")

        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=None)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id is None

    def test_unknown_key_in_request_falls_through(self, monkeypatch):
        """Key not in env map and DB has no record → tenant stays None."""
        from gateway.main import _apply_api_key_tenant_binding

        monkeypatch.setenv(
            "WALACOR_GATEWAY_API_KEYS", "sk-known:tenant-A"
        )
        get_settings.cache_clear()

        request = _FakeMWRequest("sk-unknown")  # not in env map
        request.state.caller_identity = CallerIdentity(user_id="alice")

        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=None)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id is None

    def test_x_api_key_header_resolves_env_binding(self, monkeypatch):
        """Binding lookup also works for keys delivered via X-API-Key."""
        from gateway.main import _apply_api_key_tenant_binding

        monkeypatch.setenv(
            "WALACOR_GATEWAY_API_KEYS", "sk-h:tenant-h"
        )
        get_settings.cache_clear()

        request = _FakeMWRequest(None)
        request.headers["X-API-Key"] = "sk-h"
        request.state.caller_identity = CallerIdentity(user_id="alice")

        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=None)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id == "tenant-h"
