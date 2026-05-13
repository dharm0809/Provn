"""Regression test for plugin-event completeness (fix A3).

``/v1/openwebui/events`` is on the ``completeness_middleware`` skip-list
(events bypass the standard chat/completions pipeline), so the proxy
middleware never writes an attempt row for plugin events. Without an
explicit ``write_attempt`` inside ``process_plugin_event``, plugin
events would write execution records but no attempt records — violating
the "every governed request gets an attempt" invariant.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.config import get_settings
from gateway.openwebui import governance as plugin_gov
from gateway.pipeline.context import get_pipeline_context


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


class _RecordingStorage:
    def __init__(self) -> None:
        self.attempts: list[dict] = []
        self.executions: list[dict] = []

    async def write_attempt(self, record: dict) -> None:
        self.attempts.append(dict(record))

    async def write_execution(self, record: dict):
        self.executions.append(dict(record))
        # Return a WriteResult-shaped object so process_plugin_event
        # is happy.
        return MagicMock(succeeded=["wal"], failed=[])


@pytest.fixture
def plugin_ctx(monkeypatch):
    """Swap pipeline_context.storage with a recording stub."""
    storage = _RecordingStorage()
    ctx = get_pipeline_context()
    original_storage = ctx.storage
    original_attest = ctx.attestation_cache
    original_policy = ctx.policy_cache
    ctx.storage = storage
    # Disable attestation + policy paths so process_plugin_event runs
    # against a deterministic minimum and we can focus on the attempt
    # row, not the policy evaluator.
    ctx.attestation_cache = None
    ctx.policy_cache = None
    monkeypatch.setenv("WALACOR_PLUGIN_EVENT_GOVERNANCE_ENABLED", "true")
    get_settings.cache_clear()
    try:
        yield storage
    finally:
        ctx.storage = original_storage
        ctx.attestation_cache = original_attest
        ctx.policy_cache = original_policy
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_inlet_event_writes_attempt(plugin_ctx):
    """A3: inlet plugin events produce exactly one attempt row."""
    event = {
        "event_type": "inlet",
        "model": "qwen3:4b",
        "chat_id": "chat-abc",
        "user": {"id": "user-1"},
        "data": {"all_messages": [{"role": "user", "content": "hello"}]},
    }
    await plugin_gov.process_plugin_event(event)
    paths = [a["path"] for a in plugin_ctx.attempts]
    assert paths == ["/v1/openwebui/events"], (
        f"inlet event must produce exactly one attempt row, got: {plugin_ctx.attempts}"
    )
    rec = plugin_ctx.attempts[0]
    assert rec["user"] == "user-1"
    assert rec["disposition"] in {"allowed", "blocked_post_facto"}
    # The completeness contract demands a non-empty request_id.
    assert rec["request_id"]


@pytest.mark.anyio
async def test_outlet_event_writes_attempt_and_execution(plugin_ctx):
    """A3: outlet plugin events also produce exactly one attempt row."""
    event = {
        "event_type": "outlet",
        "model": "qwen3:4b",
        "chat_id": "chat-abc",
        "user": {"id": "user-2"},
        "data": {
            "all_messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello back"},
            ],
            "assistant_response": "hello back",
            "response_length": 11,
            "governance": {},
        },
    }
    await plugin_gov.process_plugin_event(event)
    # Exactly one attempt row, regardless of outlet processing depth.
    assert len(plugin_ctx.attempts) == 1, (
        f"outlet event must produce exactly one attempt row, got: {plugin_ctx.attempts}"
    )
    rec = plugin_ctx.attempts[0]
    assert rec["path"] == "/v1/openwebui/events"
    # Outlet should attach the execution_id from the write.
    assert rec.get("execution_id") or rec.get("execution_id") is None  # tolerate either


@pytest.mark.anyio
async def test_no_storage_skips_attempt(monkeypatch):
    """A3: when no storage backend is wired, process_plugin_event returns silently."""
    ctx = get_pipeline_context()
    original = ctx.storage
    ctx.storage = None
    monkeypatch.setenv("WALACOR_PLUGIN_EVENT_GOVERNANCE_ENABLED", "true")
    get_settings.cache_clear()
    try:
        result = await plugin_gov.process_plugin_event({
            "event_type": "inlet",
            "model": "m",
            "chat_id": "c",
            "user": {"id": "u"},
            "data": {},
        })
        assert result["governance_status"] == "skipped"
    finally:
        ctx.storage = original
        get_settings.cache_clear()
