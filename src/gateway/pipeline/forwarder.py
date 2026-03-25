"""Step 3: Forward request to provider and optionally stream with tee."""

from __future__ import annotations

import asyncio
import json as _json_mod
import logging
import time
import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter
from gateway.config import get_settings
from gateway.content.stream_safety import check_stream_pii, check_stream_safety
from gateway.pipeline.context import get_pipeline_context
from gateway.metrics.prometheus import forward_duration

logger = logging.getLogger(__name__)


def _normalize_responses_to_chat_completions(
    raw_bytes: bytes, model_response: ModelResponse,
) -> bytes:
    """Convert OpenAI Responses API JSON → Chat Completions JSON for client compat."""
    try:
        data = _json_mod.loads(raw_bytes)
    except (ValueError, TypeError):
        return raw_bytes

    # Map Responses API usage (input_tokens/output_tokens) to Chat Completions format.
    raw_usage = data.get("usage") or {}
    usage = {
        "prompt_tokens": raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0)),
        "completion_tokens": raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0)),
        "total_tokens": raw_usage.get("total_tokens", 0),
    }
    # Preserve detail fields.
    if raw_usage.get("output_tokens_details"):
        usage["completion_tokens_details"] = raw_usage["output_tokens_details"]
    if raw_usage.get("input_tokens_details"):
        usage["prompt_tokens_details"] = raw_usage["input_tokens_details"]

    chat_completions = {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": data.get("created_at", 0),
        "model": data.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": model_response.content or "",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
    # Include service_tier if present.
    if data.get("service_tier"):
        chat_completions["service_tier"] = data["service_tier"]

    return _json_mod.dumps(chat_completions).encode("utf-8")


def _inject_stream_options(content: bytes | None) -> bytes | None:
    """Inject stream_options.include_usage=true into streaming request bodies.

    OpenAI-compatible APIs (including Ollama) only return token usage in
    the final SSE chunk when this option is set.
    """
    if not content:
        return content
    try:
        body = _json_mod.loads(content)
        if isinstance(body, dict) and body.get("stream") and "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}
            return _json_mod.dumps(body).encode()
    except (ValueError, TypeError):
        pass
    return content


def build_governance_sse_event(
    execution_id=None, attestation_id=None, chain_seq=None,
    policy_result=None, content_analysis=None, budget_remaining=None,
    budget_percent=None, model_id=None,
):
    """Build an SSE event with governance metadata, sent after data: [DONE]."""
    import json as _json
    payload = {}
    if execution_id:
        payload["execution_id"] = execution_id
    if attestation_id:
        payload["attestation_id"] = attestation_id
    if chain_seq is not None:
        payload["chain_seq"] = chain_seq
    if policy_result:
        payload["policy_result"] = policy_result
    if content_analysis:
        payload["content_analysis"] = content_analysis
    if budget_remaining is not None:
        payload["budget_remaining"] = budget_remaining
    if budget_percent is not None:
        payload["budget_percent"] = budget_percent
    if model_id:
        payload["model_id"] = model_id
    return f"event: governance\ndata: {_json.dumps(payload)}\n\n".encode()


async def sse_keepalive_generator(interval_seconds: float | None = None):
    if interval_seconds is None:
        interval_seconds = get_settings().sse_keepalive_interval
    """Yield SSE comment keepalives at a regular interval."""
    while True:
        await asyncio.sleep(interval_seconds)
        yield b": keepalive\n\n"


def _http_client() -> httpx.AsyncClient:
    """Shared client when governance on; otherwise a one-off client."""
    ctx = get_pipeline_context()
    if ctx.http_client is not None:
        return ctx.http_client
    settings = get_settings()
    return httpx.AsyncClient(timeout=settings.provider_timeout)


async def forward(
    adapter: ProviderAdapter,
    call: ModelCall,
    request: Request,
) -> tuple[Response, ModelResponse]:
    """Forward non-streaming request; return response and parsed ModelResponse."""
    upstream_req = await adapter.build_forward_request(call, request)
    prompt_id = call.metadata.get("prompt_id")
    if prompt_id:
        upstream_req.headers["X-Walacor-Prompt-ID"] = prompt_id
    t0 = time.perf_counter()
    client = _http_client()
    shared = client is get_pipeline_context().http_client
    if shared:
        upstream_resp = await client.send(upstream_req)
    else:
        async with client:
            upstream_resp = await client.send(upstream_req)
    forward_duration.labels(provider=adapter.get_provider_name()).observe(time.perf_counter() - t0)
    model_response = adapter.parse_response(upstream_resp)

    # Retry once if Responses API summary was rejected (org not verified).
    # The adapter already cached _reasoning_summary_available=False, so rebuild
    # will omit the summary parameter.
    if model_response.content == "__RETRY_WITHOUT_SUMMARY__":
        upstream_req = await adapter.build_forward_request(call, request)
        if shared:
            upstream_resp = await client.send(upstream_req)
        else:
            upstream_resp = await httpx.AsyncClient(
                timeout=httpx.Timeout(300),
            ).send(upstream_req)
        model_response = adapter.parse_response(upstream_resp)

    resp_headers = dict(upstream_resp.headers)
    # Remove hop-by-hop headers that conflict with Starlette's own framing.
    # Starlette sets content-length from the body; keeping transfer-encoding
    # from upstream causes "Content-Length can't be present with Transfer-Encoding".
    resp_headers.pop("transfer-encoding", None)
    resp_headers.pop("content-length", None)
    resp_headers["X-Session-Id"] = call.metadata.get("session_id", "")

    # Normalize Responses API output → Chat Completions format for clients.
    if call.metadata.get("_responses_api") and upstream_resp.status_code == 200:
        normalized = _normalize_responses_to_chat_completions(
            upstream_resp.content, model_response,
        )
        response = Response(
            content=normalized,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )
    else:
        response = Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )
    return response, model_response


