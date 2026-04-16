"""Anthropic API adapter: /v1/messages, SSE streaming, x-api-key.

Phase 14 additions:
  - parse_response extracts tool_use and server_tool_use content blocks into
    ToolInteraction objects and sets has_pending_tool_calls when stop_reason==tool_use.
  - parse_streamed_response accumulates input_json_delta chunks and detects
    stop_reason==tool_use via message_delta events.
  - build_tool_result_call constructs the Anthropic multi-turn format (assistant
    tool_use blocks + user tool_result blocks) for the active strategy loop.

Phase 24 additions: OpenAI ↔ Anthropic protocol bridge
  - When a request arrives at /v1/chat/completions (OpenAI format) but routes to
    an anthropic provider, parse_request translates the body to /v1/messages
    schema and sets metadata["_translated_from_openai"]=True.
  - build_forward_request rewrites URL to /v1/messages and replaces auth headers.
  - translate_response_body_for_client / translate_stream_for_client convert
    Anthropic responses back to OpenAI chat.completion(.chunk) format so OpenAI
    clients (OpenWebUI, etc.) get a compatible response.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, AsyncIterator

import gateway.util.json_utils as json

import httpx
from starlette.requests import Request

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter, ToolInteraction
from gateway.adapters.caching import detect_cache_hit, inject_cache_control
from gateway.config import get_settings
from gateway.util.session_id import resolve_session_id


_DEFAULT_MAX_TOKENS = 4096
_ANTHROPIC_VERSION = "2023-06-01"

# Map OpenAI `reasoning_effort` → Anthropic `thinking.budget_tokens`.
# Budgets are conservative — clients can override via explicit `thinking` in metadata.
_REASONING_EFFORT_BUDGET = {
    "low": 2000,
    "medium": 8000,
    "high": 16000,
}


def _parse_data_url(url: str) -> tuple[str | None, str | None]:
    """Parse a data: URL and return (media_type, base64_data). Returns (None, None) on failure."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None, None
    try:
        header, _, data = url[5:].partition(",")
        media_type = header.split(";")[0] or "image/png"
        # `;base64` presence indicates b64-encoded payload; we always assume base64
        return media_type, data
    except Exception:
        return None, None


def _oai_image_to_anthropic_block(image_url: Any) -> dict | None:
    """Convert an OpenAI image_url block value to an Anthropic image content block."""
    if isinstance(image_url, dict):
        url = image_url.get("url")
    else:
        url = image_url
    if not isinstance(url, str):
        return None
    if url.startswith("data:"):
        media_type, data = _parse_data_url(url)
        if not data:
            return None
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type or "image/png", "data": data},
        }
    # Any http(s) URL → Anthropic URL image source
    return {"type": "image", "source": {"type": "url", "url": url}}


def _oai_content_to_anthropic_blocks(content: Any) -> list[dict]:
    """Convert an OpenAI message `content` value into a list of Anthropic content blocks.

    Handles:
      - plain string          → [{"type": "text", "text": ...}]
      - list of OpenAI blocks → text / image_url / existing anthropic image/text pass-through
    Returns an empty list when there's nothing to translate.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    out: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text") or ""
            if txt:
                out.append({"type": "text", "text": txt})
        elif btype == "image_url":
            converted = _oai_image_to_anthropic_block(block.get("image_url"))
            if converted:
                out.append(converted)
        elif btype == "image":
            # Already Anthropic shape — pass through
            out.append(block)
        elif btype == "input_audio":
            # Anthropic has no equivalent today; drop but mark so audit can see it was stripped.
            pass
        # Unknown block types silently dropped (fail-open)
    return out


def _oai_tool_calls_to_anthropic_content(tool_calls: list[dict]) -> list[dict]:
    """Translate an OpenAI assistant's `tool_calls[]` into Anthropic `tool_use` content blocks."""
    out: list[dict] = []
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args) if raw_args else {}
            except Exception:
                parsed = {"_raw": raw_args}
        elif isinstance(raw_args, dict):
            parsed = raw_args
        else:
            parsed = {}
        out.append({
            "type": "tool_use",
            "id": tc.get("id") or "",
            "name": fn.get("name") or "",
            "input": parsed if isinstance(parsed, dict) else {},
        })
    return out


