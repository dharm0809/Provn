"""Auth-header isolation across all provider adapters.

Every ProviderAdapter.build_forward_request must:
  1) Replace the caller's `X-API-Key` / `Authorization` with the gateway's own
     configured upstream credential before forwarding.
  2) Never leak a `wgk-*` gateway key (or any other caller-supplied secret)
     to the upstream provider.

The Anthropic adapter had this bug (caller's wgk-* forwarded to Anthropic →
401 invalid x-api-key). This file pins the invariant for every adapter so the
same class of regression can't reappear in Ollama, HuggingFace, or Generic.
OpenAI is included even though it already constructs a clean header set,
because future refactors might introduce the same pattern.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.adapters.anthropic import AnthropicAdapter
from gateway.adapters.generic import GenericAdapter
from gateway.adapters.huggingface import HuggingFaceAdapter
from gateway.adapters.ollama import OllamaAdapter
from gateway.adapters.openai import OpenAIAdapter


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


CALLER_API_KEY = "wgk-caller-bootstrap-key"
UPSTREAM_KEY = "real-upstream-key-xyz"


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


# Each entry: (label, adapter, request_path, request_body)
_ADAPTERS = [
    (
        "anthropic_v1_messages",
        AnthropicAdapter(base_url="https://api.anthropic.com", api_key=UPSTREAM_KEY),
        "/v1/messages",
        b'{"model":"claude-opus-4-1-20250805","max_tokens":4,"messages":[{"role":"user","content":"ok"}]}',
    ),
    (
        "anthropic_translated_from_openai",
        AnthropicAdapter(base_url="https://api.anthropic.com", api_key=UPSTREAM_KEY),
        "/v1/chat/completions",
        b'{"model":"claude-opus-4-1-20250805","max_tokens":4,"messages":[{"role":"user","content":"ok"}]}',
    ),
    (
        "openai",
        OpenAIAdapter(base_url="https://api.openai.com", api_key=UPSTREAM_KEY),
        "/v1/chat/completions",
        b'{"model":"gpt-4o-mini","max_tokens":4,"messages":[{"role":"user","content":"ok"}]}',
    ),
    (
        "ollama",
        OllamaAdapter(base_url="http://ollama:11434", api_key=UPSTREAM_KEY),
        "/v1/chat/completions",
        b'{"model":"llama3.1:8b","messages":[{"role":"user","content":"ok"}]}',
    ),
    (
        "huggingface",
        HuggingFaceAdapter(base_url="https://api-inference.huggingface.co", api_key=UPSTREAM_KEY),
        "/v1/chat/completions",
        b'{"model":"meta-llama/Llama-3.1-8B-Instruct","messages":[{"role":"user","content":"ok"}]}',
    ),
    (
        "generic",
        GenericAdapter(
            base_url="https://upstream.example.com",
            api_key=UPSTREAM_KEY,
            model_path="model",
            prompt_path="messages.0.content",
            response_path="choices.0.message.content",
            auto_detect=True,
        ),
        "/v1/custom",
        b'{"model":"some-model","messages":[{"role":"user","content":"ok"}]}',
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "label,adapter,path,body",
    _ADAPTERS,
    ids=[entry[0] for entry in _ADAPTERS],
)
async def test_caller_x_api_key_is_not_leaked(label, adapter, path, body):
    """If the caller sends X-API-Key: wgk-*, it must NOT appear on the upstream request."""
    request = _mock_request(
        path=path,
        headers={"x-api-key": CALLER_API_KEY, "content-type": "application/json"},
        body=body,
    )

    call = await adapter.parse_request(request)
    upstream = await adapter.build_forward_request(call, request)

    upstream_values = list(upstream.headers.values())
    assert CALLER_API_KEY not in upstream_values, (
        f"{label}: caller's wgk-* key leaked to upstream headers: {dict(upstream.headers)}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "label,adapter,path,body",
    _ADAPTERS,
    ids=[entry[0] for entry in _ADAPTERS],
)
async def test_caller_authorization_is_not_leaked(label, adapter, path, body):
    """If the caller sends Authorization: Bearer wgk-*, it must NOT appear on the upstream request."""
    bearer = f"Bearer {CALLER_API_KEY}"
    request = _mock_request(
        path=path,
        headers={"authorization": bearer, "content-type": "application/json"},
        body=body,
    )

    call = await adapter.parse_request(request)
    upstream = await adapter.build_forward_request(call, request)

    upstream_values = list(upstream.headers.values())
    assert bearer not in upstream_values, (
        f"{label}: caller's Bearer token leaked to upstream headers: {dict(upstream.headers)}"
    )
    assert CALLER_API_KEY not in upstream_values, (
        f"{label}: caller's wgk-* key (Bearer body) leaked to upstream headers: {dict(upstream.headers)}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "label,adapter,path,body",
    _ADAPTERS,
    ids=[entry[0] for entry in _ADAPTERS],
)
async def test_configured_upstream_key_is_present(label, adapter, path, body):
    """The gateway's configured upstream key must appear on the outbound request."""
    request = _mock_request(
        path=path,
        headers={"x-api-key": CALLER_API_KEY, "content-type": "application/json"},
        body=body,
    )

    call = await adapter.parse_request(request)
    upstream = await adapter.build_forward_request(call, request)

    upstream_values = list(upstream.headers.values())
    # Either as x-api-key (anthropic) or in a `Bearer ...` Authorization header
    assert any(UPSTREAM_KEY in v for v in upstream_values), (
        f"{label}: upstream key not found in any outbound header: {dict(upstream.headers)}"
    )
