"""Unit tests for the control plane API endpoints and cache refresh side effects."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from gateway.control.store import ControlPlaneStore
from gateway.control.api import (
    control_list_attestations,
    control_upsert_attestation,
    control_delete_attestation,
    control_list_policies,
    control_create_policy,
    control_update_policy,
    control_delete_policy,
    control_list_budgets,
    control_upsert_budget,
    control_delete_budget,
    control_status,
)


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeRequest:
    """Minimal request-like object for testing route handlers."""

    def __init__(self, query_params=None, body=None, path_params=None):
        self.query_params = query_params or {}
        self._body = body or {}
        self.path_params = path_params or {}

    async def json(self):
        return self._body


def _make_ctx(store, attestation_cache=None, policy_cache=None, budget_tracker=None,
              sync_client=None, wal_writer=None):
    ctx = MagicMock()
    ctx.control_store = store
    ctx.attestation_cache = attestation_cache
    ctx.policy_cache = policy_cache
    ctx.budget_tracker = budget_tracker
    ctx.sync_client = sync_client
    ctx.wal_writer = wal_writer
    ctx.content_analyzers = []
    ctx.session_chain = None
    ctx.http_client = None
    # Intelligence-layer slots are truthy on a bare MagicMock (each
    # attribute access auto-creates a child Mock), which makes the
    # control_status endpoint try to serialize their get_stats() output
    # and fail. Set them explicitly to None so the falsy-guard in
    # control_status skips them.
    ctx.anomaly_detector = None
    ctx.consistency_tracker = None
    ctx.field_registry = None
    ctx.intelligence_worker = None
    return ctx


def _make_settings(tenant_id="test-tenant"):
    settings = MagicMock()
    settings.gateway_tenant_id = tenant_id
    settings.gateway_id = "gw-test"
    settings.enforcement_mode = "enforced"
    settings.skip_governance = False
    settings.control_plane_url = ""
    settings.auth_mode = "api_key"
    settings.jwt_secret = ""
    settings.jwt_jwks_url = ""
    settings.provider_ollama_url = ""
    settings.provider_openai_key = ""
    settings.provider_openai_url = ""
    settings.provider_anthropic_key = ""
    settings.provider_anthropic_url = ""
    settings.provider_huggingface_key = ""
    settings.provider_huggingface_url = ""
    settings.model_routing_json = ""
    settings.token_budget_enabled = False
    settings.lineage_enabled = True
    return settings


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "control.db")
        s = ControlPlaneStore(db_path)
        yield s
        s.close()


# ---------------------------------------------------------------------------
# Attestation endpoints
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_attestations_empty(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        mock_ctx.return_value = _make_ctx(store)
        mock_settings.return_value = _make_settings()

        resp = await control_list_attestations(FakeRequest())
        data = json.loads(resp.body)
        assert data["count"] == 0
        assert data["attestations"] == []


@pytest.mark.anyio
async def test_upsert_and_list_attestation(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        mock_ctx.return_value = _make_ctx(store)
        mock_settings.return_value = _make_settings()

        req = FakeRequest(body={"model_id": "qwen3:4b", "provider": "ollama", "status": "active"})
        resp = await control_upsert_attestation(req)
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["model_id"] == "qwen3:4b"

        resp2 = await control_list_attestations(FakeRequest())
        data2 = json.loads(resp2.body)
        assert data2["count"] == 1


@pytest.mark.anyio
async def test_delete_attestation(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        mock_ctx.return_value = _make_ctx(store)
        mock_settings.return_value = _make_settings()

        req = FakeRequest(body={"model_id": "m1", "provider": "ollama"})
        resp = await control_upsert_attestation(req)
        aid = json.loads(resp.body)["attestation_id"]

        del_req = FakeRequest(path_params={"id": aid})
        resp2 = await control_delete_attestation(del_req)
        assert json.loads(resp2.body)["deleted"] is True


# ---------------------------------------------------------------------------
# Policy endpoints
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_and_list_policy(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        mock_ctx.return_value = _make_ctx(store)
        mock_settings.return_value = _make_settings()

        req = FakeRequest(body={
            "policy_name": "safety-v1",
            "enforcement_level": "blocking",
            "rules": [{"field": "toxicity", "operator": "gt", "value": "0.8"}],
        })
        resp = await control_create_policy(req)
        assert resp.status_code == 201
        assert "policy_id" in json.loads(resp.body)

        resp2 = await control_list_policies(FakeRequest())
        data = json.loads(resp2.body)
        assert data["count"] == 1
        assert data["policies"][0]["policy_name"] == "safety-v1"


@pytest.mark.anyio
async def test_update_policy(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        mock_ctx.return_value = _make_ctx(store)
        mock_settings.return_value = _make_settings()

        req = FakeRequest(body={"policy_name": "p1"})
        resp = await control_create_policy(req)
        policy_id = json.loads(resp.body)["policy_id"]

        update_req = FakeRequest(
            path_params={"id": policy_id},
            body={"policy_name": "p1-updated", "enforcement_level": "audit_only"},
        )
        resp2 = await control_update_policy(update_req)
        assert json.loads(resp2.body)["updated"] is True


@pytest.mark.anyio
async def test_delete_policy(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        mock_ctx.return_value = _make_ctx(store)
        mock_settings.return_value = _make_settings()

        req = FakeRequest(body={"policy_name": "p1"})
        resp = await control_create_policy(req)
        pid = json.loads(resp.body)["policy_id"]

        del_req = FakeRequest(path_params={"id": pid})
        resp2 = await control_delete_policy(del_req)
        assert json.loads(resp2.body)["deleted"] is True


# ---------------------------------------------------------------------------
# Budget endpoints
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_upsert_and_list_budget(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        mock_ctx.return_value = _make_ctx(store)
        mock_settings.return_value = _make_settings()

        req = FakeRequest(body={
            "tenant_id": "test-tenant", "user": "alice", "period": "monthly", "max_tokens": 100000,
        })
        resp = await control_upsert_budget(req)
        assert resp.status_code == 200

        resp2 = await control_list_budgets(FakeRequest())
        data = json.loads(resp2.body)
        assert data["count"] == 1
        assert data["budgets"][0]["max_tokens"] == 100000


@pytest.mark.anyio
async def test_delete_budget(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        mock_ctx.return_value = _make_ctx(store)
        mock_settings.return_value = _make_settings()

        req = FakeRequest(body={"tenant_id": "t1", "user": "", "period": "daily", "max_tokens": 5000})
        resp = await control_upsert_budget(req)
        bid = json.loads(resp.body)["budget_id"]

        del_req = FakeRequest(path_params={"id": bid})
        resp2 = await control_delete_budget(del_req)
        assert json.loads(resp2.body)["deleted"] is True


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_status_endpoint(store):
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx, \
         patch("gateway.control.api.get_settings") as mock_settings:
        ctx = _make_ctx(store, attestation_cache=None, policy_cache=None, wal_writer=None, sync_client=None)
        mock_ctx.return_value = ctx
        mock_settings.return_value = _make_settings()

        resp = await control_status(FakeRequest())
        data = json.loads(resp.body)
        assert data["gateway_id"] == "gw-test"
        assert data["control_plane_enabled"] is True
        assert data["sync_mode"] == "local"


# ---------------------------------------------------------------------------
# Store unavailable → 503
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_attestations_503_when_no_store():
    with patch("gateway.control.api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value = _make_ctx(None)
        resp = await control_list_attestations(FakeRequest())
        assert resp.status_code == 503
