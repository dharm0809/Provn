"""Tests for /v1/openwebui/status endpoint."""

from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from starlette.requests import Request

from gateway.openwebui.status_api import openwebui_status


def _make_request() -> Request:
    scope = {"type": "http", "method": "GET", "path": "/v1/openwebui/status", "headers": []}
    return Request(scope)


class TestOpenWebUIStatus:
    @pytest.mark.anyio
    async def test_returns_banners_and_models(self):
        mock_ctx = MagicMock()
        mock_ctx.control_store = MagicMock()
        mock_ctx.control_store.list_attestations.return_value = [
            {"model_id": "qwen3:4b", "status": "active"},
            {"model_id": "gpt-4o", "status": "revoked"},
        ]
        mock_ctx.budget_tracker = None
        mock_ctx.wal_writer = None

        with patch("gateway.openwebui.status_api.get_pipeline_context", return_value=mock_ctx), \
             patch("gateway.openwebui.status_api.get_settings") as mock_settings:
            mock_settings.return_value.gateway_tenant_id = "test-tenant"
            mock_settings.return_value.token_budget_enabled = False
            mock_settings.return_value.disk_degraded_threshold = 0.8

            resp = await openwebui_status(_make_request())
            body = json.loads(resp.body)

            assert "banners" in body
            assert "models_status" in body
            assert "qwen3:4b" in body["models_status"]["active"]
            assert "gpt-4o" in body["models_status"]["revoked"]
            # Revoked model should generate a banner
            assert any("gpt-4o" in b["text"] for b in body["banners"])

    @pytest.mark.anyio
    async def test_budget_info_when_enabled(self):
        mock_ctx = MagicMock()
        mock_ctx.control_store = MagicMock()
        mock_ctx.control_store.list_attestations.return_value = []
        mock_ctx.budget_tracker = AsyncMock()
        mock_ctx.budget_tracker.get_snapshot = AsyncMock(return_value={
            "period": "monthly",
            "tokens_used": 9000,
            "max_tokens": 10000,
            "percent_used": 90.0,
        })
        mock_ctx.wal_writer = None

        with patch("gateway.openwebui.status_api.get_pipeline_context", return_value=mock_ctx), \
             patch("gateway.openwebui.status_api.get_settings") as mock_settings:
            mock_settings.return_value.gateway_tenant_id = "test-tenant"
            mock_settings.return_value.token_budget_enabled = True
            mock_settings.return_value.token_budget_max_tokens = 10000
            mock_settings.return_value.disk_degraded_threshold = 0.8

            resp = await openwebui_status(_make_request())
            body = json.loads(resp.body)

            assert body["budget"]["percent_used"] == 90.0
            assert body["budget"]["tokens_remaining"] == 1000
            # 90% should trigger a warning banner
            assert any("90%" in b.get("text", "") or "budget" in b.get("text", "").lower() for b in body["banners"])

    @pytest.mark.anyio
    async def test_no_control_store(self):
        mock_ctx = MagicMock()
        mock_ctx.control_store = None
        mock_ctx.budget_tracker = None
        mock_ctx.wal_writer = None

        with patch("gateway.openwebui.status_api.get_pipeline_context", return_value=mock_ctx), \
             patch("gateway.openwebui.status_api.get_settings") as mock_settings:
            mock_settings.return_value.gateway_tenant_id = "test-tenant"
            mock_settings.return_value.token_budget_enabled = False
            mock_settings.return_value.disk_degraded_threshold = 0.8

            resp = await openwebui_status(_make_request())
            body = json.loads(resp.body)
            assert body["models_status"]["active"] == []
            assert body["models_status"]["revoked"] == []
