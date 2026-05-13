"""Unit tests for the OpenWebUI plugin governance pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.openwebui.governance import (
    resolve_provider_for_model,
    _build_model_call,
    _build_model_response,
    _extract_token_usage,
    _auto_attest,
    process_plugin_event,
)
# C5: `_apply_session_chain` was extracted into the shared helper module so
# the proxy path and the plugin governance path share one implementation.
# Tests that exercised the per-module copy now go through the shared helper.
from gateway.pipeline.chain_helpers import apply_session_chain as _apply_session_chain


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ---------------------------------------------------------------------------
# Sample events
# ---------------------------------------------------------------------------

def _inlet_event(model="llama3.1:8b", chat_id="chat-1", user_id="u1"):
    return {
        "event_type": "inlet",
        "model": model,
        "chat_id": chat_id,
        "user": {"id": user_id, "name": "Test", "email": "t@x.com", "role": "user"},
        "data": {
            "all_messages": [
                {"role": "system", "content": "You are a helper."},
                {"role": "user", "content": "Hello world"},
            ],
            "message_count": 2,
            "last_user_message": "Hello world",
        },
    }


def _outlet_event(model="llama3.1:8b", chat_id="chat-1", user_id="u1"):
    return {
        "event_type": "outlet",
        "model": model,
        "chat_id": chat_id,
        "user": {"id": user_id, "name": "Test", "email": "t@x.com", "role": "user"},
        "data": {
            "all_messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "assistant_response": "Hi there!",
            "response_length": 9,
            "message_count": 2,
            "governance": {
                "execution_id": "exec-abc-123",
                "attestation_id": "self-attested:llama3.1:8b",
                "policy_result": "pass",
            },
        },
    }


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _mock_settings(
    provider="ollama",
    model_routes=None,
    tenant_id="test-tenant",
    gateway_id="gw-1",
    chain_enabled=True,
    response_policy_enabled=False,
    plugin_governance=True,
    skip_governance=False,
):
    s = MagicMock()
    s.gateway_provider = provider
    s.model_routes = model_routes or []
    s.gateway_tenant_id = tenant_id
    s.gateway_id = gateway_id
    s.session_chain_enabled = chain_enabled
    s.response_policy_enabled = response_policy_enabled
    s.plugin_event_governance_enabled = plugin_governance
    s.skip_governance = skip_governance
    s.attestation_cache_ttl = 300
    # C3: helper checks settings.record_signing_enabled to decide whether to
    # call sign_canonical. Default OFF in tests so signing is never attempted
    # — matches production default.
    s.record_signing_enabled = False
    return s


def _mock_ctx(
    has_attestation=True,
    has_policy=True,
    has_chain=True,
    has_storage=True,
    has_control_store=False,
):
    ctx = MagicMock()

    # Attestation cache
    if has_attestation:
        cache = MagicMock()
        att = MagicMock()
        att.attestation_id = "self-attested:llama3.1:8b"
        att.provider = "ollama"
        att.status = "active"
        att.verification_level = "self_attested"
        att.tenant_id = "test-tenant"
        att.is_blocked = False
        att.is_expired = False
        cache.get.return_value = att
        ctx.attestation_cache = cache
    else:
        ctx.attestation_cache = None

    # Policy cache
    if has_policy:
        pc = MagicMock()
        pc.is_stale = False
        pc.version = 1
        ctx.policy_cache = pc
    else:
        ctx.policy_cache = None

    # Session chain
    if has_chain:
        import asyncio
        from gateway.pipeline.session_chain import ChainValues
        # The shared chain helper now expects a real asyncio.Lock from
        # `session_lock(session_id)` so it can `async with` it (C4). An
        # AsyncMock auto-generates an attribute but returns a coroutine, not
        # an async-context-manager — explicit lock fixes that.
        chain = MagicMock()
        chain.next_chain_values = AsyncMock(return_value=ChainValues(
            sequence_number=0, previous_record_hash="0" * 128, previous_record_id=None,
        ))
        chain.update = AsyncMock()
        # Per-session lock for the (reserve→write→advance) critical section.
        _session_locks: dict[str, asyncio.Lock] = {}

        def _session_lock(sid: str):
            lock = _session_locks.get(sid)
            if lock is None:
                lock = asyncio.Lock()
                _session_locks[sid] = lock
            return lock

        chain.session_lock = _session_lock
        ctx.session_chain = chain
    else:
        ctx.session_chain = None

    # Storage
    if has_storage:
        storage = AsyncMock()
        write_result = MagicMock()
        write_result.succeeded = True
        write_result.failed = []
        storage.write_execution.return_value = write_result
        ctx.storage = storage
    else:
        ctx.storage = None

    ctx.sync_client = None
    ctx.content_analyzers = []

    if has_control_store:
        cs = MagicMock()
        cs.list_attestations.return_value = []
        cs.upsert_attestation.return_value = {}
        ctx.control_store = cs
    else:
        ctx.control_store = None

    return ctx


# ---------------------------------------------------------------------------
# resolve_provider_for_model
# ---------------------------------------------------------------------------

def test_resolve_provider_fallback():
    with patch("gateway.openwebui.governance.get_settings") as gs:
        gs.return_value = _mock_settings(provider="ollama", model_routes=[])
        assert resolve_provider_for_model("llama3.1:8b") == "ollama"


def test_resolve_provider_with_routes():
    routes = [
        {"pattern": "gpt-*", "provider": "openai"},
        {"pattern": "claude-*", "provider": "anthropic"},
    ]
    with patch("gateway.openwebui.governance.get_settings") as gs:
        gs.return_value = _mock_settings(provider="ollama", model_routes=routes)
        assert resolve_provider_for_model("gpt-4o") == "openai"
        assert resolve_provider_for_model("claude-3-opus") == "anthropic"
        assert resolve_provider_for_model("llama3.1:8b") == "ollama"


# ---------------------------------------------------------------------------
# _build_model_call
# ---------------------------------------------------------------------------

def test_build_model_call():
    event = _inlet_event()
    call = _build_model_call(event, "ollama")
    assert call.model_id == "llama3.1:8b"
    assert call.provider == "ollama"
    assert "[system] You are a helper." in call.prompt_text
    assert "[user] Hello world" in call.prompt_text
    assert call.metadata["session_id"] == "owui:chat-1"
    assert call.metadata["event_source"] == "openwebui_plugin"
    assert call.metadata["user"] == "u1"
    assert call.is_streaming is False


def test_build_model_call_empty_messages():
    event = {"model": "test", "chat_id": "c1", "user": {}, "data": {}}
    call = _build_model_call(event, "openai")
    assert call.prompt_text == ""
    assert call.model_id == "test"


# ---------------------------------------------------------------------------
# _build_model_response
# ---------------------------------------------------------------------------

def test_build_model_response():
    event = _outlet_event()
    # C8: `_build_model_response` now returns `(response, tokens_estimated)`
    # so callers can stamp the top-level `tokens_estimated` field on the
    # execution record. The pre-fix signature only returned `response`.
    resp, estimated = _build_model_response(event)
    assert resp.content == "Hi there!"
    assert resp.provider_request_id == "exec-abc-123"
    assert resp.usage is not None
    assert resp.usage["completion_tokens"] > 0
    # Plugin events always estimate token counts (provider headers don't
    # carry usage on the OWUI plugin path).
    assert estimated is True


def test_build_model_response_empty():
    event = {"data": {}}
    resp, estimated = _build_model_response(event)
    assert resp.content == ""
    assert resp.usage["completion_tokens"] == 1  # max(0//4, 1)
    assert estimated is True


# ---------------------------------------------------------------------------
# _extract_token_usage
# ---------------------------------------------------------------------------

def test_extract_token_usage_estimates():
    usage, estimated = _extract_token_usage({}, 400)
    assert usage["completion_tokens"] == 100  # 400 // 4
    assert usage["token_source"] == "estimated"
    assert estimated is True


def test_extract_token_usage_minimum():
    usage, estimated = _extract_token_usage({}, 0)
    assert usage["completion_tokens"] == 1
    assert estimated is True


# ---------------------------------------------------------------------------
# _auto_attest
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_auto_attest_no_control_store():
    ctx = _mock_ctx(has_control_store=False)
    settings = _mock_settings()
    att_id, att_ctx = await _auto_attest(ctx, settings, "ollama", "llama3.1:8b")
    assert att_id == "self-attested:llama3.1:8b"
    assert att_ctx["status"] == "active"
    ctx.attestation_cache.set.assert_called_once()


@pytest.mark.anyio
async def test_auto_attest_with_control_store():
    ctx = _mock_ctx(has_control_store=True)
    settings = _mock_settings()
    att_id, att_ctx = await _auto_attest(ctx, settings, "ollama", "llama3.1:8b")
    assert att_id == "self-attested:llama3.1:8b"
    assert att_ctx["status"] == "active"
    ctx.control_store.upsert_attestation.assert_called_once()


@pytest.mark.anyio
async def test_auto_attest_revoked_model():
    ctx = _mock_ctx(has_control_store=True)
    ctx.control_store.list_attestations.return_value = [
        {"model_id": "llama3.1:8b", "provider": "ollama", "status": "revoked"}
    ]
    settings = _mock_settings()
    att_id, att_ctx = await _auto_attest(ctx, settings, "ollama", "llama3.1:8b")
    assert att_ctx["status"] == "revoked"


# ---------------------------------------------------------------------------
# apply_session_chain (shared helper, used by both proxy + plugin paths)
# ---------------------------------------------------------------------------
# After C5, both call sites import the same helper. These tests now exercise
# the shared module — they remain in this file because they cover the
# plugin-event integration shape (owui: prefix sessions, mocked tracker), not
# the orchestrator's call site.

@pytest.mark.anyio
async def test_apply_session_chain():
    ctx = _mock_ctx()
    settings = _mock_settings(chain_enabled=True)
    record = {
        "execution_id": "ex-1",
        "record_id": "01234567-89ab-7cde-f012-345678901234",
        "policy_version": 1,
        "policy_result": "pass",
        "timestamp": "2026-04-10T00:00:00+00:00",
    }
    result = await _apply_session_chain(record, "owui:chat-1", ctx, settings)
    assert result.applied is True
    assert record["sequence_number"] == 0
    assert record["previous_record_id"] is None  # genesis


@pytest.mark.anyio
async def test_apply_session_chain_disabled():
    ctx = _mock_ctx(has_chain=False)
    settings = _mock_settings(chain_enabled=False)
    record = {"execution_id": "ex-1"}
    result = await _apply_session_chain(record, "owui:chat-1", ctx, settings)
    assert result.applied is False


@pytest.mark.anyio
async def test_apply_session_chain_no_session_id():
    ctx = _mock_ctx()
    settings = _mock_settings()
    result = await _apply_session_chain({}, None, ctx, settings)
    assert result.applied is False


# ---------------------------------------------------------------------------
# process_plugin_event — inlet
# ---------------------------------------------------------------------------

def _ok_pre_result():
    """Build a PreInferenceResult with the post-fix 5-field shape.

    The pre-fix code (C1) unpacked 4 values from `evaluate_pre_inference`,
    which actually returns 5 — silently raising ValueError whenever the
    policy cache was wired up. Tests now mock the typed wrapper instead.
    """
    from gateway.pipeline.chain_helpers import PreInferenceResult
    return PreInferenceResult(
        blocked=False, policy_version=1, policy_result="pass",
        error_response=None, failure_reason=None,
    )


def _blocked_pre_result(error_response):
    from gateway.pipeline.chain_helpers import PreInferenceResult
    return PreInferenceResult(
        blocked=True, policy_version=2, policy_result="blocked_by_policy",
        error_response=error_response, failure_reason="test block",
    )


@pytest.mark.anyio
async def test_process_inlet_event():
    ctx = _mock_ctx()
    settings = _mock_settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=_ok_pre_result()),
    ):
        result = await process_plugin_event(_inlet_event())

    assert result["event_type"] == "inlet"
    assert result["governance_status"] == "pass"
    assert result["attestation_id"] == "self-attested:llama3.1:8b"
    assert result["policy_result"] == "pass"
    # No storage write for inlet
    ctx.storage.write_execution.assert_not_called()


# ---------------------------------------------------------------------------
# process_plugin_event — outlet (full pipeline)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_process_outlet_event_full_pipeline():
    ctx = _mock_ctx()
    settings = _mock_settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=_ok_pre_result()),
    ):
        result = await process_plugin_event(_outlet_event())

    assert result["event_type"] == "outlet"
    assert result["governance_status"] == "pass"
    assert "execution_id" in result
    assert result.get("sequence_number") == 0
    # Storage write called
    ctx.storage.write_execution.assert_called_once()
    record = ctx.storage.write_execution.call_args[0][0]
    assert record["model_id"] == "llama3.1:8b"
    assert record["provider"] == "ollama"
    assert record["session_id"] == "owui:chat-1"
    assert record["metadata"]["event_source"] == "openwebui_plugin"
    assert record["metadata"]["original_execution_id"] == "exec-abc-123"
    # C8: plugin records always carry `tokens_estimated=True` at the top
    # level so Walacor's metadata-truncation filter can't hide the fact.
    assert record.get("tokens_estimated") is True


# ---------------------------------------------------------------------------
# process_plugin_event — no governance infra (skip_governance mode)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_process_event_no_storage():
    """No storage = nothing to write to = skip entirely."""
    ctx = _mock_ctx(has_storage=False)
    settings = _mock_settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
    ):
        result = await process_plugin_event(_outlet_event())

    assert result["governance_status"] == "skipped"


@pytest.mark.anyio
async def test_process_event_no_caches_still_writes():
    """In skip_governance mode, caches are absent but storage exists.
    Execution record should still be written to Walacor/WAL."""
    ctx = _mock_ctx(has_attestation=False, has_policy=False, has_chain=False)
    settings = _mock_settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
    ):
        result = await process_plugin_event(_outlet_event())

    # Record written even without attestation/policy caches
    ctx.storage.write_execution.assert_called_once()
    assert "execution_id" in result


# ---------------------------------------------------------------------------
# process_plugin_event — attestation not found + auto-attest
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_process_event_auto_attest():
    ctx = _mock_ctx()
    # Make resolve_attestation return not found
    ctx.attestation_cache.get.return_value = None
    settings = _mock_settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=_ok_pre_result()),
    ):
        result = await process_plugin_event(_outlet_event())

    assert result["governance_status"] == "pass"
    assert result["attestation_id"] == "self-attested:llama3.1:8b"


# ---------------------------------------------------------------------------
# process_plugin_event — policy blocks (audit-only)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_process_outlet_policy_blocked():
    ctx = _mock_ctx()
    settings = _mock_settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference",
              return_value=_blocked_pre_result(MagicMock())),
    ):
        result = await process_plugin_event(_outlet_event())

    # Still writes the execution record (audit-only)
    assert result["governance_status"] == "blocked_post_facto"
    assert result["policy_result"] == "blocked_by_policy"
    ctx.storage.write_execution.assert_called_once()


# ---------------------------------------------------------------------------
# process_plugin_event — governance failure is fail-open
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_process_event_attestation_error_fail_open():
    ctx = _mock_ctx()
    settings = _mock_settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.resolve_attestation", side_effect=RuntimeError("boom")),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=_ok_pre_result()),
    ):
        result = await process_plugin_event(_outlet_event())

    # Should still succeed (fail-open)
    assert "errors" in result
    assert any("attestation" in e for e in result["errors"])
    # Storage write still happens
    ctx.storage.write_execution.assert_called_once()


# ---------------------------------------------------------------------------
# Session chain uses owui: prefix
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_session_chain_owui_prefix():
    ctx = _mock_ctx()
    settings = _mock_settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=_ok_pre_result()),
    ):
        await process_plugin_event(_outlet_event(chat_id="my-chat-42"))

    # Verify session chain was called with owui: prefix
    ctx.session_chain.next_chain_values.assert_called_with("owui:my-chat-42")
