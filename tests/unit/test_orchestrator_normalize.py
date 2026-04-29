"""Regression test for P0-7: `else` clause attached to `try` in
`_after_stream_record` caused double normalization when SchemaIntelligence
was present and `process_response` succeeded.

Before the fix, the structure was:

    try:
        _si = ...
        if _si:
            model_response = _si.process_response(...)  # first normalization
    except Exception:
        ...
    else:                                               # ← attached to TRY
        model_response = normalize_model_response(...)  # second normalization

The `else` ran every time the try block did not raise, including the happy
path where `_si` was set and succeeded. The standalone normalizer should only
run when SI is unavailable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _build_ctx_with_si(si):
    """Build a minimal pipeline context object that `_after_stream_record` reads from."""
    ctx = SimpleNamespace()
    ctx.schema_intelligence = si
    ctx.wal_writer = MagicMock()
    ctx.walacor_client = None
    ctx.storage = None
    ctx.session_chain = None
    ctx.control_store = None
    ctx.http_client = None
    return ctx


def _make_model_response():
    from gateway.adapters.base import ModelResponse
    return ModelResponse(
        content="hello",
        usage=None,
        raw_body=b'{"content":"hello"}',
        provider_request_id="rid",
        model_hash="",
    )


def _make_call():
    from gateway.adapters.base import ModelCall
    return ModelCall(
        provider="ollama",
        model_id="test-model",
        prompt_text="hi",
        raw_body=b"{}",
        is_streaming=True,
        metadata={"session_id": None, "user": None, "prompt_id": "p"},
    )


def _make_adapter(provider_name: str = "ollama"):
    """Minimal adapter stub that satisfies `_after_stream_record`'s touch points."""
    adapter = MagicMock()
    adapter.parse_streamed_response = MagicMock(return_value=_make_model_response())
    adapter.get_provider_name = MagicMock(return_value=provider_name)
    return adapter


@pytest.mark.anyio
async def test_si_set_runs_normalization_exactly_once(monkeypatch):
    """When SchemaIntelligence is present and succeeds, normalize_model_response
    must NOT be called in addition. Asserts the standalone normalizer is untouched."""
    from gateway.pipeline import orchestrator

    si = MagicMock()
    norm_report = SimpleNamespace(changes=[])
    si.process_response = MagicMock(return_value=(_make_model_response(), norm_report))

    ctx = _build_ctx_with_si(si)

    # Patch the helpers `_after_stream_record` calls so the test does not need
    # a full pipeline. We assert on `normalize_model_response` itself — if the
    # bug returns, this stub will be called and the test will fail.
    standalone_norm_called = []

    def _fake_standalone_norm(resp, provider):
        standalone_norm_called.append(provider)
        return resp

    # Avoid running tail-end record/chain/storage logic.
    monkeypatch.setattr(orchestrator, "get_pipeline_context", lambda: ctx)
    monkeypatch.setattr(
        orchestrator, "_record_token_usage",
        MagicMock(side_effect=lambda *a, **kw: _async_none()),
    )
    monkeypatch.setattr(
        orchestrator, "_eval_post_stream_policy",
        MagicMock(side_effect=lambda *a, **kw: _async_tuple()),
    )
    monkeypatch.setattr(
        orchestrator, "_session_chain_lock",
        lambda *a, **kw: _NoopAsyncCM(),
    )
    monkeypatch.setattr(
        orchestrator, "_apply_session_chain",
        MagicMock(side_effect=lambda *a, **kw: _async_value(None)),
    )
    monkeypatch.setattr(
        orchestrator, "_write_tool_events",
        MagicMock(side_effect=lambda *a, **kw: _async_none()),
    )
    monkeypatch.setattr(
        orchestrator, "build_execution_record",
        MagicMock(return_value={"execution_id": "x", "sequence_number": 0, "record_id": "r"}),
    )

    # Patch the normalizer where it's looked up at call time.
    with patch(
        "gateway.pipeline.normalizer.normalize_model_response",
        side_effect=_fake_standalone_norm,
    ):
        await orchestrator._after_stream_record(
            buffer=[b"chunk"],
            call=_make_call(),
            adapter=_make_adapter("ollama"),
            attestation_id="att",
            policy_version=1,
            policy_result="pass",
            audit_metadata={},
            budget_estimated=0,
            pipeline_start=None,
            governance_meta=None,
            request=None,
            prebuilt_model_response=None,
        )

    # SI took the normalization path — the standalone normalizer must NOT
    # have run. With the pre-fix `else` attached to the try, this list would
    # contain "ollama".
    assert standalone_norm_called == []
    # And SI was used exactly once.
    assert si.process_response.call_count == 1


@pytest.mark.anyio
async def test_si_missing_runs_standalone_once(monkeypatch):
    """When SchemaIntelligence is absent, standalone normalize_model_response
    runs exactly once."""
    from gateway.pipeline import orchestrator

    ctx = _build_ctx_with_si(None)

    standalone_calls = []

    def _fake_standalone_norm(resp, provider):
        standalone_calls.append(provider)
        return resp

    monkeypatch.setattr(orchestrator, "get_pipeline_context", lambda: ctx)
    monkeypatch.setattr(
        orchestrator, "_record_token_usage",
        MagicMock(side_effect=lambda *a, **kw: _async_none()),
    )
    monkeypatch.setattr(
        orchestrator, "_eval_post_stream_policy",
        MagicMock(side_effect=lambda *a, **kw: _async_tuple()),
    )
    monkeypatch.setattr(
        orchestrator, "_session_chain_lock",
        lambda *a, **kw: _NoopAsyncCM(),
    )
    monkeypatch.setattr(
        orchestrator, "_apply_session_chain",
        MagicMock(side_effect=lambda *a, **kw: _async_value(None)),
    )
    monkeypatch.setattr(
        orchestrator, "_write_tool_events",
        MagicMock(side_effect=lambda *a, **kw: _async_none()),
    )
    monkeypatch.setattr(
        orchestrator, "build_execution_record",
        MagicMock(return_value={"execution_id": "x", "sequence_number": 0, "record_id": "r"}),
    )

    with patch(
        "gateway.pipeline.normalizer.normalize_model_response",
        side_effect=_fake_standalone_norm,
    ):
        await orchestrator._after_stream_record(
            buffer=[b"chunk"],
            call=_make_call(),
            adapter=_make_adapter("ollama"),
            attestation_id="att",
            policy_version=1,
            policy_result="pass",
            audit_metadata={},
            budget_estimated=0,
            pipeline_start=None,
            governance_meta=None,
            request=None,
            prebuilt_model_response=None,
        )

    assert standalone_calls == ["ollama"]


# ── async test helpers ────────────────────────────────────────────────────────

async def _async_none():
    return None


async def _async_value(v):
    return v


async def _async_tuple():
    return (0, "skipped", [])


class _NoopAsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None
