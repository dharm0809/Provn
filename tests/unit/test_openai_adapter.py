"""Unit tests for OpenAI adapter."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.adapters.openai import OpenAIAdapter, _concat_messages
from gateway.adapters.base import ModelCall, ModelResponse


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def test_concat_messages():
    assert _concat_messages([{"content": "Hi"}, {"content": "Bye"}]) == "Hi\nBye"
    assert _concat_messages([{"role": "user", "content": "Hello"}]) == "Hello"
    assert _concat_messages([]) == ""


@pytest.mark.anyio
async def test_openai_adapter_parse_request():
    adapter = OpenAIAdapter(base_url="https://api.openai.com", api_key="sk-test")
    body = b'{"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}'
    request = MagicMock()
    request.body = AsyncMock(return_value=body)
    request.url = MagicMock(path="/v1/chat/completions")
    request.headers = {}
    request.method = "POST"
    request.state._parsed_body = None

    call = await adapter.parse_request(request)
    assert call.provider == "openai"
    assert call.model_id == "gpt-4"
    assert call.prompt_text == "Hello"
    assert call.raw_body == body
    assert call.is_streaming is False


def test_openai_parse_streamed_response():
    adapter = OpenAIAdapter(base_url="https://api.openai.com", api_key="")
    chunks = [
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
        b'data: {"choices":[{"delta":{"content":" there"}}]}\n',
        b"data: [DONE]\n",
    ]
    resp = adapter.parse_streamed_response(chunks)
    assert resp.content == "Hi there"
