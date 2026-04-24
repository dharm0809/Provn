"""Event merger tests."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pytest

from gateway.connections.builder import build_events


anyio_backend = pytest.fixture(params=["asyncio"])(lambda request: request.param)


@dataclass
class FakeCtx:
    walacor_client: Any = None
    content_analyzers: list = field(default_factory=list)
    resource_monitor: Any = None
    intelligence_worker: Any = None
    lineage_reader: Any = None


def _clear_module_deques():
    from gateway.pipeline.tool_executor import _tool_exception_log
    from gateway.pipeline.forwarder import _stream_interruption_log
    _tool_exception_log.clear()
    _stream_interruption_log.clear()


@pytest.mark.anyio
async def test_events_empty_when_no_deques(anyio_backend):
    _clear_module_deques()
    assert await build_events(FakeCtx()) == []


@pytest.mark.anyio
async def test_events_sorted_newest_first(anyio_backend):
    _clear_module_deques()
    from gateway.pipeline.tool_executor import _tool_exception_log
    from gateway.pipeline.forwarder import _stream_interruption_log
    now = time.time()
    _tool_exception_log.append((now - 30, "web_search", "timeout"))
    _stream_interruption_log.append((now - 5, "ollama", "closed"))
    _tool_exception_log.append((now - 1, "mcp_x", "oops"))
    events = await build_events(FakeCtx())
    tss = [e["ts"] for e in events]
    assert tss == sorted(tss, reverse=True)
    assert events[0]["subsystem"] in ("tool_loop", "streaming")
    _clear_module_deques()


@pytest.mark.anyio
async def test_events_capped_at_50(anyio_backend):
    _clear_module_deques()
    from gateway.pipeline.tool_executor import _tool_exception_log
    now = time.time()
    for i in range(60):
        _tool_exception_log.append((now - i, f"t{i}", "e"))
    events = await build_events(FakeCtx())
    assert len(events) <= 50
    _clear_module_deques()


@pytest.mark.anyio
async def test_events_subsystem_tags_correct(anyio_backend):
    _clear_module_deques()

    class _WC:
        def __init__(self):
            self._delivery_log = deque()
            self._delivery_log.append((time.time(), "write", False, "boom"))

    events = await build_events(FakeCtx(walacor_client=_WC()))
    assert any(e["subsystem"] == "walacor_delivery" for e in events)
    for e in events:
        assert e["session_id"] is None
        assert e["execution_id"] is None
        assert "attributes" in e
