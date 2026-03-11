"""Ollama adapter: OpenAI-compatible chat completions + model digest via /api/show.

Phase 14 additions:
  - parse_response and parse_streamed_response now extract tool_calls via the shared
    OpenAI-compat helpers (_parse_chat_completions_choice, _build_interactions_from_map).
  - build_tool_result_call appends assistant tool_calls + tool result messages to support
    the active strategy loop for local Ollama models.

Phase 16 additions:
  - Instance-level TTL digest cache replaces the module-level dict that never cleared.
    Configurable via WALACOR_OLLAMA_DIGEST_CACHE_TTL (default 1800 s; 0 = no cache).
  - Ollama-native inference params (top_k, num_ctx, num_predict, repeat_penalty,
    mirostat, mirostat_tau, mirostat_eta, tfs_z) extracted from both top-level and
    options: {} sub-dict and merged into audit metadata.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import httpx
from starlette.requests import Request

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter, ToolInteraction
from gateway.adapters.thinking import strip_thinking_tokens
from gateway.config import get_settings
from gateway.util.session_id import resolve_session_id
from gateway.adapters.openai import (
    _build_interactions_from_map,
    _detect_multimodal,
    _extract_inference_params,
    _extract_system_prompt,
    _parse_chat_completions_choice,
    _process_sse_line,
)

logger = logging.getLogger(__name__)

_OLLAMA_NATIVE_PARAMS = (
    "top_k", "num_ctx", "num_predict", "repeat_penalty",
    "mirostat", "mirostat_tau", "mirostat_eta", "tfs_z",
)


async def _fetch_model_digest_raw(
    base_url: str, model_name: str, client: httpx.AsyncClient | None = None
) -> str | None:
    """Call Ollama /api/show and return the digest string. No caching — callers handle it."""
    url = f"{base_url.rstrip('/')}/api/show"
    try:
        if client is not None:
            resp = await client.post(url, json={"name": model_name}, timeout=5.0)
        else:
            async with httpx.AsyncClient() as c:
                resp = await c.post(url, json={"name": model_name}, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            # Ollama returns digest under "details.digest" or top-level "digest"
            digest = (data.get("details") or {}).get("digest") or data.get("digest")
            if digest:
                return str(digest)
    except Exception as e:
        logger.warning("Failed to fetch Ollama model digest for %s: %s", model_name, e)
    return None


def _concat_messages(messages: list[dict]) -> str:
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


class OllamaAdapter(ProviderAdapter):
    """Adapter for Ollama using its OpenAI-compatible /v1/chat/completions endpoint.

    Fetches the model digest from /api/show and stores it as model_hash on ModelResponse.
    Set WALACOR_PROVIDER_OLLAMA_URL to point at your Ollama instance (default: http://localhost:11434).

    Digest cache is instance-level with a configurable TTL (WALACOR_OLLAMA_DIGEST_CACHE_TTL,
    default 1800 s). Set to 0 to always fetch fresh — useful when weights are updated frequently
    via `ollama pull` without restarting the gateway.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        digest_cache_ttl: int = 1800,
        thinking_strip_enabled: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._digest_cache_ttl = digest_cache_ttl
        self._thinking_strip_enabled = thinking_strip_enabled
        # model_name -> (digest, fetched_at_monotonic)
        self._digest_cache: dict[str, tuple[str, float]] = {}

    def get_provider_name(self) -> str:
        return "ollama"

    def supports_streaming(self) -> bool:
        return True

    async def parse_request(self, request: Request) -> ModelCall:
        body_bytes = await request.body()
        try:
            data = json.loads(body_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON body")
        model_id = data.get("model") or ""
        messages = data.get("messages", [])
        prompt_text = _concat_messages(messages) if messages else data.get("prompt", "") or ""
        is_streaming = data.get("stream", False)
        metadata: dict[str, Any] = {}
        if request.headers.get("x-user-id"):
            metadata["user"] = request.headers["x-user-id"]
        metadata["session_id"] = resolve_session_id(request, get_settings().session_header_names_list)

        # OpenAI-standard inference params
        params = _extract_inference_params(data)
        # Ollama-native params: check top-level first, then inside options: {}
        ollama_options = data.get("options") or {}
        for k in _OLLAMA_NATIVE_PARAMS:
            v = data.get(k) if data.get(k) is not None else ollama_options.get(k)
            if v is not None:
                params[k] = v
        if params:
            metadata["inference_params"] = params

        system_prompt = _extract_system_prompt(messages)
        if system_prompt:
            metadata["system_prompt"] = system_prompt
        has_mm, mm_count = _detect_multimodal(messages)
        if has_mm:
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
        # Ollama's OpenAI-compat endpoint mirrors the same path
        url = f"{self._base_url}{original.url.path}"
        if original.url.query:
            url += f"?{original.url.query}"
        skip = {"origin", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "host"}
        headers = {k: v for k, v in original.headers.items() if k.lower() not in skip}
        # Strip content-length so httpx recomputes it from the actual (possibly modified) body.
        headers.pop("content-length", None)
        if self._api_key:
            headers.setdefault("Authorization", f"Bearer {self._api_key}")
        return httpx.Request(
            method=original.method,
            url=url,
            headers=headers,
            content=call.raw_body,
        )

    def parse_response(self, response: httpx.Response) -> ModelResponse:
        try:
            data = response.json()
        except Exception:
            return ModelResponse(content="", usage=None, raw_body=response.content)

        content, tool_interactions, has_pending = _parse_chat_completions_choice(data)

        thinking_content: str | None = None
        if self._thinking_strip_enabled:
            # Ollama OpenAI-compat may natively separate reasoning into a 'reasoning' field.
            # Fall back to strip_thinking_tokens for older Ollama versions or embedded <think> tags.
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            native_reasoning = msg.get("reasoning") if isinstance(msg, dict) else None
            if native_reasoning:
                thinking_content = native_reasoning
            elif content:
                content, thinking_content = strip_thinking_tokens(content)

        return ModelResponse(
            content=content,
            usage=data.get("usage"),
            raw_body=response.content,
            provider_request_id=data.get("id"),
            tool_interactions=tool_interactions if tool_interactions else None,
            has_pending_tool_calls=has_pending,
            thinking_content=thinking_content,
            # model_hash is set later by the orchestrator after /api/show
        )

    def parse_streamed_response(self, chunks: list[bytes]) -> ModelResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
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
                # Collect native reasoning deltas (Ollama OpenAI-compat).
                try:
                    obj = json.loads(payload)
                    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                    rpart = delta.get("reasoning")
                    if rpart:
                        reasoning_parts.append(rpart)
                except (json.JSONDecodeError, IndexError, TypeError):
                    pass

        tool_interactions = _build_interactions_from_map(tool_call_map)
        joined = "".join(content_parts)
        thinking_content: str | None = None
        if self._thinking_strip_enabled:
            # Prefer native reasoning deltas; fall back to tag stripping.
            native_reasoning = "".join(reasoning_parts) if reasoning_parts else None
            if native_reasoning:
                thinking_content = native_reasoning
            elif joined:
                joined, thinking_content = strip_thinking_tokens(joined)

        return ModelResponse(
            content=joined,
            usage=None,
            raw_body=b"".join(chunks),
            provider_request_id=state["provider_request_id"],
            tool_interactions=tool_interactions if tool_interactions else None,
            has_pending_tool_calls=state["has_pending_tool_calls"],
            thinking_content=thinking_content,
        )

    def build_tool_result_call(
        self,
        original_call: ModelCall,
        tool_calls: list[ToolInteraction],
        tool_results: list[dict],
    ) -> ModelCall:
        """Append assistant tool_calls + tool result messages (OpenAI-compat format).

        Ollama uses the same multi-turn tool format as OpenAI Chat Completions.
        prompt_text is preserved so the audit hash covers the original user intent.
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
        new_raw_body = json.dumps(body).encode("utf-8")
        return ModelCall(
            provider=original_call.provider,
            model_id=original_call.model_id,
            prompt_text=original_call.prompt_text,
            raw_body=new_raw_body,
            is_streaming=original_call.is_streaming,
            metadata=original_call.metadata,
        )

    async def fetch_model_hash(
        self, model_name: str, client: httpx.AsyncClient | None = None
    ) -> str | None:
        """Return the Ollama model digest, using a TTL-aware instance cache.

        Cache TTL is self._digest_cache_ttl seconds (default 1800).
        Set digest_cache_ttl=0 at construction to always fetch fresh.
        """
        now = time.monotonic()
        if self._digest_cache_ttl > 0:
            cached = self._digest_cache.get(model_name)
            if cached and (now - cached[1]) < self._digest_cache_ttl:
                return cached[0]
        digest = await _fetch_model_digest_raw(self._base_url, model_name, client)
        if digest and self._digest_cache_ttl > 0:
            self._digest_cache[model_name] = (digest, now)
        return digest
