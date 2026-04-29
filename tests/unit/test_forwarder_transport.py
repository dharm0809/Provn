"""Forwarder transport-error → ResourceMonitor wiring + stream PII mode tests.

Covers:
  - Bug #48: TransportError raised before a response materializes still feeds
    ``record_provider_result`` so circuit-breaker / cooldown logic sees the
    failure (not just 5xx responses).
  - Bug #19: ``WALACOR_STREAM_PII_MODE=abort`` must close the stream when PII
    is detected mid-stream instead of merely logging.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _aiter(items):
    for item in items:
        yield item


def _make_mock_adapter(provider_name: str = "openai"):
    adapter = MagicMock()
    adapter.get_provider_name.return_value = provider_name
    req = MagicMock()
    req.method = "POST"
    req.url = "https://api.example.com/v1/chat/completions"
    req.headers = {}
    req.content = b"{}"
    adapter.build_forward_request = AsyncMock(return_value=req)
    return adapter


def _make_mock_call(session_id: str = "sess-x"):
    call = MagicMock()
    call.metadata = {"session_id": session_id}
    return call


# ---------------------------------------------------------------------------
# #48: TransportError feeds ResourceMonitor.record_provider_result
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_forward_records_transport_error_to_resource_monitor():
    """Non-streaming forward(): when client.send raises ConnectError the resource
    monitor must be notified (this is the gap referenced by main.py's TODO)."""
    from gateway.pipeline.context import get_pipeline_context
    from gateway.pipeline.forwarder import forward

    ctx = get_pipeline_context()
    rm = MagicMock()
    rm.record_provider_result = MagicMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.send = AsyncMock(side_effect=httpx.ConnectError("ECONNREFUSED"))

    ctx.http_client = mock_client
    ctx.resource_monitor = rm
    try:
        with patch("gateway.pipeline.forwarder.forward_duration") as mock_dur:
            mock_dur.labels.return_value.observe = MagicMock()
            with pytest.raises(httpx.ConnectError):
                await forward(_make_mock_adapter("openai"), _make_mock_call(), MagicMock())

        rm.record_provider_result.assert_called_once()
        kwargs = rm.record_provider_result.call_args.kwargs
        args = rm.record_provider_result.call_args.args
        assert args[0] == "openai"
        assert kwargs.get("success") is False
        # error string should reflect the transport error (classified)
        assert kwargs.get("error") is not None
    finally:
        ctx.http_client = None
        ctx.resource_monitor = None


@pytest.mark.anyio
async def test_stream_with_tee_records_transport_error_to_resource_monitor():
    """Streaming path: when upstream.stream().__aenter__ raises a TransportError
    the resource monitor still sees the failure."""
    from gateway.pipeline.context import get_pipeline_context
    from gateway.pipeline.forwarder import stream_with_tee

    ctx = get_pipeline_context()
    rm = MagicMock()
    rm.record_provider_result = MagicMock()

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    ctx.http_client = mock_client
    ctx.resource_monitor = rm
    try:
        with patch("gateway.pipeline.forwarder.forward_duration") as mock_dur:
            mock_dur.labels.return_value.observe = MagicMock()
            with pytest.raises(httpx.ReadTimeout):
                await stream_with_tee(_make_mock_adapter("ollama"), _make_mock_call(), MagicMock())

        rm.record_provider_result.assert_called_once()
        args = rm.record_provider_result.call_args.args
        kwargs = rm.record_provider_result.call_args.kwargs
        assert args[0] == "ollama"
        assert kwargs.get("success") is False
        assert kwargs.get("error") is not None
    finally:
        ctx.http_client = None
        ctx.resource_monitor = None


# ---------------------------------------------------------------------------
# #19: stream_pii_mode=abort closes the stream
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stream_pii_mode_abort_closes_stream_on_detection():
    """When mode=abort and PII is detected, the generator yields an SSE error
    event and stops emitting upstream chunks."""
    from gateway.config import get_settings
    from gateway.pipeline.context import get_pipeline_context
    from gateway.pipeline.forwarder import stream_with_tee

    # The PII windowed check only runs after accumulated_text has grown by
    # _PII_CHECK_INTERVAL (500 chars). Send a single chunk large enough to
    # trip the window AND containing a real PII pattern (SSN: \d{3}-\d{2}-\d{4}).
    pii_chunk = b"A" * 600 + b" My SSN is 123-45-6789. " + b"B" * 50
    after_chunk = b"this should NOT be emitted\n"

    mock_upstream = MagicMock()
    mock_upstream.status_code = 200
    mock_upstream.aiter_bytes = MagicMock(
        return_value=_aiter([pii_chunk, after_chunk])
    )

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_upstream)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    ctx = get_pipeline_context()
    ctx.http_client = mock_client

    monkeypatch = None  # avoid dependency on the fixture; mutate settings directly
    settings = get_settings()
    original = settings.stream_pii_mode
    object.__setattr__(settings, "stream_pii_mode", "abort")
    try:
        with patch("gateway.pipeline.forwarder.forward_duration") as mock_dur:
            mock_dur.labels.return_value.observe = MagicMock()
            resp, _ = await stream_with_tee(
                _make_mock_adapter("ollama"), _make_mock_call(), MagicMock()
            )

        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c)

        joined = b"".join(chunks)
        # Must contain the abort error marker
        assert b"pii_detected" in joined
        # Must NOT contain the post-PII chunk content (truncated)
        assert b"this should NOT be emitted" not in joined
    finally:
        object.__setattr__(settings, "stream_pii_mode", original)
        ctx.http_client = None


@pytest.mark.anyio
async def test_stream_pii_mode_warn_does_not_close_stream():
    """Default warn mode keeps streaming after PII detection (current behaviour)."""
    from gateway.config import get_settings
    from gateway.pipeline.context import get_pipeline_context
    from gateway.pipeline.forwarder import stream_with_tee

    pii_chunk = b"A" * 600 + b" My SSN is 123-45-6789. " + b"B" * 50
    after_chunk = b"streamed-after-pii\n"

    mock_upstream = MagicMock()
    mock_upstream.status_code = 200
    mock_upstream.aiter_bytes = MagicMock(
        return_value=_aiter([pii_chunk, after_chunk])
    )

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_upstream)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)

    ctx = get_pipeline_context()
    ctx.http_client = mock_client

    settings = get_settings()
    original = settings.stream_pii_mode
    object.__setattr__(settings, "stream_pii_mode", "warn")
    try:
        with patch("gateway.pipeline.forwarder.forward_duration") as mock_dur:
            mock_dur.labels.return_value.observe = MagicMock()
            resp, _ = await stream_with_tee(
                _make_mock_adapter("ollama"), _make_mock_call(), MagicMock()
            )

        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c)

        joined = b"".join(chunks)
        # Stream continues past PII in warn mode
        assert b"streamed-after-pii" in joined
        assert b"pii_detected" not in joined
    finally:
        object.__setattr__(settings, "stream_pii_mode", original)
        ctx.http_client = None
