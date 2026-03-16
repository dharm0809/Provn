"""Generic adapter: configurable JSON paths + format auto-detection for custom/on-prem APIs.

Phase 16 additions:
  - Auto-detects OpenAI-compat (messages/choices), HuggingFace (inputs/generated_text),
    and Ollama-native REST (prompt/response) formats.
  - When a known format is detected, extracts inference params, system prompts, and
    multimodal flags for richer audit metadata — matching Ollama/OpenAI fidelity.
  - Manual WALACOR_GENERIC_*_PATH env vars remain fully supported as overrides.
  - Set WALACOR_GENERIC_AUTO_DETECT=false to always use configured paths (legacy mode).
"""

from __future__ import annotations

from typing import Any

import gateway.util.json_utils as json

import httpx
from starlette.requests import Request

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter
from gateway.adapters.openai import (
    _build_interactions_from_map,
    _concat_messages,
    _detect_multimodal,
    _extract_inference_params,
    _extract_system_prompt,
    _parse_chat_completions_choice,
    _process_sse_line,
)
from gateway.config import get_settings
from gateway.util.session_id import resolve_session_id


def _json_path(obj: Any, path: str) -> Any:
    """Resolve simple JSON path: $.key1.key2 or $.key. Supports one [*] for array concat."""
    if path.startswith("$."):
        path = path[2:]
    parts = path.split(".")
    current = obj
    for i, p in enumerate(parts):
        if not p:
            continue
        if p == "*" and isinstance(current, list):
            rest = ".".join(parts[i + 1:])
            if rest:
                return " ".join(str(_json_path(x, rest)) for x in current)
            return " ".join(str(x) for x in current)
        if isinstance(current, list):
            return " ".join(str(_json_path(x, ".".join(parts[i:])) if parts[i:] else x) for x in current)
        current = current.get(p) if isinstance(current, dict) else None
    return current


def _detect_request_format(data: dict) -> str:
    """Detect common request schema. Returns 'openai_messages', 'huggingface', 'openai_legacy', or 'unknown'."""
    if "messages" in data:
        return "openai_messages"
    if "inputs" in data:
        return "huggingface"
    if "prompt" in data:
        return "openai_legacy"
    return "unknown"


def _detect_response_format(data: Any) -> str:
    """Detect common response schema. Returns 'openai', 'huggingface', 'ollama_native', or 'unknown'."""
    # HuggingFace batch returns a list of dicts
    d = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else data
    if isinstance(d, dict):
        if "choices" in d:
            return "openai"
        if "generated_text" in d:
            return "huggingface"
        if "response" in d:
            return "ollama_native"
    return "unknown"


