"""Unit tests for OpenTelemetry span emission (gateway.telemetry.otel)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gateway.telemetry.otel import emit_inference_span, init_tracer, trace_span


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ---------------------------------------------------------------------------
# init_tracer
# ---------------------------------------------------------------------------

def test_init_tracer_returns_none_without_sdk():
    """init_tracer must return None when opentelemetry is not installed."""
    with patch.dict("sys.modules", {
        "opentelemetry": None,
        "opentelemetry.sdk": None,
        "opentelemetry.sdk.resources": None,
        "opentelemetry.sdk.trace": None,
        "opentelemetry.sdk.trace.export": None,
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": None,
    }):
        result = init_tracer("walacor-gateway", "http://localhost:4317")
    assert result is None


# ---------------------------------------------------------------------------
# emit_inference_span — noop when tracer is None
# ---------------------------------------------------------------------------

def test_emit_span_when_tracer_none_is_noop():
    """emit_inference_span must not raise when tracer is None."""
    # Should not raise
    emit_inference_span(
        tracer=None,
        provider="ollama",
        model_id="qwen3:4b",
        prompt_tokens=100,
        completion_tokens=50,
        execution_id="test-id",
        policy_result="pass",
        tenant_id="test-tenant",
        session_id="sess-1",
        tool_count=0,
        has_thinking=False,
    )


# ---------------------------------------------------------------------------
# emit_inference_span — correct attributes with in-memory exporter
# ---------------------------------------------------------------------------

def test_emit_span_attributes_set_correctly():
    """Verify all expected attributes are set on the span via in-memory exporter."""
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry import trace
    except ImportError:
        pytest.skip("opentelemetry-sdk not installed")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    emit_inference_span(
        tracer=tracer,
        provider="ollama",
        model_id="qwen3:4b",
        prompt_tokens=120,
        completion_tokens=80,
        execution_id="exec-abc123",
        policy_result="pass",
        tenant_id="tenant-1",
        session_id="session-xyz",
        tool_count=2,
        has_thinking=True,
        provider_request_id="chatcmpl-999",
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    attrs = dict(span.attributes or {})

    assert attrs["gen_ai.system"] == "ollama"
    assert attrs["gen_ai.request.model"] == "qwen3:4b"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 80
    assert attrs["gen_ai.response.id"] == "chatcmpl-999"
    assert attrs["walacor.execution_id"] == "exec-abc123"
    assert attrs["walacor.policy_result"] == "pass"
    assert attrs["walacor.tenant_id"] == "tenant-1"
    assert attrs["walacor.session_id"] == "session-xyz"
    assert attrs["walacor.tool_count"] == 2
    assert attrs["walacor.has_thinking"] is True


def test_emit_span_no_session_omits_attribute():
    """session_id attribute should be absent when session_id is None."""
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError:
        pytest.skip("opentelemetry-sdk not installed")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    emit_inference_span(
        tracer=tracer,
        provider="openai",
        model_id="gpt-4o",
        session_id=None,
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert "walacor.session_id" not in attrs


# ---------------------------------------------------------------------------
# trace_span — async context manager
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_trace_span_none_tracer(anyio_backend):
    """trace_span with None tracer is a no-op."""
    async with trace_span(None, "test") as span:
        assert span is None


@pytest.mark.anyio
async def test_trace_span_creates_span(anyio_backend):
    """trace_span creates and ends a span."""
    mock_tracer = MagicMock()
    mock_span = MagicMock()
    mock_tracer.start_span.return_value = mock_span

    with patch("gateway.telemetry.otel.otrace", create=True):
        async with trace_span(mock_tracer, "test_step", {"key": "val"}) as span:
            assert span is mock_span

    mock_tracer.start_span.assert_called_once()
    mock_span.set_attributes.assert_called_once_with({"key": "val"})
    mock_span.end.assert_called_once()


@pytest.mark.anyio
async def test_trace_span_records_error(anyio_backend):
    """trace_span records errors on the span."""
    mock_tracer = MagicMock()
    mock_span = MagicMock()
    mock_tracer.start_span.return_value = mock_span

    with pytest.raises(ValueError):
        with patch("gateway.telemetry.otel.otrace", create=True):
            async with trace_span(mock_tracer, "failing_step") as span:
                raise ValueError("test error")

    mock_span.set_attribute.assert_any_call("error", True)
    mock_span.set_attribute.assert_any_call("error.message", "test error")
    mock_span.end.assert_called_once()


@pytest.mark.anyio
async def test_trace_span_no_attributes(anyio_backend):
    """trace_span works without attributes (None by default)."""
    mock_tracer = MagicMock()
    mock_span = MagicMock()
    mock_tracer.start_span.return_value = mock_span

    with patch("gateway.telemetry.otel.otrace", create=True):
        async with trace_span(mock_tracer, "plain_step") as span:
            assert span is mock_span

    mock_span.set_attributes.assert_not_called()
    mock_span.end.assert_called_once()


@pytest.mark.anyio
async def test_trace_span_import_error(anyio_backend):
    """trace_span yields None when OTel SDK is not installed."""
    mock_tracer = MagicMock()
    # Make the import inside trace_span fail
    with patch.dict("sys.modules", {"opentelemetry": None, "opentelemetry.trace": None}):
        # Need a fresh tracer that will trigger the import path
        # The function imports opentelemetry.trace inside — patch it to raise ImportError
        with patch("builtins.__import__", side_effect=ImportError("no otel")):
            async with trace_span(mock_tracer, "step") as span:
                assert span is None


@pytest.mark.anyio
async def test_trace_span_with_in_memory_exporter(anyio_backend):
    """trace_span works end-to-end with the real OTel in-memory exporter."""
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError:
        pytest.skip("opentelemetry-sdk not installed")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    async with trace_span(tracer, "pipeline.parse_request", {"model": "qwen3:4b"}) as span:
        assert span is not None

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "pipeline.parse_request"
    attrs = dict(s.attributes or {})
    assert attrs["model"] == "qwen3:4b"
