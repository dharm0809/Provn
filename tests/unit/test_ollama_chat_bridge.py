"""Tests for /api/chat + /api/generate translation.

The bridge re-enters the orchestrator on a synthetic Request, then
translates the response back to Ollama-shape. These tests pin the four
properties OWUI relies on:

1. Ollama chat body → OpenAI body shape (incl. ``options`` hoisting).
2. Non-streaming OpenAI JSON → Ollama JSON (single message + ``done:true``).
3. OpenAI SSE → Ollama NDJSON (per-chunk deltas + terminator).
4. Half-closed SSE still emits a terminator so OWUI doesn't spin forever.
"""

from __future__ import annotations

import json

import pytest

from gateway.ollama_chat_bridge import (
    _ollama_chat_to_openai,
    _ollama_generate_to_openai,
    _openai_json_to_ollama,
    _translate_sse_to_ndjson,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Request translation ────────────────────────────────────────────────


def test_chat_to_openai_hoists_options_and_keeps_messages():
    out = _ollama_chat_to_openai(
        {
            "model": "claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "options": {"temperature": 0.7, "top_p": 0.9, "num_predict": 200},
        }
    )
    assert out["model"] == "claude-haiku-4-5"
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["stream"] is True
    assert out["temperature"] == 0.7
    assert out["top_p"] == 0.9
    assert out["max_tokens"] == 200  # num_predict → max_tokens


def test_chat_to_openai_format_json_becomes_response_format():
    out = _ollama_chat_to_openai({"model": "m", "messages": [], "format": "json"})
    assert out["response_format"] == {"type": "json_object"}


def test_chat_to_openai_drops_num_predict_minus_one():
    # Ollama uses -1 for "unlimited" — must not propagate as max_tokens=-1
    out = _ollama_chat_to_openai(
        {"model": "m", "messages": [], "options": {"num_predict": -1}}
    )
    assert "max_tokens" not in out


def test_generate_to_openai_builds_user_message_from_prompt():
    out = _ollama_generate_to_openai(
        {"model": "m", "prompt": "explain X", "system": "You are terse."}
    )
    assert out["messages"] == [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "explain X"},
    ]


# ── Non-streaming response translation ─────────────────────────────────


def test_openai_json_to_ollama_extracts_first_choice():
    out = _openai_json_to_ollama(
        "claude-haiku-4-5",
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        },
    )
    assert out["model"] == "claude-haiku-4-5"
    assert out["message"] == {"role": "assistant", "content": "hello"}
    assert out["done"] is True
    assert out["done_reason"] == "stop"
    assert out["prompt_eval_count"] == 5
    assert out["eval_count"] == 1


# ── Streaming response translation ─────────────────────────────────────


async def _aiter(chunks):
    for c in chunks:
        yield c


@pytest.mark.anyio
async def test_sse_to_ndjson_emits_per_chunk_then_done():
    sse_lines = [
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = []
    async for line in _translate_sse_to_ndjson(_aiter(sse_lines), "claude-haiku-4-5"):
        out.append(json.loads(line))

    contents = [o["message"]["content"] for o in out if not o["done"]]
    assert contents == ["hel", "lo"]
    assert out[-1]["done"] is True
    assert out[-1]["done_reason"] == "stop"


@pytest.mark.anyio
async def test_sse_to_ndjson_emits_terminator_on_half_closed_stream():
    """If upstream closes without [DONE], we still emit a terminator so
    OWUI doesn't spin forever waiting for one."""
    sse_lines = [b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n']
    out = []
    async for line in _translate_sse_to_ndjson(_aiter(sse_lines), "m"):
        out.append(json.loads(line))
    assert out[0]["message"]["content"] == "x"
    assert out[-1]["done"] is True


@pytest.mark.anyio
async def test_sse_to_ndjson_skips_malformed_chunks():
    sse_lines = [
        b"data: not-json\n\n",
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = []
    async for line in _translate_sse_to_ndjson(_aiter(sse_lines), "m"):
        out.append(json.loads(line))
    contents = [o["message"]["content"] for o in out if not o["done"]]
    assert contents == ["ok"]


@pytest.mark.anyio
async def test_sse_to_ndjson_handles_split_events_across_chunks():
    # Real upstreams often deliver one SSE event across two TCP chunks;
    # the buffer must reassemble before parsing.
    sse_lines = [
        b'data: {"choices":[{"delta"',
        b':{"content":"merged"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = []
    async for line in _translate_sse_to_ndjson(_aiter(sse_lines), "m"):
        out.append(json.loads(line))
    contents = [o["message"]["content"] for o in out if not o["done"]]
    assert contents == ["merged"]
