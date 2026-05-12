"""Live callability probe for model discovery.

discover_provider_models(..., live_check=True) must annotate each model with
``callable: bool`` (and ``unavailable_reason`` when False) by sending a
1-token chat completion to the configured upstream. This keeps the curated
allowlist honest — Anthropic/OpenAI list models in their /v1/models response
that the configured account cannot actually invoke.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.control.discovery import discover_provider_models, probe_model_callable


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        provider_ollama_url="http://ollama:11434",
        provider_openai_url="https://api.openai.com",
        provider_openai_key="sk-test",
        provider_anthropic_url="https://api.anthropic.com",
        provider_anthropic_key="sk-ant-test",
    )


def _http_response(status: int, body: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    return resp


@pytest.mark.anyio
async def test_probe_anthropic_404_marks_uncallable():
    settings = _settings()
    http = MagicMock()
    http.post = AsyncMock(return_value=_http_response(
        404,
        '{"type":"error","error":{"type":"not_found_error","message":"model: claude-3-5-haiku-latest"}}',
    ))

    callable_, reason = await probe_model_callable(
        "claude-3-5-haiku-latest", "anthropic", settings, http
    )

    assert callable_ is False
    assert reason and reason.startswith("HTTP 404:")
    assert "not_found_error" in reason
    # Probe must hit /v1/messages and use the configured anthropic key, not whatever
    # the caller passed
    call = http.post.call_args
    url = call.args[0] if call.args else call.kwargs.get("url")
    assert url == "https://api.anthropic.com/v1/messages"
    headers = call.kwargs["headers"]
    assert headers["x-api-key"] == "sk-ant-test"


@pytest.mark.anyio
async def test_probe_openai_200_marks_callable():
    settings = _settings()
    http = MagicMock()
    http.post = AsyncMock(return_value=_http_response(200, '{"id":"x"}'))

    callable_, reason = await probe_model_callable("gpt-4o-mini", "openai", settings, http)

    assert callable_ is True
    assert reason is None
    call = http.post.call_args
    assert call.kwargs["headers"]["authorization"] == "Bearer sk-test"


@pytest.mark.anyio
async def test_probe_unknown_provider_returns_reason():
    callable_, reason = await probe_model_callable("foo", "bedrock", _settings(), MagicMock())
    assert callable_ is False
    assert reason and "unknown provider" in reason


@pytest.mark.anyio
async def test_probe_transport_error_marks_uncallable():
    settings = _settings()
    http = MagicMock()
    http.post = AsyncMock(side_effect=RuntimeError("connection refused"))

    callable_, reason = await probe_model_callable("gpt-4o", "openai", settings, http)

    assert callable_ is False
    assert "RuntimeError" in reason
    assert "connection refused" in reason


@pytest.mark.anyio
async def test_discover_live_check_annotates_each_model():
    """discover_provider_models with live_check=True should probe every model and
    annotate callable + unavailable_reason in place. Anthropic returns two models;
    we mock the probe so one passes and one 404s."""
    settings = _settings()
    settings.provider_ollama_url = ""  # disable ollama for this test

    list_response = _http_response(
        200,
        '{"data":[{"id":"claude-opus-4-1-20250805"},{"id":"claude-3-5-haiku-latest"}]}',
    )
    list_response.json = MagicMock(return_value={
        "data": [
            {"id": "claude-opus-4-1-20250805"},
            {"id": "claude-3-5-haiku-latest"},
        ]
    })

    async def post_side_effect(url, **_kwargs):
        body = _kwargs.get("json") or {}
        if body.get("model") == "claude-3-5-haiku-latest":
            return _http_response(404, '{"error":{"type":"not_found_error","message":"model: claude-3-5-haiku-latest"}}')
        return _http_response(200, '{"id":"msg_x"}')

    async def get_side_effect(url, **_kwargs):
        # OpenAI uses GET /v1/models, Anthropic uses GET /v1/models. We only have
        # anthropic enabled here.
        return list_response

    http = MagicMock()
    http.post = AsyncMock(side_effect=post_side_effect)
    http.get = AsyncMock(side_effect=get_side_effect)

    # disable openai discovery; only anthropic
    settings.provider_openai_key = ""
    models = await discover_provider_models(settings, http, live_check=True)

    by_id = {m["model_id"]: m for m in models}
    assert by_id["claude-opus-4-1-20250805"]["callable"] is True
    assert "unavailable_reason" not in by_id["claude-opus-4-1-20250805"]
    assert by_id["claude-3-5-haiku-latest"]["callable"] is False
    assert "not_found_error" in by_id["claude-3-5-haiku-latest"]["unavailable_reason"]


@pytest.mark.anyio
async def test_discover_without_live_check_does_not_probe():
    """live_check defaults to False — no probe POST is sent."""
    settings = _settings()
    settings.provider_ollama_url = ""
    settings.provider_openai_key = ""
    list_response = MagicMock()
    list_response.status_code = 200
    list_response.json = MagicMock(return_value={"data": [{"id": "claude-opus-4-1-20250805"}]})

    http = MagicMock()
    http.get = AsyncMock(return_value=list_response)
    http.post = AsyncMock()

    models = await discover_provider_models(settings, http)  # live_check default = False

    assert http.post.await_count == 0
    assert all("callable" not in m for m in models)
