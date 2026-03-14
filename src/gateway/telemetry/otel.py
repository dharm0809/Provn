"""Phase 17: OpenTelemetry GenAI span export.

Optional dependency — imports are guarded so the gateway runs without the OTel SDK.
Install with: pip install 'walacor-gateway[telemetry]'

Design: single retroactive span per request, emitted at record-write time.
No distributed context propagation — the tamper-proof audit trail is the primary record.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)


def init_tracer(
    service_name: str,
    endpoint: str,
    timeout_ms: int = 5000,
) -> Any | None:
    """Initialise an OTel TracerProvider with OTLP gRPC exporter.

    Returns a tracer object on success, or None if the OTel SDK is not installed.
    Fail-open: errors during init are logged and None is returned.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    except ImportError:
        logger.info(
            "OTel SDK not installed — telemetry disabled. "
            "Install with: pip install 'walacor-gateway[telemetry]'"
        )
        return None

    try:
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            timeout=timeout_ms // 1000,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer(service_name)
        logger.info("OTel tracer initialised: service=%s endpoint=%s", service_name, endpoint)
        return tracer
    except Exception:
        logger.warning("OTel init failed (fail-open) — telemetry disabled", exc_info=True)
        return None


@asynccontextmanager
async def trace_span(tracer: Any, name: str, attributes: dict[str, Any] | None = None):
    """Create a child span for a pipeline step.  No-op if *tracer* is None.

    Usage::

        async with trace_span(ctx.tracer, "parse_request", {"model": model_id}) as span:
            ...

    The span is automatically ended when the block exits.  If an exception
    occurs the span is annotated with ``error=True`` and ``error.message``
    before being re-raised.

    Fail-open: if the OTel SDK is not installed the block executes normally
    and ``span`` is ``None``.
    """
    if tracer is None:
        yield None
        return
    try:
        from opentelemetry import trace as otrace

        span = tracer.start_span(name, kind=otrace.SpanKind.INTERNAL)
        if attributes:
            span.set_attributes(attributes)
        try:
            yield span
        except Exception as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            raise
        finally:
            span.end()
    except ImportError:
        yield None


def emit_inference_span(
    tracer: Any,
    *,
    provider: str,
    model_id: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    execution_id: str = "",
    policy_result: str = "",
    tenant_id: str = "",
    session_id: str | None = None,
    tool_count: int = 0,
    has_thinking: bool = False,
    provider_request_id: str | None = None,
    request_start_ns: int | None = None,
) -> None:
    """Create and immediately close a GenAI span with inference metadata.

    Uses GenAI semantic conventions (OTel v1.37+) plus Walacor-specific attributes.
    Fail-open: any exception is caught and logged at DEBUG level.
    """
    try:
        from opentelemetry.trace import SpanKind

        now_ns = time.time_ns()
        start_ns = request_start_ns if request_start_ns is not None else now_ns

        span = tracer.start_span(
            "gen_ai.chat",
            start_time=start_ns,
            kind=SpanKind.CLIENT,
        )
        attributes: dict[str, Any] = {
            "gen_ai.system": provider,
            "gen_ai.request.model": model_id,
            "gen_ai.usage.input_tokens": prompt_tokens,
            "gen_ai.usage.output_tokens": completion_tokens,
            "walacor.execution_id": execution_id,
            "walacor.policy_result": policy_result,
            "walacor.tenant_id": tenant_id,
            "walacor.tool_count": tool_count,
            "walacor.has_thinking": has_thinking,
        }
        if provider_request_id:
            attributes["gen_ai.response.id"] = provider_request_id
        if session_id:
            attributes["walacor.session_id"] = session_id

        span.set_attributes(attributes)
        span.end(end_time=now_ns)
    except Exception:
        logger.debug("OTel emit_inference_span failed (fail-open)", exc_info=True)
