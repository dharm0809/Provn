"""OpenAI API adapter: /v1/chat/completions and /v1/completions.

Phase 14 additions:
  - parse_response extracts tool_calls (Chat Completions) and web_search_call /
    code_interpreter_call / file_search_call (Responses API) into ToolInteraction objects.
  - parse_streamed_response accumulates tool_call deltas and detects finish_reason=tool_calls.
  - build_tool_result_call appends assistant tool_calls + tool result messages to support
    the active strategy tool loop for OpenAI-compat local servers.
"""

from __future__ import annotations

from typing import Any

import gateway.util.json_utils as json

import httpx
from starlette.requests import Request

import fnmatch
import logging

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter, ToolInteraction
from gateway.adapters.caching import detect_cache_hit
from gateway.config import get_settings
from gateway.util.session_id import resolve_session_id

logger = logging.getLogger(__name__)

# OpenAI reasoning models that support the Responses API with reasoning summaries.
_REASONING_MODEL_PATTERNS = ["o1-*", "o3-*", "o4-*"]


def _is_reasoning_model(model_id: str) -> bool:
    """Check if a model is an OpenAI reasoning model."""
    model_lower = model_id.lower()
    return any(fnmatch.fnmatch(model_lower, p) for p in _REASONING_MODEL_PATTERNS)


def _concat_messages(messages: list[dict]) -> str:
    """Concatenate message content for hashing."""
    parts = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
        else:
            parts.append(str(content) if content is not None else "")
    return "\n".join(parts)


_INFERENCE_PARAMS = ("temperature", "top_p", "seed", "max_tokens",
                     "presence_penalty", "frequency_penalty", "stop")


def _extract_inference_params(data: dict) -> dict:
    """Extract inference/sampling parameters from request body for audit."""
    return {k: data[k] for k in _INFERENCE_PARAMS if data.get(k) is not None}


def _extract_system_prompt(messages: list) -> str | None:
    """Extract and concatenate all system-role message content."""
    parts = []
    for m in messages:
        if m.get("role") != "system":
            continue
        c = m.get("content", "")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [b.get("text", "") for b in c
                      if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(parts) if parts else None


def _detect_multimodal(messages: list) -> tuple[bool, int]:
    """Return (has_multimodal, image_count) by scanning message content blocks."""
    count = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, list):
            count += sum(1 for b in c
                         if isinstance(b, dict) and b.get("type") in ("image_url", "image"))
    return count > 0, count


def _tool_interaction_from_raw(tc: dict) -> ToolInteraction:
    """Build a ToolInteraction from a raw tool_calls entry."""
    fn = tc.get("function") or {}
    try:
        input_data: dict | str | None = json.loads(fn.get("arguments", "{}"))
    except (json.JSONDecodeError, TypeError):
        input_data = fn.get("arguments")
    return ToolInteraction(
        tool_id=tc.get("id", ""),
        tool_type=tc.get("type", "function"),
        tool_name=fn.get("name"),
        input_data=input_data,
        output_data=None,
        sources=None,
        metadata=None,
    )


def _parse_responses_api_item(item: dict) -> tuple[str, ToolInteraction | None]:
    """Parse one Responses API output[] item. Returns (text_fragment, interaction_or_None)."""
    item_type = item.get("type", "")

    if item_type == "message":
        text = "".join(
            block.get("text", "")
            for block in (item.get("content") or [])
            if block.get("type") in ("text", "output_text")
        )
        return text, None

    if item_type == "web_search_call":
        action = item.get("action") or {}
        sources = [
            {"url": s.get("url"), "title": s.get("title")}
            for s in (action.get("sources") or [])
            if s.get("url")
        ]
        return "", ToolInteraction(
            tool_id=item.get("id", ""),
            tool_type="web_search",
            tool_name=None,
            input_data={"queries": action.get("queries"), "type": action.get("type")},
            output_data=None,
            sources=sources or None,
            metadata=None,
        )

    if item_type == "code_interpreter_call":
        return "", ToolInteraction(
            tool_id=item.get("id", ""),
            tool_type="code_interpreter",
            tool_name=None,
            input_data={"code": item.get("code")},
            output_data={"outputs": item.get("outputs")},
            sources=None,
            metadata=None,
        )

    if item_type == "file_search_call":
        return "", ToolInteraction(
            tool_id=item.get("id", ""),
            tool_type="file_search",
            tool_name=None,
            input_data={"queries": item.get("queries")},
            output_data={"results": item.get("results")},
            sources=None,
            metadata=None,
        )

    # Reasoning items are handled separately (not text fragments).
    return "", None