async def stream_with_tee(
    adapter: ProviderAdapter,
    call: ModelCall,
    request: Request,
    buffer: list[bytes] | None = None,
    background_task: BackgroundTask | None = None,
    governance_meta: dict | None = None,
) -> tuple[StreamingResponse, list[bytes]]:
    """Stream response to caller while buffering chunks (capped by max_stream_buffer_bytes). Returns (response, buffer).

    The upstream connection is opened eagerly before building StreamingResponse so
    the actual HTTP status_code (e.g. 400/401/429/500) is propagated to the caller
    instead of the previously hard-coded 200 (Finding 6).

    governance_meta: mutable dict pre-populated with attestation_id/policy_result by
    the orchestrator. The background task adds execution_id and chain_seq after write.
    After the background task completes, a governance SSE event is yielded.
    """
    upstream_req = await adapter.build_forward_request(call, request)
    prompt_id = call.metadata.get("prompt_id")
    if prompt_id:
        upstream_req.headers["X-Walacor-Prompt-ID"] = prompt_id
    if buffer is None:
        buffer = []
    settings = get_settings()
    max_buffer = settings.max_stream_buffer_bytes

    t0 = time.perf_counter()
    client = _http_client()
    shared = client is get_pipeline_context().http_client

    # Inject stream_options so provider returns token usage in final SSE chunk.
    content = _inject_stream_options(upstream_req.content)
    headers = dict(upstream_req.headers)
    if content is not upstream_req.content:
        # Body changed — drop Content-Length so httpx recomputes it.
        headers.pop("content-length", None)

    stream_kwargs = dict(
        method=upstream_req.method,
        url=str(upstream_req.url),
        headers=headers,
        content=content,
    )

    if shared:
        upstream_ctx = client.stream(**stream_kwargs)
        # Allocate a placeholder so the closure references the same name in both branches
        _owned_client: httpx.AsyncClient | None = None
    else:
        # One-off client: keep it alive for the generator's lifetime.
        _owned_client = httpx.AsyncClient(timeout=get_settings().provider_timeout)
        upstream_ctx = _owned_client.stream(**stream_kwargs)

    # Eagerly open the upstream connection to capture the status_code.
    upstream = await upstream_ctx.__aenter__()
    actual_status = upstream.status_code

    async def generate():
        buffer_size = 0
        accumulated_text = ""
        pii_checked_len = 0
        _exc: BaseException | None = None
        try:
            async for chunk in upstream.aiter_bytes():
                if buffer_size < max_buffer:
                    buffer.append(chunk)
                    buffer_size += len(chunk)
                # Mid-stream S4 safety check
                accumulated_text += chunk.decode("utf-8", errors="replace")
                if len(accumulated_text) > 4096:
                    accumulated_text = accumulated_text[-4096:]
                if check_stream_safety(accumulated_text):
                    logger.warning("S4 safety abort triggered mid-stream")
                    yield b'event: error\ndata: {"error": "content_safety", "message": "Response blocked by safety filter (S4)"}\n\n'
                    return
                # Windowed PII check — warn only (can't un-send streamed chunks)
                pii_found, pii_checked_len = check_stream_pii(accumulated_text, pii_checked_len)
                if pii_found:
                    logger.warning("PII detected in stream, logging warning")
                yield chunk
        except BaseException as e:
            _exc = e
            logger.warning(
                "Upstream stream interrupted: provider=%s error=%s",
                adapter.get_provider_name(), e, exc_info=True,
            )
            raise
        finally:
            if _exc is not None:
                await upstream_ctx.__aexit__(type(_exc), _exc, _exc.__traceback__)
            else:
                await upstream_ctx.__aexit__(None, None, None)
            if _owned_client is not None:
                await _owned_client.aclose()
            forward_duration.labels(provider=adapter.get_provider_name()).observe(time.perf_counter() - t0)
            # Run the background task here (not via StreamingResponse.background) so it
            # always executes even when the stream is interrupted before completion.
            # Starlette only calls StreamingResponse.background after normal iteration end.
            if background_task is not None:
                try:
                    await background_task()
                except Exception:
                    logger.error(
                        "Stream background task failed: provider=%s",
                        adapter.get_provider_name(), exc_info=True,
                    )

        # Phase 23: yield governance SSE event after stream + background task complete.
        # governance_meta is populated by the background task with execution_id/chain_seq.
        if governance_meta is not None:
            try:
                yield build_governance_sse_event(**governance_meta)
            except Exception:
                logger.debug("Failed to yield governance SSE event", exc_info=True)

    return StreamingResponse(
        generate(),
        status_code=actual_status,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": call.metadata.get("session_id", ""),
        },
        background=None,  # task runs in generate()'s finally — see above
    ), buffer
