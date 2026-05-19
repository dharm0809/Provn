"""DeliveryWorker Walacor-sink mode — the production fix for the
request-path/Walacor coupling.

In Walacor-backed deployments the request path now writes ONLY to the
local WAL (see main._init_storage `decoupled`); a single bounded
DeliveryWorker drains undelivered WAL rows to the Walacor backend with
the same backoff / batch-error-budget / DLQ machinery the control-plane
path uses. These tests pin that sink-mode contract:

* successful sink write -> WAL row marked delivered
* event_type dispatch: tool_call -> write_tool_event, else write_execution
* sink failure -> NOT delivered, retried, batch budget trips (backoff),
  DLQ after max retries
* poisoned record_json -> dead-lettered (never retried), queue not starved
* sink=None still uses the control-plane path (backward compatible)
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import get_settings
from gateway.wal.delivery_worker import DeliveryWorker


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _settings_isolation(monkeypatch):
    monkeypatch.delenv("WALACOR_CONTROL_PLANE_URL", raising=False)
    monkeypatch.setenv("WALACOR_WAL_DELIVERY_MAX_RETRIES", "3")
    monkeypatch.setenv("WALACOR_WAL_DELIVERY_BATCH_ERROR_BUDGET", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_wal(rows):
    wal = MagicMock()
    wal.get_undelivered = MagicMock(return_value=rows)
    wal.mark_delivered = MagicMock()
    wal.mark_dead_lettered = MagicMock()
    wal.purge_delivered = MagicMock(return_value=0)
    wal.purge_attempts = MagicMock(return_value=0)
    return wal


def _row(eid: str, body: dict) -> tuple[str, str, str]:
    return (eid, json.dumps(body), "2026-01-01T00:00:00Z")


def _sink(*, ok: bool = True, raises: bool = False) -> MagicMock:
    s = MagicMock()
    if raises:
        s.write_execution = AsyncMock(side_effect=RuntimeError("walacor down"))
        s.write_tool_event = AsyncMock(side_effect=RuntimeError("walacor down"))
    else:
        s.write_execution = AsyncMock(return_value=ok)
        s.write_tool_event = AsyncMock(return_value=ok)
    return s


@pytest.mark.anyio
async def test_sink_success_marks_delivered():
    wal = _fake_wal([_row("e1", {"execution_id": "e1"})])
    sink = _sink(ok=True)
    w = DeliveryWorker(wal, sink=sink)

    unhealthy = await w._deliver_batch_walacor()

    assert unhealthy is False
    sink.write_execution.assert_awaited_once()
    wal.mark_delivered.assert_called_once_with("e1")
    wal.mark_dead_lettered.assert_not_called()


@pytest.mark.anyio
async def test_event_type_dispatch():
    wal = _fake_wal([
        _row("e1", {"execution_id": "e1"}),
        _row("t1", {"event_id": "t1", "event_type": "tool_call"}),
    ])
    sink = _sink(ok=True)
    w = DeliveryWorker(wal, sink=sink)

    await w._deliver_batch_walacor()

    sink.write_execution.assert_awaited_once()      # the execution row
    sink.write_tool_event.assert_awaited_once()     # the tool_call row
    assert {c.args[0] for c in wal.mark_delivered.call_args_list} == {"e1", "t1"}


@pytest.mark.anyio
async def test_sink_failure_not_delivered_and_backs_off():
    # 3 failing rows, batch_error_budget=2 -> trips, signals backoff.
    wal = _fake_wal([_row(f"e{i}", {"execution_id": f"e{i}"}) for i in range(3)])
    sink = _sink(ok=False)
    w = DeliveryWorker(wal, sink=sink)

    unhealthy = await w._deliver_batch_walacor()

    assert unhealthy is True                  # budget exhausted -> _loop backs off
    wal.mark_delivered.assert_not_called()    # nothing delivered
    assert w._attempt_counts                  # retry counters recorded


@pytest.mark.anyio
async def test_sink_raise_is_treated_as_transient():
    wal = _fake_wal([_row("e1", {"execution_id": "e1"})])
    sink = _sink(raises=True)
    w = DeliveryWorker(wal, sink=sink)

    unhealthy = await w._deliver_batch_walacor()

    assert unhealthy is False                  # 1 error < budget(2)
    wal.mark_delivered.assert_not_called()
    assert w._attempt_counts.get("e1") == 1    # counted for retry, not crashed


@pytest.mark.anyio
async def test_dlq_after_max_retries():
    wal = _fake_wal([_row("stuck", {"execution_id": "stuck"})])
    sink = _sink(ok=False)
    w = DeliveryWorker(wal, sink=sink)
    # max_retries=3 -> 3rd attempt promotes to DLQ.
    for _ in range(3):
        wal.get_undelivered.return_value = [_row("stuck", {"execution_id": "stuck"})]
        await w._deliver_batch_walacor()

    wal.mark_dead_lettered.assert_called_once()
    assert wal.mark_dead_lettered.call_args.args[0] == "stuck"


@pytest.mark.anyio
async def test_poisoned_json_dead_lettered_not_retried():
    wal = _fake_wal([("bad", "{not valid json", "2026-01-01T00:00:00Z")])
    sink = _sink(ok=True)
    w = DeliveryWorker(wal, sink=sink)

    unhealthy = await w._deliver_batch_walacor()

    assert unhealthy is False
    wal.mark_dead_lettered.assert_called_once()
    assert wal.mark_dead_lettered.call_args.args[0] == "bad"
    sink.write_execution.assert_not_awaited()


@pytest.mark.anyio
async def test_sink_none_is_backward_compatible():
    """Default (no sink) must NOT use the Walacor path — _loop picks the
    control-plane _deliver_batch, preserving existing behaviour."""
    wal = _fake_wal([])
    w = DeliveryWorker(wal)            # sink=None
    assert w._sink is None
    # control-plane batch on an empty queue is a no-op and returns False.
    assert await w._deliver_batch() is False
