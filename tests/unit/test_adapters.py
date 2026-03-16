"""Unit tests for Ollama TTL cache + native params and Generic adapter auto-detection."""

from __future__ import annotations

import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gateway.adapters.ollama import OllamaAdapter
from gateway.adapters.generic import GenericAdapter, _detect_request_format, _detect_response_format


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_starlette_request(body: dict, headers: dict | None = None) -> MagicMock:
    req = MagicMock()
    raw = json.dumps(body).encode()
    req.body = AsyncMock(return_value=raw)
    req.headers = headers or {}
    req.state._parsed_body = None
    return req


# ---------------------------------------------------------------------------
# Ollama: TTL digest cache
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ollama_digest_cache_hit():
    """Second fetch within TTL returns cached value without calling /api/show again."""
    adapter = OllamaAdapter("http://localhost:11434", digest_cache_ttl=300)
    with patch("gateway.adapters.ollama._fetch_model_digest_raw", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = "sha256:abc123"
        result1 = await adapter.fetch_model_hash("llama3.2")
        result2 = await adapter.fetch_model_hash("llama3.2")
    assert result1 == "sha256:abc123"
    assert result2 == "sha256:abc123"
    mock_fetch.assert_awaited_once()  # only one network call


@pytest.mark.anyio
async def test_ollama_digest_cache_expiry():
    """Expired cache entry triggers a fresh /api/show fetch."""
    adapter = OllamaAdapter("http://localhost:11434", digest_cache_ttl=1)
    with patch("gateway.adapters.ollama._fetch_model_digest_raw", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = "sha256:v1"
        await adapter.fetch_model_hash("llama3.2")
        # Backdate the cache entry to simulate expiry
        model_name = "llama3.2"
        digest, _ = adapter._digest_cache[model_name]
        adapter._digest_cache[model_name] = (digest, time.monotonic() - 2)  # 2s ago > 1s TTL
        mock_fetch.return_value = "sha256:v2"
        result = await adapter.fetch_model_hash("llama3.2")
    assert result == "sha256:v2"
    assert mock_fetch.await_count == 2


@pytest.mark.anyio
async def test_ollama_digest_cache_ttl_zero_disables_cache():
    """TTL=0 means every call fetches fresh; nothing is written to the cache."""
    adapter = OllamaAdapter("http://localhost:11434", digest_cache_ttl=0)
    with patch("gateway.adapters.ollama._fetch_model_digest_raw", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = "sha256:fresh"
        await adapter.fetch_model_hash("llama3.2")
        await adapter.fetch_model_hash("llama3.2")
    assert mock_fetch.await_count == 2
    assert adapter._digest_cache == {}  # cache never written


# ---------------------------------------------------------------------------
# Ollama: native inference params
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ollama_native_params_extracted_top_level():
    """top_k, num_ctx, repeat_penalty at top level are captured in inference_params."""
    adapter = OllamaAdapter("http://localhost:11434")
    body = {
        "model": "llama3.2",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.7,
        "top_k": 40,
        "num_ctx": 4096,
        "repeat_penalty": 1.1,
    }
    call = await adapter.parse_request(_make_starlette_request(body))
    params = call.metadata.get("inference_params", {})
    assert params["temperature"] == 0.7
    assert params["top_k"] == 40
    assert params["num_ctx"] == 4096
    assert params["repeat_penalty"] == 1.1


@pytest.mark.anyio
async def test_ollama_native_params_from_options_block():
    """Params inside options: {} are captured when not present at top level."""
    adapter = OllamaAdapter("http://localhost:11434")
    body = {
        "model": "llama3.2",
        "messages": [{"role": "user", "content": "hello"}],
        "options": {
            "top_k": 50,
            "mirostat": 2,
            "mirostat_tau": 5.0,
            "tfs_z": 1.0,
        },
    }
    call = await adapter.parse_request(_make_starlette_request(body))
    params = call.metadata.get("inference_params", {})
    assert params["top_k"] == 50
    assert params["mirostat"] == 2
    assert params["mirostat_tau"] == 5.0
    assert params["tfs_z"] == 1.0


# ---------------------------------------------------------------------------
# Generic: format detection helpers
# ---------------------------------------------------------------------------

def test_detect_request_format_openai_messages():
    assert _detect_request_format({"messages": [], "model": "x"}) == "openai_messages"


def test_detect_request_format_huggingface():
    assert _detect_request_format({"inputs": "hello"}) == "huggingface"


def test_detect_request_format_openai_legacy():
    assert _detect_request_format({"prompt": "hello", "model": "x"}) == "openai_legacy"


def test_detect_request_format_unknown():
    assert _detect_request_format({"query": "something"}) == "unknown"


def test_detect_response_format_openai():
    assert _detect_response_format({"choices": [{"message": {"content": "hi"}}]}) == "openai"


def test_detect_response_format_huggingface():
    assert _detect_response_format({"generated_text": "hi"}) == "huggingface"


def test_detect_response_format_huggingface_list():
    assert _detect_response_format([{"generated_text": "hi"}]) == "huggingface"


def test_detect_response_format_ollama_native():
    assert _detect_response_format({"response": "hi", "done": True}) == "ollama_native"


def test_detect_response_format_unknown():
    assert _detect_response_format({"output": "hi"}) == "unknown"


# ---------------------------------------------------------------------------
# Generic: parse_request auto-detect
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_generic_parse_request_openai_messages():
    """OpenAI-messages format: extracts inference params and system prompt."""
    adapter = GenericAdapter("http://custom:8080")
    body = {
        "model": "my-model",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello"},
        ],
        "temperature": 0.5,
        "top_p": 0.9,
    }
    call = await adapter.parse_request(_make_starlette_request(body))
    assert call.model_id == "my-model"
    assert "Hello" in call.prompt_text
    assert call.metadata.get("system_prompt") == "Be concise."
    params = call.metadata.get("inference_params", {})
    assert params["temperature"] == 0.5
    assert params["top_p"] == 0.9


@pytest.mark.anyio
async def test_generic_parse_request_huggingface_inputs():
    adapter = GenericAdapter("http://hf:8080")
    body = {"inputs": "What is the capital of France?"}
    call = await adapter.parse_request(_make_starlette_request(body))
    assert call.prompt_text == "What is the capital of France?"


@pytest.mark.anyio
async def test_generic_parse_request_fallback_to_path():
    """Unknown format falls back to configured path."""
    adapter = GenericAdapter(
        "http://custom:8080",
        prompt_path="$.query",
    )
    body = {"model": "x", "query": "my query"}
    call = await adapter.parse_request(_make_starlette_request(body))
    assert call.prompt_text == "my query"


@pytest.mark.anyio
async def test_generic_parse_request_auto_detect_disabled():
    """auto_detect=False always uses configured path even for OpenAI-shaped bodies."""
    adapter = GenericAdapter("http://custom:8080", auto_detect=False)
    body = {
        "model": "x",
        "messages": [{"role": "user", "content": "hello"}],
    }
    call = await adapter.parse_request(_make_starlette_request(body))
    # With auto_detect=False, uses $.messages[*].content path → still extracts content
    # but no inference_params or system_prompt metadata
    assert "inference_params" not in call.metadata
    assert "system_prompt" not in call.metadata


# ---------------------------------------------------------------------------
# Generic: parse_response auto-detect
# ---------------------------------------------------------------------------

def _make_httpx_response(body: dict | list) -> httpx.Response:
    raw = json.dumps(body).encode()
    return httpx.Response(200, content=raw, headers={"content-type": "application/json"})


def test_generic_parse_response_openai():
    adapter = GenericAdapter("http://x")
    resp = _make_httpx_response({
        "id": "req-1",
        "choices": [{"message": {"role": "assistant", "content": "Paris"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })
    result = adapter.parse_response(resp)
    assert result.content == "Paris"
    assert result.provider_request_id == "req-1"
    assert result.usage is not None


def test_generic_parse_response_huggingface():
    adapter = GenericAdapter("http://x")
    resp = _make_httpx_response({"generated_text": "Paris is the capital."})
    result = adapter.parse_response(resp)
    assert result.content == "Paris is the capital."


def test_generic_parse_response_huggingface_batch():
    adapter = GenericAdapter("http://x")
    resp = _make_httpx_response([{"generated_text": "Paris"}])
    result = adapter.parse_response(resp)
    assert result.content == "Paris"


def test_generic_parse_response_ollama_native():
    adapter = GenericAdapter("http://x")
    resp = _make_httpx_response({"response": "The answer is 42.", "done": True})
    result = adapter.parse_response(resp)
    assert result.content == "The answer is 42."


def test_generic_parse_response_fallback_to_path():
    adapter = GenericAdapter("http://x", response_path="$.output.text")
    resp = _make_httpx_response({"output": {"text": "custom output"}})
    result = adapter.parse_response(resp)
    assert result.content == "custom output"


def test_generic_parse_response_auto_detect_disabled():
    """auto_detect=False always uses configured path, skipping format detection."""
    adapter = GenericAdapter("http://x", response_path="$.answer", auto_detect=False)
    # A response with an OpenAI-shaped body AND an "answer" field — with auto_detect=False
    # it uses the configured path, so it reads $.answer, not $.choices[...]
    resp = _make_httpx_response({"choices": [{"message": {"content": "ignored"}}], "answer": "used"})
    result = adapter.parse_response(resp)
    assert result.content == "used"


# ---------------------------------------------------------------------------
# Generic: parse_streamed_response auto-detect
# ---------------------------------------------------------------------------

def _sse_chunks(*payloads: dict | str) -> list[bytes]:
    lines = []
    for p in payloads:
        if p == "[DONE]":
            lines.append(b"data: [DONE]\n\n")
        else:
            lines.append(f"data: {json.dumps(p)}\n\n".encode())
    return lines


def test_generic_streaming_openai_compat():
    adapter = GenericAdapter("http://x")
    chunks = _sse_chunks(
        {"id": "req-1", "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
        {"id": "req-1", "choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]},
        "[DONE]",
    )
    result = adapter.parse_streamed_response(chunks)
    assert result.content == "Hello world"
    assert result.provider_request_id == "req-1"


def test_generic_streaming_huggingface():
    adapter = GenericAdapter("http://x")
    chunks = _sse_chunks(
        {"token": {"text": "The "}, "generated_text": None},
        {"token": {"text": "answer"}, "generated_text": "The answer"},
    )
    result = adapter.parse_streamed_response(chunks)
    assert "The" in result.content
    assert "answer" in result.content


# ---------------------------------------------------------------------------
# Session ID: server-side generation
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ollama_session_id_generated_when_absent():
    """When X-Session-Id header is absent, a UUID4 is generated server-side."""
    adapter = OllamaAdapter("http://localhost:11434")
    body = {"model": "qwen3:4b", "messages": [{"role": "user", "content": "hi"}]}
    call = await adapter.parse_request(_make_starlette_request(body))
    sid = call.metadata.get("session_id")
    assert sid is not None
    assert uuid.UUID(sid).version == 4


@pytest.mark.anyio
async def test_ollama_session_id_preserved_from_header():
    """When X-Session-Id header is present, its value is used verbatim."""
    adapter = OllamaAdapter("http://localhost:11434")
    body = {"model": "qwen3:4b", "messages": [{"role": "user", "content": "hi"}]}
    provided = "my-existing-session-abc"
    call = await adapter.parse_request(
        _make_starlette_request(body, headers={"x-session-id": provided})
    )
    assert call.metadata["session_id"] == provided


@pytest.mark.anyio
async def test_generic_session_id_generated_when_absent():
    """GenericAdapter always assigns a UUID4 session_id when header is absent."""
    adapter = GenericAdapter("http://custom:8080")
    body = {"model": "x", "messages": [{"role": "user", "content": "hello"}]}
    call = await adapter.parse_request(_make_starlette_request(body))
    sid = call.metadata.get("session_id")
    assert sid is not None
    assert uuid.UUID(sid).version == 4


@pytest.mark.anyio
async def test_generic_session_id_preserved_from_header():
    """GenericAdapter uses caller-provided X-Session-Id verbatim."""
    adapter = GenericAdapter("http://custom:8080")
    body = {"model": "x", "messages": [{"role": "user", "content": "hello"}]}
    provided = "caller-session-xyz"
    call = await adapter.parse_request(
        _make_starlette_request(body, headers={"x-session-id": provided})
    )
    assert call.metadata["session_id"] == provided


@pytest.mark.anyio
async def test_each_request_gets_unique_session_id():
    """Two requests without X-Session-Id get different UUIDs."""
    adapter = OllamaAdapter("http://localhost:11434")
    body = {"model": "qwen3:4b", "messages": [{"role": "user", "content": "hi"}]}
    call1 = await adapter.parse_request(_make_starlette_request(body))
    call2 = await adapter.parse_request(_make_starlette_request(body))
    assert call1.metadata["session_id"] != call2.metadata["session_id"]


# ---------------------------------------------------------------------------
# anyio backend fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"
