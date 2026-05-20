"""Write-time idempotency for DeliveryWorker Walacor sink.

PR #61 added a read-side dedup band-aid for the "ack-lost-on-return-path"
duplicate class: the write actually landed in Walacor but the worker saw
a transport failure, retried, and wrote the same record_id twice.

This module pins the *structural* fix: when a write fails, the worker
probes Walacor for the record's existence BEFORE scheduling a retry. If
the row is already there (ack lost), it marks delivered and skips retry —
no duplicate. If absent (write truly didn't land), the retry path runs
as before. The read-side dedup remains as belt-and-braces.
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
    monkeypatch.setenv("WALACOR_WAL_DELIVERY_BATCH_ERROR_BUDGET", "5")
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


def _sink_with_probe(*, write_ok: bool, exists: bool, write_raises: bool = False):
    """Sink that fails the write but answers the existence probe.

    Exposes ``execution_exists`` / ``tool_event_exists`` directly so the
    worker's ``_walacor_client_for_probe`` picks the sink itself as the
    probe surface (no need to introspect a wrapped ``_client``).
    """
    s = MagicMock()
    if write_raises:
        s.write_execution = AsyncMock(side_effect=RuntimeError("walacor down"))
        s.write_tool_event = AsyncMock(side_effect=RuntimeError("walacor down"))
    else:
        s.write_execution = AsyncMock(return_value=write_ok)
        s.write_tool_event = AsyncMock(return_value=write_ok)
    s.execution_exists = AsyncMock(return_value=exists)
    s.tool_event_exists = AsyncMock(return_value=exists)
    return s


@pytest.mark.anyio
async def test_ack_lost_existence_probe_prevents_duplicate_write():
    """Write returns False (ack lost) BUT the record IS already in Walacor.

    Expect: existence probe answers True, worker marks delivered, NO retry
    counter recorded, NO second write attempt.
    """
    wal = _fake_wal([_row("e1", {"execution_id": "e1", "record_id": "r-e1"})])
    sink = _sink_with_probe(write_ok=False, exists=True)
    w = DeliveryWorker(wal, sink=sink)

    unhealthy = await w._deliver_batch_walacor()

    # Write was attempted exactly once; existence probe was consulted.
    sink.write_execution.assert_awaited_once()
    sink.execution_exists.assert_awaited_once_with("r-e1")
    # Marked delivered — no duplicate retry will ever happen.
    wal.mark_delivered.assert_called_once_with("e1")
    # No retry counter recorded — the failure was reclassified as success.
    assert "e1" not in w._attempt_counts
    # Batch healthy — no need to back off on a phantom failure.
    assert unhealthy is False


@pytest.mark.anyio
async def test_ack_lost_existence_probe_works_when_write_raises():
    """Sink raises (TransportError-shaped) but record is in Walacor anyway."""
    wal = _fake_wal([_row("e1", {"execution_id": "e1", "record_id": "r-e1"})])
    sink = _sink_with_probe(write_ok=False, exists=True, write_raises=True)
    w = DeliveryWorker(wal, sink=sink)

    await w._deliver_batch_walacor()

    sink.execution_exists.assert_awaited_once_with("r-e1")
    wal.mark_delivered.assert_called_once_with("e1")
    assert "e1" not in w._attempt_counts


@pytest.mark.anyio
async def test_genuine_failure_probe_says_no_retry_scheduled():
    """Write fails AND Walacor confirms the record is not present.

    Expect: retry path runs as before — retry counter bumped, no delivery,
    no DLQ promotion yet (under max_retries).
    """
    wal = _fake_wal([_row("e1", {"execution_id": "e1", "record_id": "r-e1"})])
    sink = _sink_with_probe(write_ok=False, exists=False)
    w = DeliveryWorker(wal, sink=sink)

    await w._deliver_batch_walacor()

    sink.write_execution.assert_awaited_once()
    sink.execution_exists.assert_awaited_once_with("r-e1")
    # Not delivered, not dead-lettered — retry counter recorded for next pass.
    wal.mark_delivered.assert_not_called()
    wal.mark_dead_lettered.assert_not_called()
    assert w._attempt_counts.get("e1") == 1


@pytest.mark.anyio
async def test_probe_failure_falls_through_to_retry():
    """If the existence probe itself errors, treat as 'unknown' → retry path.

    The read-side dedup (PR #61) is the belt-and-braces fallback for any
    duplicate that survives.
    """
    wal = _fake_wal([_row("e1", {"execution_id": "e1", "record_id": "r-e1"})])
    sink = _sink_with_probe(write_ok=False, exists=False)
    sink.execution_exists = AsyncMock(side_effect=RuntimeError("probe network error"))
    w = DeliveryWorker(wal, sink=sink)

    await w._deliver_batch_walacor()

    # Probe was attempted, raised, swallowed; retry counter bumped.
    sink.execution_exists.assert_awaited_once()
    wal.mark_delivered.assert_not_called()
    assert w._attempt_counts.get("e1") == 1


@pytest.mark.anyio
async def test_successful_write_skips_probe_entirely():
    """Happy path: probe is only consulted on a failed write."""
    wal = _fake_wal([_row("e1", {"execution_id": "e1", "record_id": "r-e1"})])
    sink = _sink_with_probe(write_ok=True, exists=True)
    w = DeliveryWorker(wal, sink=sink)

    await w._deliver_batch_walacor()

    sink.write_execution.assert_awaited_once()
    sink.execution_exists.assert_not_awaited()  # never reached
    wal.mark_delivered.assert_called_once_with("e1")


@pytest.mark.anyio
async def test_tool_event_uses_event_id_for_probe():
    """Tool events are keyed on event_id, not record_id (distinct schemas)."""
    body = {"event_id": "ev-1", "event_type": "tool_call"}
    wal = _fake_wal([_row("t1", body)])
    sink = _sink_with_probe(write_ok=False, exists=True)
    w = DeliveryWorker(wal, sink=sink)

    await w._deliver_batch_walacor()

    sink.write_tool_event.assert_awaited_once()
    sink.tool_event_exists.assert_awaited_once_with("ev-1")
    sink.execution_exists.assert_not_awaited()
    wal.mark_delivered.assert_called_once_with("t1")


@pytest.mark.anyio
async def test_probe_resolves_through_wrapped_walacor_client():
    """Production sink shape: ``WalacorBackend`` exposes the client as ``_client``.

    The worker must find the probe methods on ``sink._client`` when the
    sink itself doesn't carry them — that's the prod wiring.
    """
    inner = MagicMock()
    inner.execution_exists = AsyncMock(return_value=True)
    inner.tool_event_exists = AsyncMock(return_value=False)

    sink = MagicMock(spec=["write_execution", "write_tool_event", "_client"])
    sink.write_execution = AsyncMock(return_value=False)
    sink.write_tool_event = AsyncMock(return_value=False)
    sink._client = inner

    wal = _fake_wal([_row("e1", {"execution_id": "e1", "record_id": "r-e1"})])
    w = DeliveryWorker(wal, sink=sink)

    await w._deliver_batch_walacor()

    inner.execution_exists.assert_awaited_once_with("r-e1")
    wal.mark_delivered.assert_called_once_with("e1")


@pytest.mark.anyio
async def test_missing_record_id_skips_probe_falls_to_retry():
    """If the body has no record_id, we can't probe — go straight to retry."""
    wal = _fake_wal([_row("e1", {"execution_id": "e1"})])  # no record_id
    sink = _sink_with_probe(write_ok=False, exists=True)
    w = DeliveryWorker(wal, sink=sink)

    await w._deliver_batch_walacor()

    sink.execution_exists.assert_not_awaited()
    wal.mark_delivered.assert_not_called()
    assert w._attempt_counts.get("e1") == 1
