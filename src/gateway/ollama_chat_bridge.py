"""Ollama-shape /api/chat + /api/generate handlers that route through the
gateway's normal /v1/chat/completions pipeline.

Why this exists
---------------
OpenWebUI, when configured as an *Ollama* connection, sends:
  - GET  /api/tags       → already handled (ollama_proxy.py)
  - POST /api/show       → already handled (ollama_proxy.py)
  - POST /api/chat       → unhandled before this module → Starlette 404
  - POST /api/generate   → unhandled before this module → Starlette 404

Without /api/chat, a Claude or OpenAI model selected in OpenWebUI's
picker (when the gateway is registered as an Ollama connection) results
in a request OpenWebUI's client never gets a usable response for, so
its UI shows a perpetual spinner with no error toast. The fix is to
translate Ollama-shape requests into OpenAI-shape and re-enter the
normal pipeline (which already routes to OpenAI / Anthropic / Ollama
adapters off the body ``model`` field), then translate the response
back into Ollama-shape (JSON or NDJSON for stream=true).

Design choices
--------------
1. **Re-enter the orchestrator, don't duplicate it.** We do NOT
   reimplement attestation/policy/budget/chain here. The translator
   builds a new ``Request`` with the OpenAI body and path, then calls
   ``handle_request`` — the orchestrator does every governance check
   exactly once, and the WAL/Walacor records identify the model
   correctly regardless of which OWUI connection surfaced the request.
2. **Stream translation is line-oriented.** OpenAI SSE chunks (``data: {...}``)
   become Ollama NDJSON (``{"message": {"content": "..."}, "done": false}``),
   with a final ``done: true`` line carrying token counts when present.
3. **Path matters for /api/* middleware exemption — we keep it.**
   ``main.py``'s ``api_key_middleware._plugin_paths`` exempts ``/api/``
   so OpenWebUI's Ollama plugin can talk without an X-API-Key header.
   The synthetic Request we hand to ``handle_request`` keeps its
   inbound headers but rewrites scope path to ``/v1/chat/completions``
   so downstream code (which often reads ``request.url.path``) sees
   the OpenAI path it expects.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger(__name__)


# ─── Request translation ────────────────────────────────────────────────


def _ollama_chat_to_openai(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate Ollama /api/chat body → OpenAI /v1/chat/completions body.

    Ollama:    {"model","messages":[{"role","content","images"?}],
                "stream","options":{"temperature","top_p","num_predict",...},
                "format"?,"keep_alive"?}
    OpenAI:    {"model","messages","stream","temperature","top_p","max_tokens",
                "response_format"?}
    """
    out: dict[str, Any] = {
        "model": payload.get("model", ""),
        "messages": payload.get("messages") or [],
        # Default stream=true matches Ollama's behavior — OpenWebUI sets it
        # explicitly, but a missing field still means "stream" upstream.
        "stream": bool(payload.get("stream", True)),
    }
    opts = payload.get("options") or {}
    # Ollama groups sampling params under "options"; OpenAI hoists them.
    if "temperature" in opts:
        out["temperature"] = opts["temperature"]
    if "top_p" in opts:
        out["top_p"] = opts["top_p"]
    if "num_predict" in opts and opts["num_predict"] not in (None, -1):
        out["max_tokens"] = opts["num_predict"]
    if "stop" in opts:
        out["stop"] = opts["stop"]
    # Ollama "format": "json" → OpenAI response_format
    if payload.get("format") == "json":
        out["response_format"] = {"type": "json_object"}
    # Forward tools if OWUI passed them (it does when "Tools" toggle is on).
    if "tools" in payload:
        out["tools"] = payload["tools"]
    return out


