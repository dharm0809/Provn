"""/api/tags filters the upstream Ollama response by attestation set.

Without this filter, OpenWebUI (configured as an Ollama connection to
the gateway) sees every model the upstream Ollama has locally pulled,
regardless of whether it's been approved in the control plane —
defeating the governance loop for model selection. The filter must:

  * Return only models whose `name` (or `model`) is in the active
    attestation set with provider="ollama".
  * Pass through unchanged when no control_store is available
    (transparent-proxy mode; the request-time enforcement on
    /v1/chat/completions still catches unapproved models).
  * Pass through unchanged on upstream errors / malformed JSON
    (no point trying to filter what we can't parse).
"""
from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _stub_store(attestations: list[dict]):
    """Build a stub control_store.list_attestations() returning fixed data."""
    store = types.SimpleNamespace()
    store.list_attestations = lambda: attestations
    return store


def _build_ctx(http_client, control_store):
    ctx = types.SimpleNamespace(
        http_client=http_client,
        control_store=control_store,
    )
    return ctx


def _request():
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": "/api/tags",
                    "headers": [], "query_string": b""})


@pytest.mark.anyio
async def test_filters_to_attested_ollama_models_only(monkeypatch):
    """Three upstream models, one attested → one returned."""
    from gateway import ollama_proxy

    upstream_payload = json.dumps({
        "models": [
            {"name": "llama3.1:8b",     "model": "llama3.1:8b"},
            {"name": "qwen3:1.7b",      "model": "qwen3:1.7b"},
            {"name": "gemma4:e2b",      "model": "gemma4:e2b"},
        ]
    }).encode("utf-8")

    response_mock = types.SimpleNamespace(
        status_code=200, content=upstream_payload,
        headers={"content-type": "application/json"},
    )
    http = types.SimpleNamespace()
    http.get = AsyncMock(return_value=response_mock)
    store = _stub_store([
        {"model_id": "llama3.1:8b", "provider": "ollama", "status": "active"},
        # qwen3 attested but for a different provider — must NOT match.
        {"model_id": "qwen3:1.7b", "provider": "openai", "status": "active"},
        # gemma attested for ollama but revoked — must NOT match.
        {"model_id": "gemma4:e2b", "provider": "ollama", "status": "revoked"},
    ])
    ctx = _build_ctx(http, store)

    monkeypatch.setattr(ollama_proxy, "get_pipeline_context", lambda: ctx)
    monkeypatch.setattr(ollama_proxy, "get_settings",
                        lambda: types.SimpleNamespace(provider_ollama_url="http://x"))

    resp = await ollama_proxy.ollama_api_tags(_request())
    body = json.loads(resp.body)
    assert [m["name"] for m in body["models"]] == ["llama3.1:8b"]


@pytest.mark.anyio
async def test_no_control_store_passes_through(monkeypatch):
    """Transparent-proxy mode (no control plane) preserves upstream
    behavior — the gateway's request-time attestation enforcement at
    /v1/chat/completions still catches unapproved models, so this
    isn't a security gap."""
    from gateway import ollama_proxy

    upstream_payload = json.dumps({"models": [{"name": "x", "model": "x"}]}).encode()
    response_mock = types.SimpleNamespace(
        status_code=200, content=upstream_payload,
        headers={"content-type": "application/json"},
    )
    http = types.SimpleNamespace()
    http.get = AsyncMock(return_value=response_mock)
    ctx = _build_ctx(http, None)  # ← no control_store

    monkeypatch.setattr(ollama_proxy, "get_pipeline_context", lambda: ctx)
    monkeypatch.setattr(ollama_proxy, "get_settings",
                        lambda: types.SimpleNamespace(provider_ollama_url="http://x"))

    resp = await ollama_proxy.ollama_api_tags(_request())
    body = json.loads(resp.body)
    assert body["models"] == [{"name": "x", "model": "x"}]


@pytest.mark.anyio
async def test_empty_attestation_set_returns_empty_models(monkeypatch):
    """Control plane present but no ollama-provider models attested →
    OpenWebUI sees ZERO models from the gateway's Ollama connection.
    That's the correct prod state when the admin has only attested
    OpenAI/Anthropic models (as on prod after today's cleanup)."""
    from gateway import ollama_proxy

    upstream_payload = json.dumps({"models": [
        {"name": "llama3.1:8b", "model": "llama3.1:8b"},
    ]}).encode()
    response_mock = types.SimpleNamespace(
        status_code=200, content=upstream_payload,
        headers={"content-type": "application/json"},
    )
    http = types.SimpleNamespace()
    http.get = AsyncMock(return_value=response_mock)
    store = _stub_store([])  # zero attestations
    ctx = _build_ctx(http, store)

    monkeypatch.setattr(ollama_proxy, "get_pipeline_context", lambda: ctx)
    monkeypatch.setattr(ollama_proxy, "get_settings",
                        lambda: types.SimpleNamespace(provider_ollama_url="http://x"))

    resp = await ollama_proxy.ollama_api_tags(_request())
    body = json.loads(resp.body)
    assert body["models"] == []


@pytest.mark.anyio
async def test_upstream_error_passes_through_unfiltered(monkeypatch):
    """Upstream 502 (Ollama unreachable) → the proxy returns the 502.
    No point trying to filter what we can't parse."""
    from gateway import ollama_proxy

    http = types.SimpleNamespace()
    http.get = AsyncMock(side_effect=ConnectionError("refused"))
    store = _stub_store([{"model_id": "x", "provider": "ollama", "status": "active"}])
    ctx = _build_ctx(http, store)

    monkeypatch.setattr(ollama_proxy, "get_pipeline_context", lambda: ctx)
    monkeypatch.setattr(ollama_proxy, "get_settings",
                        lambda: types.SimpleNamespace(provider_ollama_url="http://x"))

    resp = await ollama_proxy.ollama_api_tags(_request())
    assert resp.status_code == 502
