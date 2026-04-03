"""Anthropic API adapter: /v1/messages, SSE streaming, x-api-key.

Phase 14 additions:
  - parse_response extracts tool_use and server_tool_use content blocks into
    ToolInteraction objects and sets has_pending_tool_calls when stop_reason==tool_use.
  - parse_streamed_response accumulates input_json_delta chunks and detects
    stop_reason==tool_use via message_delta events.
  - build_tool_result_call constructs the Anthropic multi-turn format (assistant
    tool_use blocks + user tool_result blocks) for the active strategy loop.
"""

from __future__ import annotations

from typing import Any

import gateway.util.json_utils as json

import httpx
from starlette.requests import Request

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter, ToolInteraction
from gateway.adapters.caching import detect_cache_hit, inject_cache_control
from gateway.config import get_settings
from gateway.util.session_id import resolve_session_id


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


def _parse_content_block(block: dict) -> tuple[str, ToolInteraction | None]:
    """Return (text_fragment, interaction_or_None) for one Anthropic content block."""
    block_type = block.get("type", "")

    if block_type == "text":
        return block.get("text", ""), None

    if block_type in ("tool_use", "server_tool_use"):
        return "", ToolInteraction(
            tool_id=block.get("id", ""),
            tool_type="function" if block_type == "tool_use" else "server_tool",
            tool_name=block.get("name"),
            input_data=block.get("input"),
            output_data=None,   # result returned in the next user message
            sources=None,
            metadata=None,
        )

    return "", None


def _iter_sse_objects(chunks: list[bytes]):
    """Yield parsed JSON objects from raw SSE chunk bytes."""
    for chunk in chunks:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
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
        interactions.append(ToolInteraction(
            tool_id=tb["id"],
            tool_type="function",
            tool_name=tb["name"],
            input_data=input_data,
            output_data=None,
            sources=None,
            metadata=None,
        ))
    return interactions


def _handle_stream_event(
    obj: dict,
    content_parts: list[str],
    tool_block_map: dict[int, dict],
    state: dict,
) -> None:
    """Process one decoded SSE event object (mutates content_parts, tool_block_map, state)."""
    obj_type = obj.get("type", "")

    if obj_type == "message_start":
        state["provider_request_id"] = (obj.get("message") or {}).get("id")

    elif obj_type == "content_block_start":
        block = obj.get("content_block") or {}
        if block.get("type") == "tool_use":
            idx = obj.get("index", 0)
            tool_block_map[idx] = {
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input_json": "",
            }

    elif obj_type == "content_block_delta":
        idx = obj.get("index", 0)
        delta = obj.get("delta") or {}
        if delta.get("type") == "text_delta":
            content_parts.append(delta.get("text", ""))
        elif delta.get("type") == "input_json_delta" and idx in tool_block_map:
            tool_block_map[idx]["input_json"] += delta.get("partial_json", "")

    elif obj_type == "message_delta":
        state["stop_reason"] = (obj.get("delta") or {}).get("stop_reason")


class AnthropicAdapter(ProviderAdapter):
    """Adapter for Anthropic /v1/messages API."""

    def __init__(self, base_url: str, api_key: str, *, prompt_caching: bool = True) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._prompt_caching = prompt_caching

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
        return ModelCall(
            provider=self.get_provider_name(),
            model_id=model_id,
            prompt_text=prompt_text,
            raw_body=body_bytes,
            is_streaming=is_streaming,
            metadata=metadata,
        )

    async def build_forward_request(self, call: ModelCall, original: Request) -> httpx.Request:
        url = f"{self._base_url}{original.url.path}"
        if original.url.query:
            url += f"?{original.url.query}"
        headers = dict(original.headers)
        # Strip content-length so httpx recomputes it from the actual (possibly modified) body.
        headers.pop("content-length", None)
        if self._api_key and "x-api-key" not in [h.lower() for h in headers]:
            headers["x-api-key"] = self._api_key

        body = call.raw_body
        if self._prompt_caching:
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

    def parse_response(self, response: httpx.Response) -> ModelResponse:
        try:
            data = response.json()
        except Exception:
            return ModelResponse(content="", usage=None, raw_body=response.content)

        text_parts: list[str] = []
        tool_interactions: list[ToolInteraction] = []
        stop_reason = data.get("stop_reason")

        for block in (data.get("content") or []):
            text_frag, interaction = _parse_content_block(block)
            if text_frag:
                text_parts.append(text_frag)
            if interaction:
                tool_interactions.append(interaction)

        has_pending = stop_reason == "tool_use" and bool(tool_interactions)

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
        )

    def parse_streamed_response(self, chunks: list[bytes]) -> ModelResponse:
        """Assemble response from SSE chunks, capturing tool_use blocks for audit.

        Anthropic streaming events used:
          message_start      → provider_request_id
          content_block_start → detect tool_use blocks by index
          content_block_delta → accumulate text_delta and input_json_delta
          message_delta      → stop_reason (tool_use triggers has_pending_tool_calls)
        """
        content_parts: list[str] = []
        tool_block_map: dict[int, dict] = {}
        state: dict = {"provider_request_id": None, "stop_reason": None}

        for obj in _iter_sse_objects(chunks):
            _handle_stream_event(obj, content_parts, tool_block_map, state)

        tool_interactions = _build_tool_interactions_from_map(tool_block_map)
        has_pending = state["stop_reason"] == "tool_use" and bool(tool_interactions)

        return ModelResponse(
            content="".join(content_parts),
            usage=None,
            raw_body=b"".join(chunks),
            provider_request_id=state["provider_request_id"],
            tool_interactions=tool_interactions if tool_interactions else None,
            has_pending_tool_calls=has_pending,
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
