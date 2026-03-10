"""Step 3: Forward request to provider and optionally stream with tee."""

from __future__ import annotations

import logging
import time
import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter
from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context
from gateway.metrics.prometheus import forward_duration

logger = logging.getLogger(__name__)


def build_governance_sse_event(execution_id=None, attestation_id=None, chain_seq=None, policy_result=None):
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
    return f"event: governance\ndata: {_json.dumps(payload)}\n\n".encode()


def _http_client() -> httpx.AsyncClient:
    """Shared client when governance on; otherwise a one-off client."""
    ctx = get_pipeline_context()
    if ctx.http_client is not None:
        return ctx.http_client
    return httpx.AsyncClient(timeout=60.0)


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
    resp_headers = dict(upstream_resp.headers)
    resp_headers["X-Session-Id"] = call.metadata.get("session_id", "")
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

    stream_kwargs = dict(
        method=upstream_req.method,
        url=str(upstream_req.url),
        headers=upstream_req.headers,
        content=upstream_req.content,
    )

    if shared:
        upstream_ctx = client.stream(**stream_kwargs)
        # Allocate a placeholder so the closure references the same name in both branches
        _owned_client: httpx.AsyncClient | None = None
    else:
        # One-off client: keep it alive for the generator's lifetime.
        _owned_client = httpx.AsyncClient(timeout=60.0)
        upstream_ctx = _owned_client.stream(**stream_kwargs)

    # Eagerly open the upstream connection to capture the status_code.
    upstream = await upstream_ctx.__aenter__()
    actual_status = upstream.status_code

    async def generate():
        buffer_size = 0
        _exc: BaseException | None = None
        try:
            async for chunk in upstream.aiter_bytes():
                if buffer_size < max_buffer:
                    buffer.append(chunk)
                    buffer_size += len(chunk)
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
            "X-Session-Id": call.metadata.get("session_id", ""),
        },
        background=None,  # task runs in generate()'s finally — see above
    ), buffer