def _ollama_generate_to_openai(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate Ollama /api/generate (single-prompt completion) → OpenAI chat.

    /api/generate is the legacy completion endpoint. We model the prompt
    as a single user message. Some clients also pass ``system``; we surface
    it as a system message when present.
    """
    msgs: list[dict[str, Any]] = []
    if payload.get("system"):
        msgs.append({"role": "system", "content": payload["system"]})
    if payload.get("prompt"):
        msgs.append({"role": "user", "content": payload["prompt"]})
    return _ollama_chat_to_openai({**payload, "messages": msgs})


# ─── Response translation ───────────────────────────────────────────────


def _now_iso() -> str:
    # Ollama timestamps are RFC3339Nano; second precision is fine for OWUI.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _openai_json_to_ollama(model: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Translate one non-streaming OpenAI completion → Ollama /api/chat shape."""
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = body.get("usage") or {}
    return {
        "model": model,
        "created_at": _now_iso(),
        "message": {
            "role": msg.get("role", "assistant"),
            "content": msg.get("content", ""),
        },
        "done": True,
        "done_reason": choice.get("finish_reason") or "stop",
        "prompt_eval_count": usage.get("prompt_tokens", 0),
        "eval_count": usage.get("completion_tokens", 0),
    }


async def _translate_sse_to_ndjson(
    upstream: AsyncIterator[bytes],
    model: str,
) -> AsyncIterator[bytes]:
    """Convert an OpenAI SSE byte stream into Ollama NDJSON byte stream.

    The OpenAI stream is a sequence of ``data: {json}\\n\\n`` lines,
    terminated by ``data: [DONE]\\n\\n``. Each ``delta.content`` becomes
    one Ollama NDJSON object; the final ``[DONE]`` becomes a single
    ``{"done": true, ...}`` line. Other event types (tool_calls, role
    deltas) are silently skipped — Ollama's NDJSON has no analog and
    OpenWebUI's Ollama client doesn't display them anyway.
    """
    buf = bytearray()
    last_finish_reason = "stop"
    async for chunk in upstream:
        if not chunk:
            continue
        buf.extend(chunk)
        # SSE events are separated by blank lines (\n\n). Process whole
        # events; keep any partial trailing event in the buffer.
        while True:
            i = buf.find(b"\n\n")
            if i < 0:
                break
            event = bytes(buf[:i])
            del buf[: i + 2]
            for line in event.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    yield (
                        json.dumps(
                            {
                                "model": model,
                                "created_at": _now_iso(),
                                "message": {"role": "assistant", "content": ""},
                                "done": True,
                                "done_reason": last_finish_reason,
                            }
                        )
                        + "\n"
                    ).encode()
                    return
                try:
                    obj = json.loads(payload)
                except Exception:  # malformed SSE chunk — skip, don't crash
                    continue
                choice = (obj.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if choice.get("finish_reason"):
                    last_finish_reason = choice["finish_reason"]
                content = delta.get("content")
                if not content:
                    continue
                yield (
                    json.dumps(
                        {
                            "model": model,
                            "created_at": _now_iso(),
                            "message": {"role": "assistant", "content": content},
                            "done": False,
                        }
                    )
                    + "\n"
                ).encode()
    # If upstream closed without [DONE], emit a terminator so OWUI doesn't
    # spin forever waiting for one — mirroring the gateway's own resilience
    # to half-closed SSE.
    yield (
        json.dumps(
            {
                "model": model,
                "created_at": _now_iso(),
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": last_finish_reason,
            }
        )
        + "\n"
    ).encode()


# ─── Synthetic-request plumbing ────────────────────────────────────────


def _rewrite_request(request: Request, new_body: bytes, new_path: str) -> Request:
    """Build a Request that looks like a POST to ``new_path`` with ``new_body``.

    We keep the original ASGI scope (so middleware-attached attrs survive),
    rewrite ``scope["path"]`` and ``scope["raw_path"]``, and pre-populate
    ``request._body`` so the orchestrator's ``await request.body()`` /
    ``await request.json()`` calls return the rewritten payload without
    trying to re-read the (already-consumed) ASGI receive channel.
    """
    new_scope = dict(request.scope)
    new_scope["path"] = new_path
    new_scope["raw_path"] = new_path.encode()
    # Update Content-Length so downstream code that trusts headers sees the
    # new body length. Headers are a list of (bytes, bytes) tuples.
    headers = []
    cl_set = False
    for k, v in new_scope.get("headers", []):
        if k.lower() == b"content-length":
            headers.append((k, str(len(new_body)).encode()))
            cl_set = True
        else:
            headers.append((k, v))
    if not cl_set:
        headers.append((b"content-length", str(len(new_body)).encode()))
    new_scope["headers"] = headers

    async def _receive():
        return {"type": "http.request", "body": new_body, "more_body": False}

    rewritten = Request(new_scope, receive=_receive)
    # Pre-cache the body so .body()/.json() short-circuit.
    rewritten._body = new_body  # type: ignore[attr-defined]
    return rewritten


# ─── Public handlers ───────────────────────────────────────────────────


async def _handle_ollama_request(
    request: Request, translator
) -> Response:
    """Shared implementation for /api/chat and /api/generate."""
    try:
        raw = await request.body()
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        return JSONResponse(
            {"error": f"invalid JSON: {exc}"}, status_code=400
        )

    openai_body = translator(payload)
    model = openai_body.get("model", "")
    stream = bool(openai_body.get("stream", True))
    new_body = json.dumps(openai_body).encode()

    synthetic = _rewrite_request(request, new_body, "/v1/chat/completions")

    # Late import: handle_request lives in orchestrator and pulls in heavy
    # modules; deferring keeps this file cheap to import at startup.
    from gateway.pipeline.orchestrator import handle_request

    inner = await handle_request(synthetic)

    # ── Non-streaming JSON → Ollama JSON ────────────────────────────
    if not stream or not isinstance(inner, StreamingResponse):
        # Read whatever Response we got; if it's a JSONResponse with an
        # OpenAI-shaped body, translate. Anything else (errors, 4xx/5xx)
        # passes through unchanged so OWUI sees the upstream's reason.
        body_bytes = getattr(inner, "body", b"") or b""
        try:
            decoded = json.loads(body_bytes)
        except Exception:
            return inner
        if inner.status_code >= 400 or "choices" not in decoded:
            # Pass error responses through verbatim — OWUI displays them.
            return inner
        return JSONResponse(_openai_json_to_ollama(model, decoded))

    # ── Streaming SSE → Ollama NDJSON ───────────────────────────────
    return StreamingResponse(
        _translate_sse_to_ndjson(inner.body_iterator, model),
        status_code=inner.status_code,
        media_type="application/x-ndjson",
        background=inner.background,
    )


async def ollama_api_chat(request: Request) -> Response:
    """POST /api/chat — Ollama-shape chat endpoint, fronts /v1/chat/completions."""
    return await _handle_ollama_request(request, _ollama_chat_to_openai)


async def ollama_api_generate(request: Request) -> Response:
    """POST /api/generate — Ollama-shape completion endpoint."""
    return await _handle_ollama_request(request, _ollama_generate_to_openai)