def _extract_reasoning_summary(output_items: list[dict]) -> str | None:
    """Extract reasoning summary text from Responses API output items."""
    summaries: list[str] = []
    for item in output_items:
        if item.get("type") != "reasoning":
            continue
        for entry in item.get("summary") or []:
            if entry.get("type") == "summary_text" and entry.get("text"):
                summaries.append(entry["text"])
    return "\n".join(summaries) if summaries else None


def _parse_chat_completions_choice(
    data: dict,
) -> tuple[str, list[ToolInteraction], bool]:
    """Extract content, tool interactions, and pending flag from a Chat Completions response."""
    choices = data.get("choices") or []
    if not choices:
        return "", [], False

    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    msg = choice.get("message") or choice.get("text")

    if isinstance(msg, str):
        return msg, [], False

    if not isinstance(msg, dict):
        return "", [], False

    content = msg.get("content", "") or msg.get("text", "") or ""
    raw_tc = msg.get("tool_calls") or []
    interactions = [_tool_interaction_from_raw(tc) for tc in raw_tc]
    pending = finish_reason == "tool_calls" and bool(raw_tc)
    return content, interactions, pending


def _parse_responses_api_output(
    output_items: list[dict],
) -> tuple[str, list[ToolInteraction]]:
    """Extract text and tool interactions from a Responses API output[] array."""
    text_parts: list[str] = []
    interactions: list[ToolInteraction] = []
    for item in output_items:
        frag, interaction = _parse_responses_api_item(item)
        if frag:
            text_parts.append(frag)
        if interaction:
            interactions.append(interaction)
    return "".join(text_parts), interactions


