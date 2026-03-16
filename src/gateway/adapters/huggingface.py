"""HuggingFace adapter: TGI /generate and Inference Endpoints chat style."""

from __future__ import annotations

from typing import Any

import gateway.util.json_utils as json

import httpx
from starlette.requests import Request

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter


class HuggingFaceAdapter(ProviderAdapter):
    """Adapter for HuggingFace TGI or Inference Endpoints (OpenAI-compatible or /generate)."""

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def get_provider_name(self) -> str:
        return "huggingface"

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
        prompt_text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content", ""))
            for m in messages
        )
        if not prompt_text and "inputs" in data:
            prompt_text = str(data.get("inputs", ""))
        is_streaming = data.get("stream", False)
        return ModelCall(
            provider=self.get_provider_name(),
            model_id=model_id,
            prompt_text=prompt_text,
            raw_body=body_bytes,
            is_streaming=is_streaming,
            metadata={},
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
        content = ""
        if "choices" in data and data["choices"]:
            content = (data["choices"][0].get("message") or {}).get("content") or (data["choices"][0].get("text") or "")
        elif "generated_text" in data:
            content = data["generated_text"]
        return ModelResponse(
            content=content,
            usage=data.get("usage"),
            raw_body=response.content,
            provider_request_id=data.get("id"),
        )

    def parse_streamed_response(self, chunks: list[bytes]) -> ModelResponse:
        content_parts = []
        provider_request_id = None
        usage = None
        for chunk in chunks:
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                if line.startswith("data: "):
                    try:
                        obj = json.loads(line[6:].strip())
                        if provider_request_id is None:
                            provider_request_id = obj.get("id")
                        if "choices" in obj and obj["choices"]:
                            delta = (obj["choices"][0].get("delta") or {}).get("content") or ""
                            if delta:
                                content_parts.append(delta)
                        elif "token" in obj and "text" in obj["token"]:
                            content_parts.append(obj["token"]["text"])
                        if obj.get("usage"):
                            usage = obj["usage"]
                    except json.JSONDecodeError:
                        pass
        return ModelResponse(
            content="".join(content_parts),
            usage=usage,
            raw_body=b"".join(chunks),
            provider_request_id=provider_request_id,
        )
