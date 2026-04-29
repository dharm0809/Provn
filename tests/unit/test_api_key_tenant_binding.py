"""Tests for per-API-key tenant binding (PR #18 follow-up).

Covers:
1. Store CRUD: set / get / unset and the existence-check used by the HTTP
   layer to 404 unknown keys.
2. The 60s in-process cache: a second `get_key_tenant` call must not hit
   SQLite when the first one populated the entry.
3. The control-plane HTTP API: POST sets, GET reads back, POST null
   unbinds, POST/GET on an unknown key returns 404.
4. The middleware overlay: when a control-plane binding exists for the
   validated raw key, ``CallerIdentity.tenant_id`` is overridden; when it
   doesn't, identity is left untouched (downstream falls back to
   ``settings.gateway_tenant_id``).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.auth.identity import CallerIdentity
from gateway.control.api import (
    control_get_key_tenant,
    control_set_key_tenant,
)
from gateway.control.store import ControlPlaneStore


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRequest:
    def __init__(self, body=None, path_params=None):
        self._body = body if body is not None else {}
        self.path_params = path_params or {}
        self.query_params = {}

    async def json(self):
        return self._body


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "control.db")
        s = ControlPlaneStore(db_path)
        yield s
        s.close()


def _hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _make_ctx(store):
    ctx = MagicMock()
    ctx.control_store = store
    return ctx


def _make_settings(tenant_id="default-tenant"):
    settings = MagicMock()
    settings.gateway_tenant_id = tenant_id
    return settings


# ---------------------------------------------------------------------------
# Store CRUD
# ---------------------------------------------------------------------------


class TestStoreTenantBinding:
    def test_get_returns_none_when_key_unknown(self, store):
        assert store.get_key_tenant("does-not-exist") is None

    def test_set_returns_false_when_no_assignments(self, store):
        # No policy assignments yet → set must report no rows updated.
        assert store.set_key_tenant("ghost-hash", "acme") is False
        assert store.get_key_tenant("ghost-hash") is None

    def test_bind_then_get_returns_tenant(self, store):
        kh = _hash("raw-key-1")
        store.set_key_policies(kh, ["pol-1"])
        # No tenant bound yet — lookup is None.
        assert store.get_key_tenant(kh) is None
        # Bind tenant.
        assert store.set_key_tenant(kh, "acme") is True
        assert store.get_key_tenant(kh) == "acme"

    def test_unbind_clears_tenant(self, store):
        kh = _hash("raw-key-2")
        store.set_key_policies(kh, ["pol-1"])
        store.set_key_tenant(kh, "acme")
        assert store.get_key_tenant(kh) == "acme"
        store.set_key_tenant(kh, None)
        assert store.get_key_tenant(kh) is None

    def test_empty_string_treated_as_unbind(self, store):
        kh = _hash("raw-key-3")
        store.set_key_policies(kh, ["pol-1"])
        store.set_key_tenant(kh, "acme")
        store.set_key_tenant(kh, "   ")
        assert store.get_key_tenant(kh) is None

    def test_tenant_persists_across_set_key_policies(self, store):
        """set_key_policies replaces rows but must preserve tenant binding."""
        kh = _hash("raw-key-4")
        store.set_key_policies(kh, ["pol-a"])
        store.set_key_tenant(kh, "acme")
        # Now overwrite the policy list — the tenant must survive.
        store.set_key_policies(kh, ["pol-b", "pol-c"])
        assert store.get_key_tenant(kh) == "acme"

    def test_has_key_reflects_assignment_presence(self, store):
        kh = _hash("raw-key-5")
        assert store.has_key(kh) is False
        store.set_key_policies(kh, ["pol-1"])
        assert store.has_key(kh) is True


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestKeyTenantCache:
    def test_second_call_does_not_hit_sqlite(self, store):
        kh = _hash("cached-key")
        store.set_key_policies(kh, ["pol-1"])
        store.set_key_tenant(kh, "acme")

        # ``set_key_tenant`` has already invalidated the cache, so the FIRST
        # ``get_key_tenant`` call must populate it from SQLite; the SECOND
        # must hit cache. We can't ``patch.object`` ``sqlite3.Connection``
        # methods (read-only attributes), so we wrap ``_ensure_conn`` to
        # return a proxy whose ``.execute`` we count.
        select_calls: list[str] = []
        real_conn = store._ensure_conn()

        class CountingConn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args, **kwargs):
                if (
                    "FROM key_policy_assignments" in sql
                    and "tenant_id IS NOT NULL" in sql
                ):
                    select_calls.append(sql)
                return self._conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        proxy = CountingConn(real_conn)
        with patch.object(store, "_ensure_conn", return_value=proxy):
            assert store.get_key_tenant(kh) == "acme"
            assert store.get_key_tenant(kh) == "acme"

        assert len(select_calls) == 1, (
            f"Expected exactly 1 SELECT (cache hit on 2nd call); got {len(select_calls)}"
        )

    def test_set_invalidates_cache(self, store):
        kh = _hash("invalidate-key")
        store.set_key_policies(kh, ["pol-1"])
        store.set_key_tenant(kh, "tenant-a")
        # Prime the cache.
        assert store.get_key_tenant(kh) == "tenant-a"
        # Re-bind.
        store.set_key_tenant(kh, "tenant-b")
        assert store.get_key_tenant(kh) == "tenant-b"

    def test_explicit_invalidation(self, store):
        kh = _hash("explicit-key")
        store.set_key_policies(kh, ["pol-1"])
        store.set_key_tenant(kh, "acme")
        assert store.get_key_tenant(kh) == "acme"
        store.invalidate_key_tenant_cache(kh)
        # Still resolves correctly after invalidation (refetched from DB).
        assert store.get_key_tenant(kh) == "acme"


# ---------------------------------------------------------------------------
# Control-plane HTTP API
# ---------------------------------------------------------------------------


class TestControlPlaneAPI:
    @pytest.mark.anyio
    async def test_post_sets_then_get_reads_back(self, store):
        kh = _hash("http-key")
        store.set_key_policies(kh, ["pol-1"])

        with patch("gateway.control.api.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = _make_ctx(store)

            resp = await control_set_key_tenant(
                FakeRequest(body={"tenant_id": "acme"}, path_params={"key_hash": kh})
            )
            assert resp.status_code == 200
            data = json.loads(resp.body)
            assert data["tenant_id"] == "acme"
            assert data["status"] == "updated"

            resp2 = await control_get_key_tenant(
                FakeRequest(path_params={"key_hash": kh})
            )
            assert resp2.status_code == 200
            assert json.loads(resp2.body)["tenant_id"] == "acme"

    @pytest.mark.anyio
    async def test_post_null_unbinds(self, store):
        kh = _hash("http-key-2")
        store.set_key_policies(kh, ["pol-1"])
        store.set_key_tenant(kh, "acme")

        with patch("gateway.control.api.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = _make_ctx(store)

            resp = await control_set_key_tenant(
                FakeRequest(body={"tenant_id": None}, path_params={"key_hash": kh})
            )
            assert resp.status_code == 200
            assert json.loads(resp.body)["tenant_id"] is None

            resp2 = await control_get_key_tenant(
                FakeRequest(path_params={"key_hash": kh})
            )
            assert json.loads(resp2.body)["tenant_id"] is None

    @pytest.mark.anyio
    async def test_post_unknown_key_returns_404(self, store):
        with patch("gateway.control.api.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = _make_ctx(store)

            resp = await control_set_key_tenant(
                FakeRequest(
                    body={"tenant_id": "acme"},
                    path_params={"key_hash": "ghost"},
                )
            )
            assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_get_unknown_key_returns_404(self, store):
        with patch("gateway.control.api.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = _make_ctx(store)

            resp = await control_get_key_tenant(
                FakeRequest(path_params={"key_hash": "ghost"})
            )
            assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_post_invalid_body_returns_400(self, store):
        kh = _hash("http-key-3")
        store.set_key_policies(kh, ["pol-1"])

        with patch("gateway.control.api.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = _make_ctx(store)

            # Missing tenant_id field
            resp = await control_set_key_tenant(
                FakeRequest(body={"other": "field"}, path_params={"key_hash": kh})
            )
            assert resp.status_code == 400

            # Non-string tenant_id
            resp2 = await control_set_key_tenant(
                FakeRequest(body={"tenant_id": 42}, path_params={"key_hash": kh})
            )
            assert resp2.status_code == 400


# ---------------------------------------------------------------------------
# Middleware overlay onto CallerIdentity
# ---------------------------------------------------------------------------


class _FakeRequestState:
    pass


class _FakeMWRequest:
    """Stand-in for starlette.Request used by _apply_api_key_tenant_binding."""

    def __init__(self, raw_key: str | None):
        headers: dict[str, str] = {}
        if raw_key:
            headers["Authorization"] = f"Bearer {raw_key}"
        self.headers = headers
        self.state = _FakeRequestState()


class TestMiddlewareTenantBinding:
    def test_binding_overrides_caller_identity_tenant(self, store):
        from gateway.main import _apply_api_key_tenant_binding

        raw_key = "sk-real-key-abc"
        kh = _hash(raw_key)
        store.set_key_policies(kh, ["pol-1"])
        store.set_key_tenant(kh, "acme")

        request = _FakeMWRequest(raw_key)
        # Header-resolved identity, no tenant_id (header path on its own
        # can't derive one from a bare API key).
        request.state.caller_identity = CallerIdentity(user_id="alice")

        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=store)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id == "acme"
        assert request.state.caller_identity.source == "api_key_tenant_binding"
        assert request.state.caller_identity.user_id == "alice"

    def test_no_binding_leaves_identity_untouched(self, store):
        """When the validated key has no DB binding, tenant_id stays None.

        Downstream `_resolve_tenant` falls back to settings.gateway_tenant_id —
        verifying that fallback path here would require the orchestrator,
        but we can assert the middleware doesn't synthesize a tenant value.
        """
        from gateway.main import _apply_api_key_tenant_binding

        raw_key = "sk-not-bound"
        # Note: no set_key_policies / set_key_tenant.

        request = _FakeMWRequest(raw_key)
        request.state.caller_identity = CallerIdentity(user_id="alice")

        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=store)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id is None
        # source must NOT have been bumped to api_key_tenant_binding
        assert request.state.caller_identity.source != "api_key_tenant_binding"

    def test_no_control_store_is_no_op(self, store):
        """Skip-governance / no-control-plane deployments must not crash."""
        from gateway.main import _apply_api_key_tenant_binding

        request = _FakeMWRequest("sk-anything")
        request.state.caller_identity = CallerIdentity(user_id="alice")

        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=None)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id is None

    def test_missing_authorization_header_is_no_op(self, store):
        from gateway.main import _apply_api_key_tenant_binding

        request = _FakeMWRequest(None)
        request.state.caller_identity = CallerIdentity(user_id="alice")

        with patch("gateway.main.get_pipeline_context") as mock_ctx:
            mock_ctx.return_value = SimpleNamespace(control_store=store)
            _apply_api_key_tenant_binding(request)

        assert request.state.caller_identity.tenant_id is None
