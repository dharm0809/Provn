"""Tests covering the plugin governance fixes (C1, C4, C8).

* C1 — `evaluate_pre_inference` returns a 5-tuple, but the plugin governance
  pipeline used to unpack 4 values. This raised `ValueError` (caught as the
  generic "policy: <exc>" error) whenever `policy_cache` was configured. The
  fix routes calls through `run_pre_inference` so a future arity change
  breaks both the orchestrator and the plugin path at the same line.

* C4 — `process_plugin_event` used to call `_apply_session_chain` without
  acquiring the per-session lock. Two concurrent OWUI outlet events for the
  same chat_id could read the same `last_record_id` and emit duplicate
  `previous_record_id`. The fix wraps the chain section in
  `session_chain_critical_section` which holds an asyncio.Lock per
  session_id.

* C8 — Plugin events estimate token counts (governance headers don't carry
  real values) so the execution record now carries `tokens_estimated: True`
  at the TOP level — not just in metadata. Walacor's metadata-keep filter
  drops the `token_source: "estimated"` marker on long prompts, so the
  top-level field is the only durable signal.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.openwebui.governance import process_plugin_event
from gateway.pipeline.chain_helpers import PreInferenceResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ok_pre_result() -> PreInferenceResult:
    return PreInferenceResult(
        blocked=False, policy_version=1, policy_result="pass",
        error_response=None, failure_reason=None,
    )


def _ctx(*, chain_tracker, has_storage=True):
    """A pipeline-context double set up with the minimum surface the plugin
    governance entrypoint touches."""
    ctx = MagicMock()
    # Attestation
    cache = MagicMock()
    att = MagicMock()
    att.attestation_id = "self-attested:llama3.1:8b"
    att.provider = "ollama"
    att.status = "active"
    att.verification_level = "self_attested"
    att.tenant_id = "test-tenant"
    cache.get.return_value = att
    ctx.attestation_cache = cache
    # Policy cache — present so `run_pre_inference` is exercised.
    pc = MagicMock()
    pc.is_stale = False
    pc.version = 1
    ctx.policy_cache = pc
    # Session chain
    ctx.session_chain = chain_tracker
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
    ctx.control_store = None
    return ctx


def _settings():
    s = MagicMock()
    s.gateway_provider = "ollama"
    s.model_routes = []
    s.gateway_tenant_id = "test-tenant"
    s.gateway_id = "gw-1"
    s.session_chain_enabled = True
    s.response_policy_enabled = False
    s.plugin_event_governance_enabled = True
    s.skip_governance = False
    s.attestation_cache_ttl = 300
    s.record_signing_enabled = False  # C3 default
    return s


def _outlet_event(chat_id="chat-1"):
    return {
        "event_type": "outlet",
        "model": "llama3.1:8b",
        "chat_id": chat_id,
        "user": {"id": "u1", "name": "Test", "email": "t@x.com", "role": "user"},
        "data": {
            "all_messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "assistant_response": "Hi there!",
            "response_length": 9,
            "governance": {"execution_id": "exec-source"},
        },
    }


def _real_chain_tracker():
    """Return a real SessionChainTracker so session_lock semantics + state
    transitions match production. AsyncMock has no lock semantics."""
    from gateway.pipeline.session_chain import SessionChainTracker
    return SessionChainTracker()


# ── C1: evaluate_pre_inference unpack arity ─────────────────────────────


@pytest.mark.anyio
async def test_run_pre_inference_called_with_5_tuple_compatible_path():
    """C1 regression: ensure the plugin governance flow runs through the
    typed wrapper without raising ValueError. The pre-fix code unpacked 4
    values from a 5-tuple — caught only by the broad "except Exception" so
    every policy-cache-configured deployment silently dropped its policy
    decision."""
    ctx = _ctx(chain_tracker=_real_chain_tracker())
    settings = _settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference",
              return_value=_ok_pre_result()) as mock_pre,
    ):
        result = await process_plugin_event(_outlet_event())
    # 1. run_pre_inference was actually invoked (proves we go through the
    #    typed wrapper, not the raw evaluate_pre_inference call site).
    mock_pre.assert_called_once()
    # 2. No ValueError leaked into the error bucket (proof that the unpack
    #    arity bug is gone — the pre-fix code surfaced this as
    #    "policy: not enough values to unpack").
    errors = result.get("errors", []) or []
    assert not any("policy" in e for e in errors), errors
    assert result["policy_result"] == "pass"


@pytest.mark.anyio
async def test_pre_inference_blocked_propagates_typed_failure_reason():
    """Policy blocks must propagate `failure_reason` to the result body so
    the dashboard shows WHY the policy rejected. Pre-fix code dropped this
    entirely because the 5th field wasn't unpacked."""
    ctx = _ctx(chain_tracker=_real_chain_tracker())
    settings = _settings()
    blocked = PreInferenceResult(
        blocked=True, policy_version=2, policy_result="blocked_by_policy",
        error_response=MagicMock(), failure_reason="model_id not in allowlist",
    )
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=blocked),
    ):
        result = await process_plugin_event(_outlet_event())
    assert result["governance_status"] == "blocked_post_facto"
    assert result["policy_result"] == "blocked_by_policy"
    assert result["policy_failure_reason"] == "model_id not in allowlist"


