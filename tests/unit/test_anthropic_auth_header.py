"""AnthropicAdapter.build_forward_request must replace the caller's auth header
with the gateway's configured Anthropic key, regardless of which auth header
the client sent. Skipping the swap leaks gateway API keys (wgk-*) to Anthropic,
which then returns 401 invalid x-api-key — the failure mode that masquerades
as "Anthropic API not working" on /v1/messages.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.adapters.anthropic import AnthropicAdapter


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _mock_request(path: str, headers: dict[str, str], body: bytes) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.headers = headers
    req.method = "POST"
    req.url = MagicMock()
    req.url.path = path
    req.url.query = ""
    req.state._parsed_body = None
    return req


@pytest.mark.anyio
async def test_v1_messages_swaps_x_api_key_even_when_client_sent_one():
    adapter = AnthropicAdapter(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-real-upstream-key",
    )
    body = b'{"model":"claude-opus-4-1-20250805","max_tokens":4,"messages":[{"role":"user","content":"ok"}]}'
    request = _mock_request(
        path="/v1/messages",
        headers={"x-api-key": "wgk-caller-bootstrap-key", "content-type": "application/json"},
        body=body,
    )

    call = await adapter.parse_request(request)
    upstream = await adapter.build_forward_request(call, request)

    assert upstream.headers["x-api-key"] == "sk-ant-real-upstream-key"
    assert "wgk-caller-bootstrap-key" not in dict(upstream.headers).values()
    assert upstream.headers["anthropic-version"]


@pytest.mark.anyio
async def test_v1_messages_strips_authorization_header_from_client():
    adapter = AnthropicAdapter(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-real-upstream-key",
    )
    body = b'{"model":"claude-opus-4-1-20250805","max_tokens":4,"messages":[{"role":"user","content":"ok"}]}'
    request = _mock_request(
        path="/v1/messages",
        headers={
            "authorization": "Bearer wgk-caller-bootstrap-key",
            "content-type": "application/json",
        },
        body=body,
    )

    call = await adapter.parse_request(request)
    upstream = await adapter.build_forward_request(call, request)

    assert "authorization" not in {h.lower() for h in upstream.headers}
    assert upstream.headers["x-api-key"] == "sk-ant-real-upstream-key"


@pytest.mark.anyio
async def test_translated_openai_path_still_uses_clean_headers():
    adapter = AnthropicAdapter(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-real-upstream-key",
    )
    body = b'{"model":"claude-opus-4-1-20250805","max_tokens":4,"messages":[{"role":"user","content":"ok"}]}'
    request = _mock_request(
        path="/v1/chat/completions",
        headers={"x-api-key": "wgk-caller-bootstrap-key", "content-type": "application/json"},
        body=body,
    )

    call = await adapter.parse_request(request)
    upstream = await adapter.build_forward_request(call, request)

    assert upstream.headers["x-api-key"] == "sk-ant-real-upstream-key"
    assert str(upstream.url).endswith("/v1/messages")
