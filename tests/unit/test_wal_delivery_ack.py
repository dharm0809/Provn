"""Regression tests for the StorageRouter -> WAL delivery acknowledgement path.

Covers fixes A1 and A5:

* A1 — When the Walacor backend succeeds the local SQLite WAL must be
  marked delivered, otherwise records pile up forever in deployments
  that do not run a control-plane DeliveryWorker.

* A5 — Tool events share the ``wal_records`` table / ``delivered``
  column with executions, so the same ack path must cover them.

These tests use a real WALWriter against a tmp SQLite file (so we can
inspect the ``delivered`` column directly) and stub Walacor with a
MagicMock — same shape as ``tests/unit/test_storage_router.py``.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.storage.router import StorageRouter
from gateway.storage.wal_backend import WALBackend
from gateway.storage.walacor_backend import WalacorBackend
from gateway.wal.writer import WALWriter


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _make_walacor_client(*, fail: bool = False) -> MagicMock:
    client = MagicMock()
    if fail:
        client.write_execution = AsyncMock(side_effect=RuntimeError("walacor 500"))
        client.write_tool_event = AsyncMock(side_effect=RuntimeError("walacor 500"))
    else:
        client.write_execution = AsyncMock()
        client.write_tool_event = AsyncMock()
    client.write_attempt = AsyncMock()
    client.close = AsyncMock()
    return client


def _undelivered_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM wal_records WHERE delivered = 0"
        ).fetchone()[0]
    finally:
        conn.close()


def _delivered_for(db_path: str, execution_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT delivered FROM wal_records WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return bool(row and row[0] == 1)
    finally:
        conn.close()


@pytest.fixture
def wal_writer(tmp_path):
    """Real WALWriter on a fresh tmp SQLite file; cleaned up on teardown."""
    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    # Don't start the dedicated thread — these tests use the synchronous
    # write_durable / write_tool_event API which goes through self._conn.
    yield writer
    writer.close()


@pytest.mark.anyio
async def test_walacor_success_marks_wal_delivered(wal_writer, tmp_path):
    """A1: when Walacor accepts the write, local WAL row is delivered."""
    wal_backend = WALBackend(wal_writer)
    walacor = _make_walacor_client()
    router = StorageRouter([wal_backend, WalacorBackend(walacor)])

    # Plant a row that bypasses the dedicated thread so we can flush
    # synchronously and assert delivered=1 right after the write.
    wal_writer.write_durable({"execution_id": "exec-a1"})

    res = await router.write_execution({"execution_id": "exec-a1"})

    assert "walacor" in res.succeeded
    # The router fans out to WAL too, which only enqueues — but the row
    # we planted above is the one that has to flip delivered=1.
    assert _delivered_for(wal_writer._path, "exec-a1"), (
        "WAL row must be marked delivered after Walacor success"
    )
    assert _undelivered_count(wal_writer._path) == 0


@pytest.mark.anyio
async def test_walacor_failure_keeps_wal_pending(wal_writer):
    """A1: when Walacor fails, WAL row stays pending so retries can drain it."""
    wal_backend = WALBackend(wal_writer)
    walacor = _make_walacor_client(fail=True)
    router = StorageRouter([wal_backend, WalacorBackend(walacor)])

    wal_writer.write_durable({"execution_id": "exec-fail"})

    res = await router.write_execution({"execution_id": "exec-fail"})

    assert "walacor" in res.failed
    assert not _delivered_for(wal_writer._path, "exec-fail"), (
        "WAL row must NOT be marked delivered when Walacor fails"
    )
    assert _undelivered_count(wal_writer._path) == 1


@pytest.mark.anyio
async def test_wal_only_deployment_no_ack(wal_writer):
    """A1: with no Walacor backend, the router does not pre-emptively ack."""
    wal_backend = WALBackend(wal_writer)
    router = StorageRouter([wal_backend])

    wal_writer.write_durable({"execution_id": "exec-wal-only"})
    res = await router.write_execution({"execution_id": "exec-wal-only"})

    assert res.succeeded == ["wal"]
    # WAL-only deployments are expected to leave rows pending until
    # either the DeliveryWorker (when control plane is configured) or
    # a future durable sink acknowledges them.
    assert not _delivered_for(wal_writer._path, "exec-wal-only")


@pytest.mark.anyio
async def test_tool_event_walacor_success_marks_delivered(wal_writer):
    """A5: tool events share the delivered column — same ack path applies."""
    wal_backend = WALBackend(wal_writer)
    walacor = _make_walacor_client()
    router = StorageRouter([wal_backend, WalacorBackend(walacor)])

    # Plant the tool-event row directly so we can flush synchronously.
    wal_writer.write_tool_event({"event_id": "tool-1"})

    await router.write_tool_event({"event_id": "tool-1"})

    # Tool events are keyed by event_id in the execution_id column.
    assert _delivered_for(wal_writer._path, "tool-1"), (
        "Tool-event WAL row must be marked delivered after Walacor success"
    )


@pytest.mark.anyio
async def test_tool_event_walacor_failure_keeps_pending(wal_writer):
    """A5: tool events stay pending when Walacor fails, same as executions."""
    wal_backend = WALBackend(wal_writer)
    walacor = _make_walacor_client(fail=True)
    router = StorageRouter([wal_backend, WalacorBackend(walacor)])

    wal_writer.write_tool_event({"event_id": "tool-fail"})
    await router.write_tool_event({"event_id": "tool-fail"})

    assert not _delivered_for(wal_writer._path, "tool-fail")


@pytest.mark.anyio
async def test_walbackend_mark_delivered_helper(wal_writer):
    """A1: the explicit WALBackend.mark_delivered hook works on its own."""
    backend = WALBackend(wal_writer)
    wal_writer.write_durable({"execution_id": "exec-helper"})
    assert not _delivered_for(wal_writer._path, "exec-helper")

    backend.mark_delivered("exec-helper")
    assert _delivered_for(wal_writer._path, "exec-helper")


@pytest.mark.anyio
async def test_walbackend_mark_delivered_missing_writer_no_raise():
    """A1: mark_delivered must never raise — completeness path is fire-and-forget."""
    # Construct a backend where the writer's mark_delivered explodes.
    bad_writer = MagicMock()
    bad_writer.mark_delivered.side_effect = RuntimeError("disk gone")
    backend = WALBackend(bad_writer)
    # Should swallow the exception.
    backend.mark_delivered("exec-x")