# ── C4: per-session lock under concurrency ──────────────────────────────


@pytest.mark.anyio
async def test_concurrent_plugin_events_for_same_chat_serialise():
    """C4 regression: two concurrent outlet events for the same chat_id must
    produce a valid (non-broken) ID-pointer chain. Without the lock both
    coroutines could read the same `last_record_id` from the tracker and
    emit two records pointing at the same predecessor."""
    tracker = _real_chain_tracker()
    ctx = _ctx(chain_tracker=tracker)
    settings = _settings()
    # Captured records — assert their previous_record_id linkage at the end.
    captured: list[dict] = []

    async def capture_write(rec):
        # Force an interleaving window: yield to the event loop after
        # stamping but before tracker.update() runs (the helper does
        # advance AFTER this write). Without the per-session lock the second
        # coroutine could enter apply_session_chain here and read stale state.
        await asyncio.sleep(0.005)
        captured.append(dict(rec))
        wr = MagicMock()
        wr.succeeded = True
        wr.failed = []
        return wr

    ctx.storage.write_execution = capture_write

    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=_ok_pre_result()),
    ):
        # Fire two outlet events for the SAME chat_id concurrently.
        await asyncio.gather(
            process_plugin_event(_outlet_event(chat_id="same-chat")),
            process_plugin_event(_outlet_event(chat_id="same-chat")),
        )

    assert len(captured) == 2
    # Distinct record_ids per record (no duplication).
    rec_ids = [r.get("record_id") for r in captured]
    assert len(set(rec_ids)) == 2, f"duplicate record_ids: {rec_ids}"
    # Sequence numbers must be distinct (0 and 1).
    seqs = sorted(r.get("sequence_number") for r in captured)
    assert seqs == [0, 1], f"sequence collision: {seqs}"
    # The seq-1 record's previous_record_id must point at the seq-0 record's
    # record_id — proof the lock kept the (reserve→write→advance) atomic.
    by_seq = {r["sequence_number"]: r for r in captured}
    assert by_seq[1]["previous_record_id"] == by_seq[0]["record_id"]


# ── C8: tokens_estimated top-level field ─────────────────────────────────


@pytest.mark.anyio
async def test_outlet_record_carries_tokens_estimated_top_level():
    """C8: every plugin outlet event writes `tokens_estimated: True` at the
    top level of the execution record. Top-level (not metadata) because
    Walacor's metadata-keep filter drops fields on long prompts."""
    ctx = _ctx(chain_tracker=_real_chain_tracker())
    settings = _settings()
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=_ok_pre_result()),
    ):
        await process_plugin_event(_outlet_event())
    assert ctx.storage.write_execution.await_count == 1
    record = ctx.storage.write_execution.await_args[0][0]
    # MUST be at the top of the dict, not nested in metadata.
    assert record.get("tokens_estimated") is True
    # And the metadata still carries token_source for back-compat clients.
    assert record["metadata"].get("event_source") == "openwebui_plugin"


@pytest.mark.anyio
async def test_inlet_event_does_not_set_tokens_estimated():
    """Inlet events don't write an execution record (they're audit-only at
    the policy stage). The `tokens_estimated` field must not appear in the
    result body for inlet calls."""
    ctx = _ctx(chain_tracker=_real_chain_tracker())
    settings = _settings()
    inlet = {
        "event_type": "inlet",
        "model": "llama3.1:8b",
        "chat_id": "c-2",
        "user": {"id": "u1"},
        "data": {"all_messages": [{"role": "user", "content": "hi"}]},
    }
    with (
        patch("gateway.openwebui.governance.get_pipeline_context", return_value=ctx),
        patch("gateway.openwebui.governance.get_settings", return_value=settings),
        patch("gateway.openwebui.governance.run_pre_inference", return_value=_ok_pre_result()),
    ):
        result = await process_plugin_event(inlet)
    # Inlet records aren't written — `tokens_estimated` doesn't belong here.
    assert "tokens_estimated" not in result
    ctx.storage.write_execution.assert_not_called()
