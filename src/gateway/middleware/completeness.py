"""Completeness invariant middleware: record every request in gateway_attempts."""

from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import Response

from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context
from gateway.util.request_context import (
    new_request_id,
    disposition_var,
    execution_id_var,
    provider_var,
    model_id_var,
)
from gateway.metrics.prometheus import gateway_attempts_total

logger = logging.getLogger(__name__)

# Hold strong refs to in-flight background writes so the event loop can't GC
# them mid-await. Tasks self-discard on completion.
_pending_attempt_writes: set[asyncio.Task] = set()


async def _write_attempt_bg(storage, record: dict, timeout: float) -> None:
    """Background task: write one attempt record without blocking the response.

    Completeness is a best-effort invariant (failures already log and continue).
    Running this off the request critical path removes the slow Walacor HTTP POST
    from tail latency — WAL backend already returns in microseconds via enqueue,
    and the HTTP-backed Walacor backend can take hundreds of ms under load.
    """
    try:
        await asyncio.wait_for(storage.write_attempt(record), timeout=timeout)
        disp = record.get("disposition", "unknown")
        gateway_attempts_total.labels(disposition=disp).inc()
    except asyncio.TimeoutError:
        logger.warning("write_attempt timed out after %.1fs — skipping", timeout)
    except Exception as e:
        logger.warning("Failed to write gateway_attempt: %s", e)


async def completeness_middleware(request: Request, call_next) -> Response:
    """Run first: set request_id and default disposition. In finally: write one gateway_attempts row."""
    # Skip completeness tracking for non-proxy routes (health, metrics, lineage dashboard).
    if request.url.path in ("/", "/health", "/metrics", "/v1/models") or request.url.path.startswith(("/lineage", "/v1/lineage", "/v1/control", "/v1/attestation-proofs", "/v1/policies", "/v1/compliance", "/v1/openwebui", "/v1/attachments")):
        return await call_next(request)
    rid = new_request_id()
    response: Response | None = None  # set only on success; None if call_next raises
    try:
        response = await call_next(request)
        return response
    finally:
        settings = get_settings()
        ctx = get_pipeline_context()
        if settings.completeness_enabled and ctx.storage:
            # Prefer request.state values: BaseHTTPMiddleware runs call_next in a separate
            # anyio task, so ContextVar mutations in the handler are not visible here.
            disposition = getattr(request.state, "walacor_disposition", disposition_var.get())
            status_code = response.status_code if response is not None else 500
            tenant_id = settings.gateway_tenant_id or ""
            provider = getattr(request.state, "walacor_provider", provider_var.get())
            model_id = getattr(request.state, "walacor_model_id", model_id_var.get())
            execution_id = getattr(request.state, "walacor_execution_id", execution_id_var.get())
            user_id = getattr(request.state, "walacor_user_id", None)
            reason = getattr(request.state, "walacor_reason", None)
            record = {
                "request_id": rid,
                "tenant_id": tenant_id,
                "path": request.url.path,
                "disposition": disposition,
                "status_code": status_code,
                "provider": provider,
                "model_id": model_id,
                "execution_id": execution_id,
                "user": user_id,
                "reason": reason,
            }
            # Fire-and-forget: response is already queued to the client by the
            # time this runs (we're in the `finally` after `return response`).
            # Spawning a task here moves the Walacor HTTP round-trip off the
            # request tail. Tracked in a module set so the event loop keeps
            # strong refs until completion.
            task = asyncio.create_task(
                _write_attempt_bg(ctx.storage, record, settings.completeness_timeout)
            )
            _pending_attempt_writes.add(task)
            task.add_done_callback(_pending_attempt_writes.discard)
