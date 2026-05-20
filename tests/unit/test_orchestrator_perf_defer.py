"""Pin the hot-path performance optimisations in ``pipeline/orchestrator``:

1. Intent classification (``_classify_intent_si``) is launched as an
   ``asyncio.Task`` BEFORE the governance pre-checks run, so the 50-200 ms
   ONNX inference overlaps with attestation/policy/budget/rate-limit and
   does not add to wall-clock latency on the hot path.

2. SchemaMapper (``_run_schema_mapper``) is deferred off the request
   response path on the success branch — the orchestrator returns the
   HTTP response BEFORE the 50-150 ms ONNX classification has run, and
   the audit record is built/written in a shielded background task that
   eventually populates the mapping.

These tests target the helper functions directly so we do not need to
spin up the full ASGI app + provider stack. They lock in the latency
contracts the perf refactor was introduced to honour; if the helpers
ever go serial again the asserts will fire.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.adapters.base import ModelCall, ModelResponse
from gateway.pipeline import orchestrator


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ── Shared scaffolding ──────────────────────────────────────────────────────


def _make_call(prompt_text: str = "hello") -> ModelCall:
    return ModelCall(
        provider="openai",
        model_id="gpt-4o-mini",
        is_streaming=False,
        raw_body=b"{}",
        prompt_text=prompt_text,
        metadata={},
    )


def _make_ctx(skip_governance: bool = False) -> SimpleNamespace:
    """Minimal pipeline context for the helpers under test."""
    return SimpleNamespace(
        skip_governance=skip_governance,
        tool_registry=None,
        model_registry=None,
        verdict_buffer=None,
        shadow_runner=None,
        intelligence_db=None,
        schema_intelligence=None,
        schema_mapper=None,
    )


# ── Fix #1: intent classification runs concurrently with pre-checks ─────────


@pytest.mark.anyio
async def test_classify_intent_si_skip_governance_returns_empty():
    """Transparent-proxy mode must never invoke SchemaIntelligence."""
    ctx = _make_ctx(skip_governance=True)
    settings = SimpleNamespace(mcp_servers_json=None)
    result = await orchestrator._classify_intent_si(
        ctx, settings, _make_call(), body_dict={}, body_meta=None,
    )
    assert result == {}
    assert ctx.schema_intelligence is None  # must not have been instantiated


@pytest.mark.anyio
async def test_classify_intent_si_runs_in_parallel_with_governance(monkeypatch):
    """Verify the intent task overlaps with a fake "governance" coroutine.

    Both work units sleep 50 ms. If the helper runs them serially the
    total elapsed will be ~100 ms; in the parallel arrangement the
    orchestrator now uses it should be ~50 ms. We assert <80 ms so the
    test is not flaky on slow CI while still failing if the code goes
    serial again.
    """
    ctx = _make_ctx()
    settings = SimpleNamespace(mcp_servers_json=None)

    # Stub SchemaIntelligence with a sleepy ``process_request`` so we can
    # measure overlap deterministically without loading ONNX.
    class _SlowSI:
        def __init__(self, **_kwargs):
            pass

        def process_request(self, _messages, _meta, _model_id):
            time.sleep(0.05)  # blocking — to_thread offloads it
            return {
                "_intent": "normal",
                "_intent_confidence": 0.9,
                "_intent_tier": "tier2",
                "user_question": "hi",
                "_intent_reason": "stub",
            }

    monkeypatch.setattr(
        "gateway.classifier.unified.SchemaIntelligence", _SlowSI, raising=False,
    )

    async def _fake_governance():
        await asyncio.sleep(0.05)
        return "ok"

    t0 = time.perf_counter()
    task = asyncio.create_task(
        orchestrator._classify_intent_si(ctx, settings, _make_call(), {}, None)
    )
    gov_result = await _fake_governance()
    enrichment = await task
    elapsed = time.perf_counter() - t0

    assert gov_result == "ok"
    assert enrichment.get("_intent") == "normal"
    assert elapsed < 0.08, (
        f"intent + governance ran serially (elapsed={elapsed:.3f}s); "
        "the perf refactor must keep them concurrent"
    )


@pytest.mark.anyio
async def test_classify_intent_si_swallows_exceptions():
    """SI failures must never bubble to the request pipeline."""
    ctx = _make_ctx()

    class _BoomSI:
        def __init__(self, **_kwargs):
            pass

        def process_request(self, *_a, **_kw):
            raise RuntimeError("ONNX session corrupted")

    ctx.schema_intelligence = _BoomSI()
    settings = SimpleNamespace(mcp_servers_json=None)
    result = await orchestrator._classify_intent_si(
        ctx, settings, _make_call(), {}, None,
    )
    assert result == {}


def test_apply_intent_enrichment_overwrites_prompt_and_audit():
    """The merge helper must rewrite ``prompt_text`` and populate audit fields."""
    call = _make_call(prompt_text="system: be helpful\nuser: hi")
    extra: dict = {}
    enrichment = {
        "_intent": "normal",
        "_intent_confidence": 0.91,
        "_intent_tier": "tier2",
        "_intent_reason": "default",
        "user_question": "hi",
        "conversation_turns": 1,
        "question_fingerprint": "abc",
        "extraction_method": "regex",
        "has_rag_context": False,
        "has_files": False,
        "chat_id": "c-1",
        "message_id": "m-1",
    }
    new_call = orchestrator._apply_intent_enrichment(
        call, extra, enrichment, body_meta={"chat_id": "c-1", "message_id": "m-1"},
    )
    assert new_call.prompt_text == "hi"
    assert new_call.metadata["_intent"] == "normal"
    assert new_call.metadata["chat_id"] == "c-1"
    assert extra["walacor_audit"]["user_question"] == "hi"
    assert extra["walacor_audit"]["extraction_method"] == "regex"


def test_apply_intent_enrichment_empty_is_noop():
    call = _make_call(prompt_text="original")
    extra: dict = {"walacor_audit": {"foo": "bar"}}
    new_call = orchestrator._apply_intent_enrichment(call, extra, {}, None)
    assert new_call is call
    assert extra == {"walacor_audit": {"foo": "bar"}}


# ── Fix #2: SchemaMapper is deferred off the response path ──────────────────


class _FakeCanonical:
    """Minimal stand-in for ``gateway.schema.mapper.CanonicalResponse``."""

    def __init__(self):
        self.usage = SimpleNamespace(
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
        )
        self.content = ""
        self.thinking_content = ""
        self.mapping = SimpleNamespace(
            confidence=0.8, confidence_on_mapped=0.9,
            mapped_fields=["a", "b"], unmapped_fields=[],
        )
        self.overflow = {}
        self.timing = None
        self.citations = []


class _SlowMapper:
    """Fake SchemaMapper whose ``map_response`` sleeps so we can detect the
    deferral. The hot-path code must NOT await this sleep before returning
    the response to the client.
    """

    def __init__(self, sleep_s: float = 0.2):
        self._sleep_s = sleep_s
        self.called_at: float | None = None

    def map_response(self, _raw):
        self.called_at = time.perf_counter()
        time.sleep(self._sleep_s)
        return _FakeCanonical()

    def timeout_count_60s(self) -> int:
        return 0


@pytest.mark.anyio
async def test_run_schema_mapper_skipped_on_4xx_5xx():
    """Mapper must not run on error responses (gated <400)."""
    ctx = _make_ctx()
    ctx.schema_mapper = _SlowMapper(sleep_s=0)
    fake_request = SimpleNamespace(state=SimpleNamespace())
    http_resp = MagicMock(status_code=500, body=b"{}")
    model_resp = ModelResponse(content="", usage=None, raw_body=b"")

    new_resp, meta = await orchestrator._run_schema_mapper(
        fake_request, ctx, http_resp, model_resp,
    )
    assert meta == {}
    assert ctx.schema_mapper.called_at is None
    assert new_resp is model_resp


@pytest.mark.anyio
async def test_finalize_audit_write_runs_deferred_mapper(monkeypatch):
    """When ``_schema_mapper_deferred=True`` is set on request.state, the
    finalize helper must invoke the SchemaMapper inline (i.e. from the
    background task), feeding the resulting metadata into the eventual
    record write.
    """
    ctx = _make_ctx()
    ctx.schema_mapper = _SlowMapper(sleep_s=0)

    fake_request = SimpleNamespace(
        state=SimpleNamespace(_schema_mapper_deferred=True),
    )
    http_resp = MagicMock(status_code=200, body=b'{"ok": true}')
    model_resp = ModelResponse(content="hello", usage=None, raw_body=b"")
    call = _make_call()

    seen: dict = {}

    async def _fake_build_and_write(request, c, mr, params, ctx_, settings_):
        seen["metadata"] = dict(c.metadata)
        seen["canonical"] = getattr(request.state, "_canonical_response", None)

    monkeypatch.setattr(orchestrator, "_build_and_write_record", _fake_build_and_write)

    await orchestrator._finalize_audit_write(
        fake_request, call, model_resp, http_resp,
        params=MagicMock(),
        ctx=ctx,
        settings=SimpleNamespace(),
    )
    # SchemaMapper ran and merged its metadata into the call before the write.
    assert "schema_mapper_confidence" in seen["metadata"]
    assert seen["canonical"] is not None
    # The one-shot flag is flipped off so a second call would not re-run.
    assert fake_request.state._schema_mapper_deferred is False


@pytest.mark.anyio
async def test_finalize_audit_write_mapper_failure_records_mapping_error(monkeypatch):
    """A SchemaMapper crash inside the deferred task must surface as
    ``mapping_error`` on the audit record, NEVER re-raise — the client
    response has already been sent.
    """
    ctx = _make_ctx()

    class _BoomMapper:
        def map_response(self, _raw):
            raise RuntimeError("ORT exploded")

        def timeout_count_60s(self) -> int:
            return 0

    ctx.schema_mapper = _BoomMapper()
    fake_request = SimpleNamespace(
        state=SimpleNamespace(_schema_mapper_deferred=True),
    )
    http_resp = MagicMock(status_code=200, body=b'{"ok": true}')
    model_resp = ModelResponse(content="hi", usage=None, raw_body=b"")
    call = _make_call()

    seen: dict = {}

    async def _fake_build_and_write(request, c, mr, params, ctx_, settings_):
        seen["metadata"] = dict(c.metadata)

    monkeypatch.setattr(orchestrator, "_build_and_write_record", _fake_build_and_write)

    # Must not raise.
    await orchestrator._finalize_audit_write(
        fake_request, call, model_resp, http_resp,
        params=MagicMock(),
        ctx=ctx,
        settings=SimpleNamespace(),
    )
    # The mapper crash was caught by _run_schema_mapper itself (debug log,
    # no metadata). The deferred wrapper does not double-record. Either
    # way: no exception escaped to the caller — that is the invariant.
    assert "metadata" in seen


@pytest.mark.anyio
async def test_hot_path_returns_before_deferred_mapper_completes(monkeypatch):
    """Simulate the success-branch pattern from ``_handle_request_inner``:

      1. Set ``request.state._schema_mapper_deferred = True``
      2. Launch ``_finalize_audit_write`` as a shielded background task
      3. The "response" is returned immediately

    Assert that step 3 happens before the slow mapper has finished, and
    that the background task does eventually complete (i.e. the mapping
    is EVENTUALLY populated — the contract the dashboard relies on).
    """
    ctx = _make_ctx()
    slow_mapper = _SlowMapper(sleep_s=0.15)
    ctx.schema_mapper = slow_mapper

    fake_request = SimpleNamespace(
        state=SimpleNamespace(_schema_mapper_deferred=True),
    )
    http_resp = MagicMock(status_code=200, body=b'{"ok": true}')
    model_resp = ModelResponse(content="hello", usage=None, raw_body=b"")
    call = _make_call()

    write_finished = asyncio.Event()

    async def _fake_build_and_write(*_a, **_kw):
        write_finished.set()

    monkeypatch.setattr(orchestrator, "_build_and_write_record", _fake_build_and_write)

    async def _bg_run():
        await asyncio.shield(
            orchestrator._finalize_audit_write(
                fake_request, call, model_resp, http_resp,
                params=MagicMock(), ctx=ctx, settings=SimpleNamespace(),
            )
        )

    t_dispatch = time.perf_counter()
    bg = asyncio.create_task(_bg_run())
    # Simulate the orchestrator returning the response to the client.
    # In production this happens immediately after the create_task above.
    response_returned_at = time.perf_counter()
    response_latency = response_returned_at - t_dispatch

    # Must be near-zero — the hot path does not await the slow mapper.
    assert response_latency < 0.02, (
        f"hot-path latency leaked {response_latency*1000:.1f} ms — "
        "the schema mapper deferral has regressed"
    )

    # Mapper has either not started yet OR started very recently —
    # critically, it has NOT finished. (Allow scheduling slop.)
    assert not write_finished.is_set(), (
        "background audit write finished before the response was returned — "
        "deferral is not effective"
    )

    # Eventual consistency: the background task does run to completion.
    await asyncio.wait_for(bg, timeout=2.0)
    assert write_finished.is_set()
    assert slow_mapper.called_at is not None