class GenericAdapter(ProviderAdapter):
    """Configurable adapter via env: WALACOR_GENERIC_MODEL_PATH, WALACOR_GENERIC_PROMPT_PATH, etc.

    With auto_detect=True (default), the adapter inspects each request/response and uses
    rich parsing for known formats (OpenAI-compat, HuggingFace, Ollama-native REST).
    Unknown formats fall back to the configured JSON path extraction.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model_path: str = "$.model",
        prompt_path: str = "$.messages[*].content",
        response_path: str = "$.choices[0].message.content",
        auto_detect: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_path = model_path
        self._prompt_path = prompt_path
        self._response_path = response_path
        self._auto_detect = auto_detect

    def get_provider_name(self) -> str:
        return "generic"

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

        model_id = str(_json_path(data, self._model_path) or "")
        metadata: dict[str, Any] = {}
        if request.headers.get("x-user-id"):
            metadata["user"] = request.headers["x-user-id"]
        metadata["session_id"] = resolve_session_id(request, get_settings().session_header_names_list)

        fmt = _detect_request_format(data) if self._auto_detect else "unknown"

        if fmt == "openai_messages":
            messages = data.get("messages", [])
            prompt_text = _concat_messages(messages)
            params = _extract_inference_params(data)
            if params:
                metadata["inference_params"] = params
            sp = _extract_system_prompt(messages)
            if sp:
                metadata["system_prompt"] = sp
            has_mm, mm_count = _detect_multimodal(messages)
            if has_mm:
                metadata["has_multimodal_input"] = True
                metadata["multimodal_input_count"] = mm_count
        elif fmt == "huggingface":
            inputs = data.get("inputs", "")
            prompt_text = " ".join(inputs) if isinstance(inputs, list) else str(inputs)
        elif fmt == "openai_legacy":
            prompt_text = data.get("prompt") or ""
            params = _extract_inference_params(data)
            if params:
                metadata["inference_params"] = params
        else:
            prompt_val = _json_path(data, self._prompt_path)
            prompt_text = " ".join(prompt_val) if isinstance(prompt_val, list) else str(prompt_val or "")

        return ModelCall(
            provider=self.get_provider_name(),
            model_id=model_id,
            prompt_text=prompt_text,
            raw_body=body_bytes,
            is_streaming=bool(data.get("stream", False)),
            metadata=metadata,
        )

    async def build_forward_request(self, call: ModelCall, original: Request) -> httpx.Request:
        url = f"{self._base_url}{original.url.path}"
        if original.url.query:
            url += f"?{original.url.query}"
        headers = dict(original.headers)
        if self._api_key:
            headers.setdefault("Authorization", f"Bearer {self._api_key}")
        return httpx.Request(method=original.method, url=url, headers=headers, content=call.raw_body)

    def parse_response(self, response: httpx.Response) -> ModelResponse:
        try:
            data = response.json()
        except Exception:
            return ModelResponse(content="", usage=None, raw_body=response.content)

        if self._auto_detect:
            fmt = _detect_response_format(data)
            if fmt == "openai":
                content, tool_interactions, has_pending = _parse_chat_completions_choice(data)
                return ModelResponse(
                    content=content,
                    usage=data.get("usage"),
                    raw_body=response.content,
                    provider_request_id=data.get("id"),
                    tool_interactions=tool_interactions if tool_interactions else None,
                    has_pending_tool_calls=has_pending,
                )
            if fmt == "huggingface":
                d = data[0] if isinstance(data, list) else data
                return ModelResponse(
                    content=d.get("generated_text") or "",
                    usage=None,
                    raw_body=response.content,
                )
            if fmt == "ollama_native":
                return ModelResponse(
                    content=data.get("response") or "",
                    usage=data.get("usage"),
                    raw_body=response.content,
                    provider_request_id=data.get("id"),
                )

        # Fallback: configured path
        content = str(_json_path(data, self._response_path) or "")
        return ModelResponse(
            content=content,
            usage=data.get("usage"),
            raw_body=response.content,
            provider_request_id=data.get("id"),
        )

    def parse_streamed_response(self, chunks: list[bytes]) -> ModelResponse:
        detected_fmt: str | None = None
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
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if detected_fmt is None and self._auto_detect:
                    detected_fmt = _detect_response_format(obj)

                if detected_fmt == "openai":
                    _process_sse_line(payload, content_parts, tool_call_map, state)
                elif detected_fmt == "huggingface":
                    token = obj.get("token") or {}
                    text = token.get("text") or obj.get("generated_text") or ""
                    if text:
                        content_parts.append(str(text))
                    if state["provider_request_id"] is None:
                        state["provider_request_id"] = obj.get("id")
                else:
                    part = _json_path(obj, self._response_path)
                    if part:
                        content_parts.append(str(part))
                    if state["provider_request_id"] is None:
                        state["provider_request_id"] = obj.get("id")

        tool_interactions = _build_interactions_from_map(tool_call_map) if detected_fmt == "openai" else None
        return ModelResponse(
            content="".join(content_parts),
            usage=None,
            raw_body=b"".join(chunks),
            provider_request_id=state["provider_request_id"],
            tool_interactions=tool_interactions if tool_interactions else None,
            has_pending_tool_calls=state["has_pending_tool_calls"],
        )
