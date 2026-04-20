"""Anthropic SSE translator state machine + TCP-split regression."""
from __future__ import annotations
import json
import pytest
from gateway.adapters.anthropic import _AnthropicToOpenAISSE, _iter_sse_objects


def _sse_block(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _parse_chunks(raw: bytes) -> list[dict]:
    out = []
    for line in raw.decode().splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


# ── role emit on message_start ────────────────────────────────────────────────

def test_message_start_emits_role_delta() -> None:
    t = _AnthropicToOpenAISSE("claude-3")
    out = t.feed(_sse_block("message_start", {
        "type": "message_start",
        "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
    }))
    chunks = _parse_chunks(out)
    assert any(c["choices"][0]["delta"].get("role") == "assistant" for c in chunks)


# ── text_delta → content ──────────────────────────────────────────────────────

def test_text_delta_becomes_content_chunk() -> None:
    t = _AnthropicToOpenAISSE("claude-3")
    t.feed(_sse_block("message_start", {"type": "message_start", "message": {}}))
    out = t.feed(_sse_block("content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Hello!"},
    }))
    chunks = _parse_chunks(out)
    contents = [c["choices"][0]["delta"].get("content", "") for c in chunks]
    assert "Hello!" in contents


# ── thinking_delta suppressed client-side ────────────────────────────────────

def test_thinking_delta_suppressed_in_sse_output() -> None:
    t = _AnthropicToOpenAISSE("claude-3")
    out = t.feed(_sse_block("content_block_delta", {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "Let me think..."},
    }))
    chunks = _parse_chunks(out)
    # Thinking should not appear as content in SSE output
    contents = " ".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert "Let me think" not in contents


# ── message_stop → [DONE] ─────────────────────────────────────────────────────

def test_message_stop_emits_done() -> None:
    t = _AnthropicToOpenAISSE("claude-3")
    out = t.feed(_sse_block("message_stop", {"type": "message_stop"}))
    assert b"[DONE]" in out


def test_flush_emits_done_when_not_yet_sent() -> None:
    t = _AnthropicToOpenAISSE("claude-3")
    assert b"[DONE]" in t.flush()


def test_flush_idempotent_after_done() -> None:
    t = _AnthropicToOpenAISSE("claude-3")
    t.feed(_sse_block("message_stop", {"type": "message_stop"}))
    assert t.flush() == b""


# ── finish_reason on message_delta ───────────────────────────────────────────

def test_message_delta_emits_finish_reason() -> None:
    t = _AnthropicToOpenAISSE("claude-3")
    out = t.feed(_sse_block("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 10},
    }))
    chunks = _parse_chunks(out)
    reasons = [c["choices"][0].get("finish_reason") for c in chunks]
    assert "stop" in reasons


# ── TCP-split regression: large data: line spanning multiple chunks ───────────

def test_iter_sse_objects_handles_tcp_split() -> None:
    large_content = "x" * 50_000
    data = {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": large_content}}
    full_sse = f"data: {json.dumps(data)}\n\n".encode()

    # Simulate TCP splitting the 50KB line across 3 chunks
    mid1, mid2 = len(full_sse) // 3, 2 * len(full_sse) // 3
    chunks = [full_sse[:mid1], full_sse[mid1:mid2], full_sse[mid2:]]

    objects = list(_iter_sse_objects(chunks))
    assert len(objects) == 1
    assert objects[0]["delta"]["text"] == large_content


def test_iter_sse_objects_skips_done_marker() -> None:
    chunks = [b"data: [DONE]\n\n"]
    assert list(_iter_sse_objects(chunks)) == []


def test_iter_sse_objects_skips_malformed_json() -> None:
    chunks = [b"data: {broken\n\ndata: {\"type\": \"ok\"}\n\n"]
    objects = list(_iter_sse_objects(chunks))
    assert len(objects) == 1
    assert objects[0]["type"] == "ok"
