"""Completeness invariant middleware: record every request in gateway_attempts."""

from __future__ import annotations

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


async def completeness_middleware(request: Request, call_next) -> Response:
    """Run first: set request_id and default disposition. In finally: write one gateway_attempts row."""
    # Skip completeness tracking for non-proxy routes (health, metrics, lineage dashboard).
    if request.url.path in ("/", "/health", "/metrics", "/v1/models") or request.url.path.startswith(("/lineage", "/v1/lineage", "/v1/control", "/v1/attestation-proofs", "/v1/policies")):
        return await call_next(request)
    rid = new_request_id()
    response: Response | None = None  # set only on success; None if call_next raises
    try:
        response = await call_next(request)
        return response
    finally:
        settings = get_settings()
        ctx = get_pipeline_context()
        if settings.completeness_enabled and (ctx.wal_writer or ctx.walacor_client):
            # Prefer request.state values: BaseHTTPMiddleware runs call_next in a separate
            # anyio task, so ContextVar mutations in the handler are not visible here.
            disposition = getattr(request.state, "walacor_disposition", disposition_var.get())
            status_code = response.status_code if response is not None else 500
            tenant_id = settings.gateway_tenant_id or ""
            provider = getattr(request.state, "walacor_provider", provider_var.get())
            model_id = getattr(request.state, "walacor_model_id", model_id_var.get())
            execution_id = getattr(request.state, "walacor_execution_id", execution_id_var.get())
            user_id = getattr(request.state, "walacor_user_id", None)
            try:
                if ctx.walacor_client:
                    await ctx.walacor_client.write_attempt(
                        request_id=rid,
                        tenant_id=tenant_id,
                        path=request.url.path,
                        disposition=disposition,
                        status_code=status_code,
                        provider=provider,
                        model_id=model_id,
                        execution_id=execution_id,
                        user=user_id,
                    )
                if ctx.wal_writer:
                    ctx.wal_writer.write_attempt(
                        request_id=rid,
                        tenant_id=tenant_id,
                        path=request.url.path,
                        disposition=disposition,
                        status_code=status_code,
                        provider=provider,
                        model_id=model_id,
                        execution_id=execution_id,
                        user=user_id,
                    )
                gateway_attempts_total.labels(disposition=disposition).inc()
            except Exception as e:
                logger.warning("Failed to write gateway_attempt: %s", e)