def _oai_messages_to_anthropic_messages(messages: list[dict]) -> tuple[list[dict], list[str]]:
    """Translate an OpenAI messages array to Anthropic messages + collected system prompts.

    Responsibilities:
      - Extract role:system messages into a separate list (caller joins into `system`).
      - Translate role:assistant messages with `tool_calls` to an Anthropic assistant
        message containing `tool_use` content blocks (alongside any text).
      - Translate role:tool / role:function messages to user-role messages containing
        `tool_result` content blocks. Consecutive tool results are merged into a single
        user message (Anthropic convention).
      - Translate multimodal content blocks (text + image) via _oai_content_to_anthropic_blocks.
      - Coerce unknown roles to user (Anthropic only accepts user/assistant).
    """
    systems: list[str] = []
    out: list[dict] = []

    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")

        if role == "system":
            txt_blocks = _oai_content_to_anthropic_blocks(m.get("content"))
            joined = "".join(b.get("text", "") for b in txt_blocks if b.get("type") == "text")
            if joined:
                systems.append(joined)
            continue

        if role in ("tool", "function"):
            # Convert to Anthropic tool_result content block under a user message.
            content_val = m.get("content")
            if isinstance(content_val, list):
                # If the client already sent Anthropic-style blocks, pass through;
                # otherwise, stringify text blocks.
                result_content: Any = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content_val
                )
            else:
                result_content = content_val if isinstance(content_val, str) else str(content_val or "")
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or m.get("name") or "",
                "content": result_content,
            }
            # Merge into previous user message if it's already a tool_result group.
            if out and out[-1]["role"] == "user" and isinstance(out[-1].get("content"), list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            # Assistant messages may carry both text content AND tool_calls.
            blocks = _oai_content_to_anthropic_blocks(m.get("content"))
            tool_calls = m.get("tool_calls")
            if tool_calls:
                blocks = blocks + _oai_tool_calls_to_anthropic_content(tool_calls)
            # Anthropic requires non-empty content; drop empty assistant turns.
            if not blocks:
                continue
            # Collapse to string form when it's a single text block (reduces overhead).
            if len(blocks) == 1 and blocks[0].get("type") == "text":
                out.append({"role": "assistant", "content": blocks[0]["text"]})
            else:
                out.append({"role": "assistant", "content": blocks})
            continue

        # user (and any unknown role coerced to user)
        if role != "user":
            role = "user"
        blocks = _oai_content_to_anthropic_blocks(m.get("content"))
        if not blocks:
            continue
        if len(blocks) == 1 and blocks[0].get("type") == "text":
            out.append({"role": "user", "content": blocks[0]["text"]})
        else:
            out.append({"role": "user", "content": blocks})

    return out, systems


def translate_oai_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Translate OpenAI-style tool definitions → Anthropic tool schema.

    Handles three input shapes:
      - OpenAI function calling:
          {"type": "function", "function": {"name", "description", "parameters"}}
      - Anthropic custom tool:
          {"name", "description", "input_schema"}
      - Anthropic server tool (web_search_20250305, code_execution_*, etc.):
          {"name", "type": "web_search_20250305", "max_uses": 3, ...}

    Server tools pass through unchanged — they have provider-specific `type`
    strings like "web_search_20250305" that Anthropic recognizes and executes
    internally. Dropping these would silently disable native web search.
    """
    out: list[dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            out.append({
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        elif "name" in t:
            # Anthropic-shape custom tool OR a server tool (web_search_20250305 etc.).
            # Pass through as-is; normalize `parameters` → `input_schema` if the
            # client used the OpenAI-ish key name for a custom tool.
            entry = dict(t)
            if "parameters" in entry and "input_schema" not in entry:
                entry["input_schema"] = entry.pop("parameters")
            out.append(entry)
    return out


def translate_oai_chat_to_anthropic(data: dict) -> dict:
    """Convert OpenAI /v1/chat/completions body → Anthropic /v1/messages body.

    Phase 24.3 scope: text + images + client-originated tool calls / tool results +
    reasoning_effort → extended thinking budget. Extracts system messages to the
    top-level `system` field; merges consecutive role:tool messages into a single
    user-role tool_result group.
    """
    out: dict[str, Any] = {"model": data.get("model", "")}

    msgs_out, system_parts = _oai_messages_to_anthropic_messages(data.get("messages") or [])

    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    out["messages"] = msgs_out

    # max_tokens is REQUIRED by Anthropic
    out["max_tokens"] = int(data.get("max_tokens") or _DEFAULT_MAX_TOKENS)

    for k in ("temperature", "top_p", "top_k", "stream"):
        if data.get(k) is not None:
            out[k] = data[k]

    stop = data.get("stop")
    if stop:
        out["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)

    # Extended thinking: honour OpenAI-style reasoning_effort OR an explicit
    # `thinking` object if the client passed one through.
    thinking_param = data.get("thinking")
    if isinstance(thinking_param, dict) and thinking_param.get("type") == "enabled":
        out["thinking"] = thinking_param
    else:
        effort = data.get("reasoning_effort") or data.get("reasoning")
        if isinstance(effort, dict):
            effort = effort.get("effort")
        if isinstance(effort, str) and effort.lower() in _REASONING_EFFORT_BUDGET:
            budget = _REASONING_EFFORT_BUDGET[effort.lower()]
            out["thinking"] = {"type": "enabled", "budget_tokens": budget}

    # Anthropic constraint: when extended thinking is enabled, temperature must be 1.0.
    if "thinking" in out:
        out["temperature"] = 1.0
        # budget_tokens must be < max_tokens
        budget = out["thinking"].get("budget_tokens", 0)
        if budget >= out["max_tokens"]:
            out["max_tokens"] = budget + 1024

    # Tools: translate OpenAI function-calling schema to Anthropic if the client sent any
    tools = data.get("tools")
    if tools:
        out["tools"] = translate_oai_tools_to_anthropic(tools)
    tool_choice = data.get("tool_choice")
    if tool_choice == "auto" or tool_choice == "none":
        out["tool_choice"] = {"type": tool_choice}
    elif tool_choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif isinstance(tool_choice, dict):
        # {"type": "function", "function": {"name": "x"}} → {"type": "tool", "name": "x"}
        fn = tool_choice.get("function") or {}
        if fn.get("name"):
            out["tool_choice"] = {"type": "tool", "name": fn["name"]}

    return out


_ANTHROPIC_STOP_TO_OAI_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def translate_anthropic_response_to_oai(data: dict, model_id: str) -> dict:
    """Convert an Anthropic /v1/messages response dict → OpenAI chat.completion dict.

    Extracts:
      - `text` blocks → message.content (joined)
      - `tool_use` blocks → message.tool_calls[] (OpenAI function-calling shape)
      - `thinking` / `redacted_thinking` blocks → message.reasoning_content (for audit clients)
    Cache token breakdown from usage is surfaced in OpenAI's
    prompt_tokens_details.cached_tokens shape.
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []

    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "thinking":
            reasoning_parts.append(block.get("thinking", ""))
        elif btype == "redacted_thinking":
            reasoning_parts.append("[redacted]")
        elif btype in ("tool_use", "server_tool_use"):
            args = block.get("input")
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(args) if args is not None else "{}",
                },
            })

    content = "".join(text_parts)

    stop_reason = data.get("stop_reason") or ""
    if stop_reason == "tool_use" and tool_calls:
        finish_reason = "tool_calls"
    else:
        finish_reason = _ANTHROPIC_STOP_TO_OAI_FINISH.get(stop_reason, "stop")

    message: dict[str, Any] = {"role": "assistant", "content": content or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)

    usage = data.get("usage") or {}
    input_tok = int(usage.get("input_tokens") or 0)
    output_tok = int(usage.get("output_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    # Anthropic's input_tokens excludes cache_read; OpenAI's prompt_tokens includes it.
    prompt_tok = input_tok + cache_read + cache_create
    usage_out: dict[str, Any] = {
        "prompt_tokens": prompt_tok,
        "completion_tokens": output_tok,
        "total_tokens": prompt_tok + output_tok,
    }
    if cache_read or cache_create:
        usage_out["prompt_tokens_details"] = {"cached_tokens": cache_read}

    return {
        "id": data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model") or model_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
        "usage": usage_out,
    }


# ── Error body translation ────────────────────────────────────────────────────
# Anthropic: {"type": "error", "error": {"type": "...", "message": "..."}}
# OpenAI:    {"error": {"message": "...", "type": "...", "code": null, "param": null}}

_ANTHROPIC_TO_OAI_ERROR_TYPE = {
    "invalid_request_error": "invalid_request_error",
    "authentication_error": "authentication_error",
    "permission_error": "permission_denied",
    "not_found_error": "not_found_error",
    "request_too_large": "invalid_request_error",
    "rate_limit_error": "rate_limit_exceeded",
    "api_error": "server_error",
    "overloaded_error": "server_error",
}


def translate_anthropic_error_to_oai(raw_body: bytes) -> bytes:
    """Translate an Anthropic error response body to OpenAI error shape.

    Fail-open: returns the original bytes if parsing fails or the body isn't
    recognizably an Anthropic error.
    """
    try:
        data = json.loads(raw_body)
    except Exception:
        return raw_body
    if not isinstance(data, dict):
        return raw_body
    err = data.get("error")
    if not isinstance(err, dict):
        return raw_body
    msg = err.get("message") or "upstream error"
    anthropic_type = err.get("type") or "api_error"
    out = {
        "error": {
            "message": msg,
            "type": _ANTHROPIC_TO_OAI_ERROR_TYPE.get(anthropic_type, "server_error"),
            "code": anthropic_type,
            "param": None,
        }
    }
    return json.dumps_bytes(out)


class _AnthropicToOpenAISSE:
    """Stateful translator: feed Anthropic SSE bytes, get OpenAI SSE bytes.

    Anthropic emits one event per `\\n\\n`-delimited block. Each block has
    `event: <type>` and `data: <json>` lines. We parse the JSON and convert
    each event into zero or more OpenAI chat.completion.chunk SSE chunks.

    Supported translations:
      - message_start              → delta {role: assistant, content: ""}
      - text_delta                 → delta {content: ...}
      - content_block_start (tool) → delta {tool_calls: [{id, type, function:{name}}]}
      - input_json_delta           → delta {tool_calls: [{function:{arguments}}]}
      - thinking_delta             → suppressed client-side (kept for audit)
      - message_delta              → delta {} with finish_reason
      - message_stop               → [DONE]

    Tool-call streaming uses `_tool_index_map` to preserve OpenAI's `index` ordering
    for incremental tool_calls updates.
    """

    def __init__(self, model_id: str) -> None:
        self._buf = b""
        self._created = int(time.time())
        self._model = model_id
        self._chatcmpl_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self._role_emitted = False
        self._done = False
        self._has_tool_calls = False
        # maps Anthropic content_block index → OpenAI tool_calls array index
        self._tool_index_map: dict[int, int] = {}
        self._next_tool_index = 0
        # Anthropic content_block indices that belong to server-side tool_use
        # (web_search_20250305 etc). Their input_json_delta events should NOT
        # bleed into the OpenAI tool_calls output — Anthropic runs them internally.
        self._server_tool_indices: set[int] = set()

    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        out: list[bytes] = []
        while b"\n\n" in self._buf:
            block, self._buf = self._buf.split(b"\n\n", 1)
            translated = self._translate_block(block)
            if translated:
                out.append(translated)
        return b"".join(out)

    def flush(self) -> bytes:
        """Emit final [DONE] marker if not already sent."""
        if self._done:
            return b""
        self._done = True
        return b"data: [DONE]\n\n"

    def _make_chunk(self, delta: dict, finish_reason: Any = None) -> bytes:
        payload = {
            "id": self._chatcmpl_id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }
        return b"data: " + json.dumps_bytes(payload) + b"\n\n"

    def _translate_block(self, block: bytes) -> bytes:
        data_json: dict | None = None
        for line in block.splitlines():
            if line.startswith(b"data: "):
                payload = line[6:].strip()
                if not payload or payload == b"[DONE]":
                    continue
                try:
                    data_json = json.loads(payload)
                except Exception:
                    return b""
                break
        if not data_json:
            return b""

        ev_type = data_json.get("type", "")

        if ev_type == "message_start":
            msg = data_json.get("message") or {}
            if msg.get("id"):
                self._chatcmpl_id = msg["id"]
            if msg.get("model"):
                self._model = msg["model"]
            self._role_emitted = True
            return self._make_chunk({"role": "assistant", "content": ""})

        if ev_type == "content_block_start":
            block_body = data_json.get("content_block") or {}
            btype = block_body.get("type")
            if btype == "tool_use":
                # Client-side custom tool call — surface to client as OpenAI tool_calls.
                anth_idx = data_json.get("index", 0)
                oai_idx = self._next_tool_index
                self._next_tool_index += 1
                self._tool_index_map[anth_idx] = oai_idx
                self._has_tool_calls = True
                return self._make_chunk({
                    "tool_calls": [{
                        "index": oai_idx,
                        "id": block_body.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block_body.get("name", ""),
                            "arguments": "",
                        },
                    }]
                })
            if btype == "server_tool_use":
                # Anthropic-native server tool (web_search, code_execution, …) — the
                # provider runs it internally. Track the index so we know to SWALLOW
                # its input_json_delta events too, but emit nothing to the client.
                anth_idx = data_json.get("index", 0)
                self._server_tool_indices.add(anth_idx)
                return b""
            # text / thinking / web_search_tool_result etc. → no client-facing emit at start
            return b""

        if ev_type == "content_block_delta":
            delta = data_json.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text", "")
                if text:
                    return self._make_chunk({"content": text})
                return b""
            if dtype == "input_json_delta":
                anth_idx = data_json.get("index", 0)
                if anth_idx in self._server_tool_indices:
                    # Server-side tool argument streaming — hide from client.
                    return b""
                oai_idx = self._tool_index_map.get(anth_idx)
                if oai_idx is None:
                    return b""
                partial = delta.get("partial_json", "")
                if not partial:
                    return b""
                return self._make_chunk({
                    "tool_calls": [{
                        "index": oai_idx,
                        "function": {"arguments": partial},
                    }]
                })
            if dtype in ("thinking_delta", "signature_delta"):
                # Claude extended-thinking: suppressed client-side; captured by the
                # gateway audit path via parse_streamed_response.
                return b""
            if dtype == "citations_delta":
                # Drop for now — only emitted when using Anthropic's native web_search tool.
                return b""
            return b""

        if ev_type == "message_delta":
            stop_reason = (data_json.get("delta") or {}).get("stop_reason")
            # If the only tool calls were server-side, from the client's point
            # of view this is a normal "stop" — nothing pending to execute.
            if stop_reason == "tool_use" and self._has_tool_calls:
                finish = "tool_calls"
            elif stop_reason == "tool_use":
                finish = "stop"
            else:
                finish = _ANTHROPIC_STOP_TO_OAI_FINISH.get(stop_reason or "", "stop")
            return self._make_chunk({}, finish_reason=finish)

        if ev_type == "message_stop":
            self._done = True
            return b"data: [DONE]\n\n"

        if ev_type == "error":
            # Upstream error surfaced mid-stream. Emit a plain SSE error event so
            # well-behaved clients see something; OpenWebUI logs this as a stream abort.
            err = (data_json.get("error") or {})
            msg = (err.get("message") or "upstream error").replace("\n", " ")
            return f'event: error\ndata: {{"error": {{"message": "{msg}", "type": "server_error"}}}}\n\n'.encode()

        # ping, content_block_stop → no client-visible output
        return b""


def _concat_messages_anthropic(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        if isinstance(m.get("content"), str):
            parts.append(m["content"])
        elif isinstance(m.get("content"), list):
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
        else:
            parts.append(str(m.get("content", "")))
    return "\n".join(parts)


def _merge_server_tool_pairs(interactions: list[ToolInteraction]) -> list[ToolInteraction]:
    """Collapse server_tool_use + web_search_tool_result pairs into one ToolInteraction.

    Anthropic emits TWO content blocks for a native server tool call:
      1. server_tool_use      — holds id, name, input (the query)
      2. web_search_tool_result — holds tool_use_id, content (the results)

    For audit purposes we want ONE row per call, not two. Match them by
    tool_use_id == id and merge the result's sources/output onto the
    corresponding server_tool_use entry.
    """
    if not interactions:
        return interactions

    # Build lookup of "web_search" tool_type entries (results) keyed by tool_id
    result_by_id = {}
    result_indices = []
    for i, t in enumerate(interactions):
        if t.tool_type == "web_search" and getattr(t, "metadata", None) and t.metadata.get("source") == "anthropic_native":
            if t.tool_id:
                result_by_id[t.tool_id] = t
                result_indices.append(i)

    if not result_by_id:
        return interactions

    merged: list[ToolInteraction] = []
    skip = set(result_indices)
    for i, t in enumerate(interactions):
        if i in skip:
            continue
        if t.tool_type == "server_tool" and t.tool_id in result_by_id:
            result = result_by_id[t.tool_id]
            merged.append(ToolInteraction(
                tool_id=t.tool_id,
                tool_type="server_tool",
                tool_name=t.tool_name,
                input_data=t.input_data,
                output_data=result.output_data,
                sources=result.sources,
                metadata={"source": "anthropic_native"},
            ))
        else:
            merged.append(t)
    return merged


def _parse_content_block(block: dict) -> tuple[str, str, ToolInteraction | None]:
    """Return (text_fragment, thinking_fragment, interaction_or_None) for one Anthropic content block.

    Known block types:
      - text, thinking, redacted_thinking          → reasoning/output text
      - tool_use                                    → custom tool invocation (client-side execution)
      - server_tool_use                             → server tool invocation (Anthropic executes)
      - web_search_tool_result                      → server tool result (captured for audit)
      - code_execution_tool_result (future)         → same pattern
    Other block types are silently ignored (fail-open).
    """
    block_type = block.get("type", "")

    if block_type == "text":
        return block.get("text", ""), "", None

    if block_type == "thinking":
        return "", block.get("thinking", ""), None

    if block_type == "redacted_thinking":
        return "", "[redacted]", None

    if block_type in ("tool_use", "server_tool_use"):
        return "", "", ToolInteraction(
            tool_id=block.get("id", ""),
            tool_type="function" if block_type == "tool_use" else "server_tool",
            tool_name=block.get("name"),
            input_data=block.get("input"),
            output_data=None,   # result returned in the next user message
            sources=None,
            metadata={"source": "anthropic_native"} if block_type == "server_tool_use" else None,
        )

    if block_type == "web_search_tool_result":
        # Anthropic's server-side web search returns results inline in the same response.
        # Capture URLs + titles for audit so the lineage dashboard can show what the model saw.
        content_field = block.get("content") or []
        sources_out: list[dict] = []
        error_info: dict | None = None
        if isinstance(content_field, list):
            for r in content_field:
                if not isinstance(r, dict):
                    continue
                if r.get("type") == "web_search_result":
                    sources_out.append({
                        "title": r.get("title") or "",
                        "url": r.get("url") or "",
                        "page_age": r.get("page_age") or "",
                    })
        elif isinstance(content_field, dict):
            # error shape: {"type": "web_search_tool_result_error", "error_code": "..."}
            if content_field.get("type") == "web_search_tool_result_error":
                error_info = {"error_code": content_field.get("error_code")}
        return "", "", ToolInteraction(
            tool_id=block.get("tool_use_id", ""),
            tool_type="web_search",
            tool_name="web_search",
            input_data=None,     # input is on the paired server_tool_use block above
            output_data={"error": error_info} if error_info else {"result_count": len(sources_out)},
            sources=sources_out or None,
            metadata={"source": "anthropic_native", "is_error": bool(error_info)},
        )

    return "", "", None


def _iter_sse_objects(chunks: list[bytes]):
    """Yield parsed JSON objects from raw SSE chunk bytes.

    Concatenates all chunks before splitting — a single `data:` line can be
    46KB+ (e.g. Anthropic's web_search_tool_result content) and TCP will
    almost always split it across multiple `aiter_bytes()` chunks. Parsing
    each chunk independently would silently drop the spanning event.
    """
    full = b"".join(chunks)
    for line in full.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _build_tool_interactions_from_map(tool_block_map: dict[int, dict]) -> list[ToolInteraction]:
    """Convert an accumulated tool_block_map into ToolInteraction objects."""
    interactions = []
    for _, tb in sorted(tool_block_map.items()):
        try:
            input_data = json.loads(tb["input_json"]) if tb["input_json"] else {}
        except json.JSONDecodeError:
            input_data = tb["input_json"]
        is_server = tb.get("kind") == "server_tool_use"
        interactions.append(ToolInteraction(
            tool_id=tb["id"],
            tool_type="server_tool" if is_server else "function",
            tool_name=tb["name"],
            input_data=input_data,
            output_data=tb.get("result_output"),
            sources=tb.get("result_sources"),
            metadata={"source": "anthropic_native"} if is_server else None,
        ))
    return interactions


def _handle_stream_event(
    obj: dict,
    content_parts: list[str],
    thinking_parts: list[str],
    tool_block_map: dict[int, dict],
    state: dict,
) -> None:
    """Process one decoded SSE event object (mutates content/thinking/tool_block_map/state)."""
    obj_type = obj.get("type", "")

    if obj_type == "message_start":
        msg = obj.get("message") or {}
        state["provider_request_id"] = msg.get("id")
        if msg.get("usage"):
            state["usage"] = msg["usage"]

    elif obj_type == "content_block_start":
        block = obj.get("content_block") or {}
        btype = block.get("type")
        if btype in ("tool_use", "server_tool_use"):
            idx = obj.get("index", 0)
            tool_block_map[idx] = {
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input_json": "",
                "kind": btype,
            }
        elif btype == "web_search_tool_result":
            # Non-delta'd result — the full results array is right here.
            tool_use_id = block.get("tool_use_id", "")
            content_field = block.get("content") or []
            sources_out: list[dict] = []
            if isinstance(content_field, list):
                for r in content_field:
                    if isinstance(r, dict) and r.get("type") == "web_search_result":
                        sources_out.append({
                            "title": r.get("title") or "",
                            "url": r.get("url") or "",
                            "page_age": r.get("page_age") or "",
                        })
            # Attach to the matching server_tool_use entry by id (not index).
            for tb in tool_block_map.values():
                if tb.get("id") == tool_use_id:
                    tb["result_sources"] = sources_out
                    tb["result_output"] = {"result_count": len(sources_out)}
                    break

    elif obj_type == "content_block_delta":
        idx = obj.get("index", 0)
        delta = obj.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            content_parts.append(delta.get("text", ""))
        elif dtype == "thinking_delta":
            thinking_parts.append(delta.get("thinking", ""))
        elif dtype == "input_json_delta" and idx in tool_block_map:
            tool_block_map[idx]["input_json"] += delta.get("partial_json", "")

    elif obj_type == "message_delta":
        state["stop_reason"] = (obj.get("delta") or {}).get("stop_reason")
        if obj.get("usage"):
            prev = state.get("usage") or {}
            state["usage"] = {**prev, **obj["usage"]}


class AnthropicAdapter(ProviderAdapter):
    """Adapter for Anthropic /v1/messages API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        prompt_caching: bool = True,
        beta_headers: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._prompt_caching = prompt_caching
        # Comma-separated list of anthropic-beta flags (e.g. "prompt-caching-2024-07-31")
        self._beta_headers = beta_headers.strip()

    def get_provider_name(self) -> str:
        return "anthropic"

    def supports_streaming(self) -> bool:
        return True

    async def parse_request(self, request: Request) -> ModelCall:
        body_bytes = await request.body()
        _cached = getattr(request.state, "_parsed_body", None)
        data = _cached if isinstance(_cached, dict) else None
        if data is None:
            try:
                data = json.loads(body_bytes.decode("utf-8"))
            except json.JSONDecodeError:
                raise ValueError("Invalid JSON body")

        # Phase 24: detect OpenAI-format clients hitting /v1/chat/completions and
        # translate the body to Anthropic /v1/messages schema. The flag in metadata
        # is later read by the forwarder to translate the response back.
        translated_from_openai = "/chat/completions" in request.url.path
        if translated_from_openai:
            data = translate_oai_chat_to_anthropic(data)
            body_bytes = json.dumps_bytes(data)

        # Phase 24.5: auto-inject Anthropic's NATIVE web_search server tool when
        # web_search is enabled and the client didn't bring their own tools.
        # Anthropic runs the search entirely server-side in the same streaming
        # forward — no gateway-side tool loop needed, which means real-time
        # streaming with zero extra latency. Our parse_response/stream handlers
        # capture the server_tool_use + web_search_tool_result blocks for audit.
        _settings = get_settings()
        if (
            _settings.web_search_enabled
            and not data.get("tools")
        ):
            data["tools"] = [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": _settings.web_search_max_results or 3,
            }]
            body_bytes = json.dumps_bytes(data)

        model_id = data.get("model") or ""
        messages = data.get("messages") or []
        prompt_text = _concat_messages_anthropic(messages)
        # Strip <think> tags echoed back from prior turns so audit trail stays clean
        if get_settings().thinking_strip_enabled and "<think>" in prompt_text.lower():
            from gateway.adapters.thinking import strip_thinking_tokens
            prompt_text, _ = strip_thinking_tokens(prompt_text)
        is_streaming = data.get("stream", False)
        metadata: dict[str, Any] = {}
        if request.headers.get("x-user-id"):
            metadata["user"] = request.headers["x-user-id"]
        metadata["session_id"] = resolve_session_id(request, get_settings().session_header_names_list, data)
        # System prompt (top-level Anthropic field, not in messages array)
        system_raw = data.get("system", "")
        if isinstance(system_raw, list):
            system_text = "\n".join(b.get("text", "") for b in system_raw
                                    if isinstance(b, dict) and b.get("type") == "text")
        elif isinstance(system_raw, str):
            system_text = system_raw
        else:
            system_text = ""
        if system_text:
            metadata["system_prompt"] = system_text
        # Inference params
        _ANTHROPIC_PARAMS = ("temperature", "top_p", "top_k", "max_tokens")
        params = {k: data[k] for k in _ANTHROPIC_PARAMS if data.get(k) is not None}
        if params:
            metadata["inference_params"] = params
        # Multimodal detection
        mm_count = sum(
            1 for m in messages
            for b in (m.get("content") if isinstance(m.get("content"), list) else [])
            if isinstance(b, dict) and b.get("type") == "image"
        )
        if mm_count:
            metadata["has_multimodal_input"] = True
            metadata["multimodal_input_count"] = mm_count
        if translated_from_openai:
            metadata["_translated_from_openai"] = True
        return ModelCall(
            provider=self.get_provider_name(),
            model_id=model_id,
            prompt_text=prompt_text,
            raw_body=body_bytes,
            is_streaming=is_streaming,
            metadata=metadata,
        )

    async def build_forward_request(self, call: ModelCall, original: Request) -> httpx.Request:
        translated = call.metadata.get("_translated_from_openai", False)

        if translated:
            # OpenAI clients hit /v1/chat/completions; Anthropic only accepts /v1/messages.
            url = f"{self._base_url}/v1/messages"
        else:
            url = f"{self._base_url}{original.url.path}"
            if original.url.query:
                url += f"?{original.url.query}"

        if translated:
            # Build a clean header set for the upstream call. Don't echo client's
            # Authorization (the gateway's API key) — Anthropic only wants x-api-key.
            headers = {
                "content-type": "application/json",
                "accept": "application/json",
                "x-api-key": self._api_key or "",
                "anthropic-version": _ANTHROPIC_VERSION,
            }
        else:
            headers = dict(original.headers)
            headers.pop("content-length", None)
            headers.pop("host", None)
            if self._api_key and "x-api-key" not in [h.lower() for h in headers]:
                headers["x-api-key"] = self._api_key
            headers.setdefault("anthropic-version", _ANTHROPIC_VERSION)

        if self._beta_headers:
            headers["anthropic-beta"] = self._beta_headers

        body = call.raw_body

        # Phase 24.2: if the gateway injected OpenAI-format tools after parse_request
        # ran (via prepare_tools), translate them here so Anthropic accepts the body.
        # Idempotent — already-Anthropic tools pass through translate_oai_tools_to_anthropic.
        if translated:
            try:
                data = json.loads(body)
                tools = data.get("tools")
                if tools and any(
                    isinstance(t, dict) and t.get("type") == "function" for t in tools
                ):
                    data["tools"] = translate_oai_tools_to_anthropic(tools)
                    # tool_choice: translate if in OpenAI shape
                    tc = data.get("tool_choice")
                    if isinstance(tc, str) and tc in ("auto", "none"):
                        data["tool_choice"] = {"type": tc}
                    elif isinstance(tc, dict) and tc.get("type") == "function":
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            data["tool_choice"] = {"type": "tool", "name": fn["name"]}
                    body = json.dumps_bytes(data)
            except (json.JSONDecodeError, TypeError):
                pass

        if self._prompt_caching and not translated:
            # Skip cache_control injection for translated bodies — messages have string
            # content (not the list-of-blocks shape inject_cache_control expects).
            try:
                data = json.loads(body)
                messages = data.get("messages")
                if messages:
                    data["messages"] = inject_cache_control(messages)
                    body = json.dumps_bytes(data)
            except (json.JSONDecodeError, TypeError):
                pass  # forward original body on parse failure

        return httpx.Request(
            method=original.method,
            url=url,
            headers=headers,
            content=body,
        )

    # ── Phase 24: response translation hooks ─────────────────────────────────
    def translate_response_body_for_client(self, raw_body: bytes, call: ModelCall) -> bytes:
        """Translate a non-streaming Anthropic response body to OpenAI chat.completion bytes.

        Called by the forwarder when call.metadata['_translated_from_openai'] is set.
        Returns raw_body unchanged on parse failure (fail-open).
        """
        try:
            data = json.loads(raw_body)
        except Exception:
            return raw_body
        oai = translate_anthropic_response_to_oai(data, call.model_id)
        return json.dumps_bytes(oai)

    async def translate_stream_for_client(
        self,
        chunks_iter: AsyncIterator[bytes],
        call: ModelCall,
    ) -> AsyncIterator[bytes]:
        """Translate Anthropic SSE stream → OpenAI chat.completion.chunk SSE stream.

        The audit/buffer in the forwarder is kept on the ORIGINAL Anthropic chunks
        (so parse_streamed_response still works); only the bytes yielded to the
        client are translated.
        """
        translator = _AnthropicToOpenAISSE(call.model_id)
        async for chunk in chunks_iter:
            translated = translator.feed(chunk)
            if translated:
                yield translated
        # final flush
        tail = translator.flush()
        if tail:
            yield tail

    def parse_response(self, response: httpx.Response) -> ModelResponse:
        try:
            data = response.json()
        except Exception:
            return ModelResponse(content="", usage=None, raw_body=response.content)

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_interactions: list[ToolInteraction] = []
        stop_reason = data.get("stop_reason")

        for block in (data.get("content") or []):
            if not isinstance(block, dict):
                continue
            text_frag, thinking_frag, interaction = _parse_content_block(block)
            if text_frag:
                text_parts.append(text_frag)
            if thinking_frag:
                thinking_parts.append(thinking_frag)
            if interaction:
                tool_interactions.append(interaction)

        # Merge server-side tool_use + paired web_search_tool_result entries into
        # one ToolInteraction so audit records a single row per native tool call,
        # with the query in input_data and the sources in output_data/sources.
        tool_interactions = _merge_server_tool_pairs(tool_interactions)

        # Native Anthropic server tools are executed server-side during the same
        # request — they're complete, not pending.
        has_pending = (
            stop_reason == "tool_use"
            and bool(tool_interactions)
            and any(t.tool_type == "function" for t in tool_interactions)
        )

        usage = data.get("usage")
        if usage and self._prompt_caching:
            cache_info = detect_cache_hit(usage)
            usage = {**usage, **cache_info}

        return ModelResponse(
            content="".join(text_parts),
            usage=usage,
            raw_body=response.content,
            provider_request_id=data.get("id"),
            tool_interactions=tool_interactions if tool_interactions else None,
            has_pending_tool_calls=has_pending,
            thinking_content="".join(thinking_parts) if thinking_parts else None,
        )

    def parse_streamed_response(self, chunks: list[bytes]) -> ModelResponse:
        """Assemble response from SSE chunks, capturing text, thinking, and tool_use for audit.

        Anthropic streaming events used:
          message_start        → provider_request_id, initial usage
          content_block_start  → detect tool_use blocks by index
          content_block_delta  → text_delta, thinking_delta, input_json_delta
          message_delta        → stop_reason + final usage
        """
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_block_map: dict[int, dict] = {}
        state: dict = {"provider_request_id": None, "stop_reason": None, "usage": None}

        for obj in _iter_sse_objects(chunks):
            _handle_stream_event(obj, content_parts, thinking_parts, tool_block_map, state)

        tool_interactions = _build_tool_interactions_from_map(tool_block_map)
        has_pending = state["stop_reason"] == "tool_use" and bool(tool_interactions)

        return ModelResponse(
            content="".join(content_parts),
            usage=state.get("usage"),
            raw_body=b"".join(chunks),
            provider_request_id=state["provider_request_id"],
            tool_interactions=tool_interactions if tool_interactions else None,
            has_pending_tool_calls=has_pending,
            thinking_content="".join(thinking_parts) if thinking_parts else None,
        )

    def build_tool_result_call(
        self,
        original_call: ModelCall,
        tool_calls: list[ToolInteraction],
        tool_results: list[dict],
    ) -> ModelCall:
        """Append assistant tool_use + user tool_result turns (Anthropic multi-turn format).

        Anthropic expects:
          1. Assistant message with tool_use content blocks.
          2. User message with tool_result content blocks containing the outputs.
        """
        body = json.loads(original_call.raw_body)
        messages: list[dict] = body.get("messages", [])

        messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tc.tool_id,
                    "name": tc.tool_name or "",
                    "input": tc.input_data if isinstance(tc.input_data, dict) else {},
                }
                for tc in tool_calls
            ],
        })
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": result["tool_call_id"],
                    "content": str(result["content"]),
                }
                for result in tool_results
            ],
        })

        body["messages"] = messages
        new_raw_body = json.dumps_bytes(body)
        return ModelCall(
            provider=original_call.provider,
            model_id=original_call.model_id,
            prompt_text=original_call.prompt_text,
            raw_body=new_raw_body,
            is_streaming=original_call.is_streaming,
            metadata=original_call.metadata,
        )