def _accumulate_tool_call_delta(
    tool_call_map: dict[int, dict[str, Any]],
    tc_delta: dict,
) -> None:
    """Merge one streaming tool_call delta into the accumulator dict (mutates in place)."""
    idx = tc_delta.get("index", 0)
    if idx not in tool_call_map:
        tool_call_map[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    entry = tool_call_map[idx]
    if tc_delta.get("id"):
        entry["id"] = tc_delta["id"]
    if tc_delta.get("type"):
        entry["type"] = tc_delta["type"]
    fn_delta = tc_delta.get("function") or {}
    if fn_delta.get("name"):
        entry["function"]["name"] += fn_delta["name"]
    if fn_delta.get("arguments"):
        entry["function"]["arguments"] += fn_delta["arguments"]


def _build_interactions_from_map(tool_call_map: dict[int, dict]) -> list[ToolInteraction]:
    """Convert accumulated streaming tool_call_map into ToolInteraction objects."""
    interactions = []
    for _, tc in sorted(tool_call_map.items()):
        fn = tc.get("function") or {}
        try:
            input_data: dict | str | None = json.loads(fn.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            input_data = fn.get("arguments")
        interactions.append(ToolInteraction(
            tool_id=tc.get("id", ""),
            tool_type=tc.get("type", "function"),
            tool_name=fn.get("name"),
            input_data=input_data,
            output_data=None,
            sources=None,
            metadata=None,
        ))
    return interactions


def _process_sse_line(
    payload: str,
    content_parts: list[str],
    tool_call_map: dict[int, dict[str, Any]],
    state: dict[str, Any],
) -> None:
    """Process one decoded SSE payload (mutates content_parts, tool_call_map, state)."""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return

    if state.get("provider_request_id") is None:
        state["provider_request_id"] = obj.get("id")

    choices = obj.get("choices") or [{}]
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    delta = choice.get("delta") or {}

    part = delta.get("content") or delta.get("text")
    if part:
        content_parts.append(part)

    for tc_delta in (delta.get("tool_calls") or []):
        _accumulate_tool_call_delta(tool_call_map, tc_delta)

    if finish_reason == "tool_calls":
        state["has_pending_tool_calls"] = True

    # Capture usage from the final SSE chunk (Ollama & OpenAI send it here).
    usage = obj.get("usage")
    if usage:
        state["usage"] = usage


class OpenAIAdapter(ProviderAdapter):
    """Adapter for OpenAI-compatible API (chat/completions and completions)."""

    # Class-level flag: if reasoning summary fails (org not verified), disable it.
    _reasoning_summary_available: bool | None = None

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def get_provider_name(self) -> str:
        return "openai"

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
        messages = data.get("messages", [])
        prompt_text = _concat_messages(messages) if messages else data.get("prompt", "") or ""
        # Strip <think> tags echoed back from prior turns so audit trail stays clean
        if get_settings().thinking_strip_enabled and "<think>" in prompt_text.lower():
            from gateway.adapters.thinking import strip_thinking_tokens
            prompt_text, _ = strip_thinking_tokens(prompt_text)
        is_streaming = data.get("stream", False)
        metadata: dict[str, Any] = {}
        if request.headers.get("x-user-id"):
            metadata["user"] = request.headers["x-user-id"]
        metadata["session_id"] = resolve_session_id(request, get_settings().session_header_names_list, data)
        params = _extract_inference_params(data)
        if params:
            metadata["inference_params"] = params
        system_prompt = _extract_system_prompt(messages)
        if system_prompt:
            metadata["system_prompt"] = system_prompt
        has_mm, mm_count = _detect_multimodal(messages)
        if has_mm:
            metadata["has_multimodal_input"] = True
            metadata["multimodal_input_count"] = mm_count
        # Tag for Responses API routing: reasoning models always, others when web search enabled.
        # gateway_web_search_enabled takes priority — keeps OpenAI on Chat Completions path
        # and routes web search through the gateway's active tool loop instead.
        is_openai_direct = self._base_url.rstrip("/").endswith("api.openai.com")
        settings = get_settings()
        if is_openai_direct:
            if _is_reasoning_model(model_id):
                metadata["_responses_api"] = True
            elif settings.gateway_web_search_enabled:
                metadata["_gateway_web_search"] = True
            elif settings.openai_web_search_enabled:
                metadata["_responses_api"] = True
                metadata["_openai_web_search"] = True
        # Strip non-standard fields from body before forwarding to provider.
        # OpenWebUI adds "metadata", "chat_id", etc. that OpenAI rejects.
        _OPENAI_STANDARD_FIELDS = {
            "model", "messages", "stream", "temperature", "top_p", "n", "stop",
            "max_tokens", "max_completion_tokens", "presence_penalty", "frequency_penalty",
            "logit_bias", "logprobs", "top_logprobs", "user", "tools", "tool_choice",
            "response_format", "seed", "service_tier", "stream_options", "store",
            "reasoning_effort", "parallel_tool_calls", "functions", "function_call",
        }
        if is_openai_direct and isinstance(data, dict):
            cleaned = {k: v for k, v in data.items() if k in _OPENAI_STANDARD_FIELDS}
            if cleaned != data:
                body_bytes = json.dumps(cleaned).encode("utf-8")

        return ModelCall(
            provider=self.get_provider_name(),
            model_id=model_id,
            prompt_text=prompt_text,
            raw_body=body_bytes,
            is_streaming=is_streaming,
            metadata=metadata,
        )

    async def build_forward_request(self, call: ModelCall, original: Request) -> httpx.Request:
        # Build clean headers for the upstream provider — only forward what's needed.
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # Preserve session tracking header if present.
        sid = original.headers.get("x-session-id")
        if sid:
            headers["X-Session-Id"] = sid

        # Route through Responses API: reasoning models (for summaries) or web-search-enabled models.
        if call.metadata.get("_responses_api") and self._base_url.rstrip("/").endswith("api.openai.com"):
            url = f"{self._base_url}/v1/responses"
            body = self._build_responses_api_body(call)
            reason = "web search" if call.metadata.get("_openai_web_search") else "reasoning summary"
            logger.info(
                "Routing %s to Responses API with %s",
                call.model_id, reason,
            )
            return httpx.Request(method="POST", url=url, headers=headers, content=body)

        # Standard Chat Completions path.
        url = f"{self._base_url}{original.url.path}"
        if original.url.query:
            url += f"?{original.url.query}"
        return httpx.Request(
            method=original.method,
            url=url,
            headers=headers,
            content=call.raw_body,
        )

    def _build_responses_api_body(self, call: ModelCall) -> bytes:
        """Transform a Chat Completions request body into Responses API format."""
        try:
            data = json.loads(call.raw_body)
        except (json.JSONDecodeError, TypeError):
            return call.raw_body

        messages = data.get("messages", [])
        # Separate system prompt from conversation.
        instructions = None
        input_messages: list[dict] = []
        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                instructions = msg.get("content", "")
            else:
                input_messages.append(msg)

        is_reasoning = _is_reasoning_model(call.model_id)
        uses_web_search = call.metadata.get("_openai_web_search", False)

        responses_body: dict[str, Any] = {
            "model": data.get("model", call.model_id),
            "input": input_messages if input_messages else data.get("messages", []),
        }

        # Add reasoning config only for reasoning models.
        if is_reasoning:
            reasoning_config: dict[str, Any] = {"effort": "medium"}
            if OpenAIAdapter._reasoning_summary_available is not False:
                reasoning_config["summary"] = "auto"
            responses_body["reasoning"] = reasoning_config

        # Add web_search tool for OpenAI native web search.
        if uses_web_search or is_reasoning:
            tools: list[dict[str, Any]] = []
            if uses_web_search:
                tools.append({"type": "web_search"})
            if tools:
                responses_body["tools"] = tools

        if instructions:
            responses_body["instructions"] = instructions
        # Carry over max_tokens if specified.
        if data.get("max_tokens"):
            responses_body["max_output_tokens"] = data["max_tokens"]
        if data.get("max_completion_tokens"):
            responses_body["max_output_tokens"] = data["max_completion_tokens"]
        if data.get("temperature") is not None:
            responses_body["temperature"] = data["temperature"]

        return json.dumps(responses_body).encode("utf-8")

    def parse_response(self, response: httpx.Response) -> ModelResponse:
        try:
            data = response.json()
        except Exception:
            return ModelResponse(content="", usage=None, raw_body=response.content)

        # Detect reasoning summary unavailable (org not verified) and cache for future.
        error = data.get("error")
        if error and isinstance(error, dict):
            param = error.get("param", "")
            if param == "reasoning.summary" and "verified" in error.get("message", "").lower():
                if OpenAIAdapter._reasoning_summary_available is not False:
                    OpenAIAdapter._reasoning_summary_available = False
                    logger.warning(
                        "Reasoning summary unavailable (org not verified) — "
                        "disabling for future requests. Responses API still used for reasoning models."
                    )
                # Signal that this needs a retry without summary via a sentinel content value.
                return ModelResponse(
                    content="__RETRY_WITHOUT_SUMMARY__",
                    usage=None,
                    raw_body=response.content,
                )

        cc_content, cc_tools, pending = _parse_chat_completions_choice(data)
        output_items = data.get("output") or []
        ra_text, ra_tools = _parse_responses_api_output(output_items)

        content = (cc_content + ra_text).strip() if ra_text else cc_content
        tool_interactions = cc_tools + ra_tools

        usage = data.get("usage")
        if usage:
            # Normalize Responses API usage fields to Chat Completions names
            # so downstream (hasher, lineage) always sees prompt_tokens/completion_tokens.
            if "input_tokens" in usage and "prompt_tokens" not in usage:
                usage["prompt_tokens"] = usage["input_tokens"]
            if "output_tokens" in usage and "completion_tokens" not in usage:
                usage["completion_tokens"] = usage["output_tokens"]
            if "total_tokens" not in usage:
                usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            cache_info = detect_cache_hit(usage)
            usage = {**usage, **cache_info}

        # Extract reasoning summary from Responses API output as thinking_content.
        thinking_content = _extract_reasoning_summary(output_items)
        if thinking_content:
            logger.info(
                "Captured reasoning summary (%d chars) from Responses API",
                len(thinking_content),
            )
        # Mark summary as available if we got it.
        if thinking_content and OpenAIAdapter._reasoning_summary_available is None:
            OpenAIAdapter._reasoning_summary_available = True

        # If no summary text but reasoning tokens were used, create an indicator.
        # Check both Responses API field (output_tokens_details) and Chat Completions
        # field (completion_tokens_details) since the usage may come from either path.
        if not thinking_content and usage:
            details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
            reasoning_tokens = details.get("reasoning_tokens", 0)
            if reasoning_tokens > 0:
                thinking_content = (
                    f"[Reasoning: {reasoning_tokens} tokens used for internal chain-of-thought. "
                    f"Verify your OpenAI organization to see full reasoning summaries.]"
                )

        return ModelResponse(
            content=content,
            usage=usage,
            raw_body=response.content,
            provider_request_id=data.get("id"),
            tool_interactions=tool_interactions if tool_interactions else None,
            has_pending_tool_calls=pending,
            thinking_content=thinking_content,
        )

    def parse_streamed_response(self, chunks: list[bytes]) -> ModelResponse:
        """Assemble content from SSE chunks, capturing tool_call deltas for audit."""
        content_parts: list[str] = []
        tool_call_map: dict[int, dict[str, Any]] = {}
        state: dict[str, Any] = {"provider_request_id": None, "has_pending_tool_calls": False}

        for chunk in chunks:
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    continue
                _process_sse_line(payload, content_parts, tool_call_map, state)

        tool_interactions = _build_interactions_from_map(tool_call_map)
        return ModelResponse(
            content="".join(content_parts),
            usage=state.get("usage"),
            raw_body=b"".join(chunks),
            provider_request_id=state["provider_request_id"],
            tool_interactions=tool_interactions if tool_interactions else None,
            has_pending_tool_calls=state["has_pending_tool_calls"],
        )

    def build_tool_result_call(
        self,
        original_call: ModelCall,
        tool_calls: list[ToolInteraction],
        tool_results: list[dict],
    ) -> ModelCall:
        """Append assistant tool_calls + tool result messages (OpenAI-compat format).

        prompt_text is preserved from the original call so the audit hash covers
        the user's intent, not the full multi-turn tool exchange.
        """
        body = json.loads(original_call.raw_body)
        messages: list[dict] = body.get("messages", [])

        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tc.tool_id,
                    "type": "function",
                    "function": {
                        "name": tc.tool_name or "",
                        "arguments": (
                            json.dumps(tc.input_data)
                            if isinstance(tc.input_data, dict)
                            else (tc.input_data or "{}")
                        ),
                    },
                }
                for tc in tool_calls
            ],
        })
        for result in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": result["tool_call_id"],
                "content": str(result["content"]),
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
