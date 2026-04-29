"""Tests that WALBackend delegates writes via the WALWriter enqueue API (dedicated thread)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.storage.wal_backend import WALBackend


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture
def mock_writer():
    writer = MagicMock()
    writer.enqueue_write_execution = MagicMock()
    writer.enqueue_write_attempt = MagicMock()
    writer.enqueue_write_tool_event = MagicMock()
    writer.close = MagicMock()
    return writer


@pytest.fixture
def backend(mock_writer):
    return WALBackend(mock_writer)


class TestWriteExecutionUsesEnqueue:
    @pytest.mark.anyio
    async def test_write_execution_calls_enqueue(self, backend, mock_writer):
        record = {"execution_id": "exec-1", "data": "test"}
        result = await backend.write_execution(record)

        assert result is True
        mock_writer.enqueue_write_execution.assert_called_once_with(record)

    @pytest.mark.anyio
    async def test_write_execution_failure_returns_false(self, backend, mock_writer):
        record = {"execution_id": "exec-fail"}
        mock_writer.enqueue_write_execution.side_effect = RuntimeError("disk full")
        result = await backend.write_execution(record)

        assert result is False


class TestWriteAttemptUsesEnqueue:
    @pytest.mark.anyio
    async def test_write_attempt_calls_enqueue(self, backend, mock_writer):
        record = {"request_id": "req-1", "status": 200}
        await backend.write_attempt(record)

        mock_writer.enqueue_write_attempt.assert_called_once_with(**record)

    @pytest.mark.anyio
    async def test_write_attempt_failure_does_not_raise(self, backend, mock_writer):
        record = {"request_id": "req-fail"}
        mock_writer.enqueue_write_attempt.side_effect = RuntimeError("db locked")
        # Should not raise — exception is caught and logged
        await backend.write_attempt(record)


class TestWriteToolEventUsesEnqueue:
    @pytest.mark.anyio
    async def test_write_tool_event_calls_enqueue(self, backend, mock_writer):
        record = {"event_id": "evt-1", "tool_name": "web_search"}
        await backend.write_tool_event(record)

        mock_writer.enqueue_write_tool_event.assert_called_once_with(record)

    @pytest.mark.anyio
    async def test_write_tool_event_failure_does_not_raise(self, backend, mock_writer):
        record = {"event_id": "evt-fail"}
        mock_writer.enqueue_write_tool_event.side_effect = RuntimeError("write error")
        # Should not raise — exception is caught and logged
        await backend.write_tool_event(record)


class TestSyncMethodsNotCalledDirectly:
    """Verify that legacy sync writer methods are NOT called directly from WALBackend."""

    @pytest.mark.anyio
    async def test_write_durable_not_called_directly(self, backend, mock_writer):
        record = {"execution_id": "exec-direct"}
        mock_writer.write_durable = MagicMock()
        await backend.write_execution(record)

        # The legacy sync method should NOT have been called directly
        mock_writer.write_durable.assert_not_called()

    @pytest.mark.anyio
    async def test_write_attempt_sync_not_called_directly(self, backend, mock_writer):
        record = {"request_id": "req-direct", "status": 200}
        mock_writer.write_attempt = MagicMock()
        await backend.write_attempt(record)

        # The legacy sync method should NOT have been called directly
        mock_writer.write_attempt.assert_not_called()

    @pytest.mark.anyio
    async def test_write_tool_event_sync_not_called_directly(self, backend, mock_writer):
        record = {"event_id": "evt-direct"}
        mock_writer.write_tool_event = MagicMock()
        await backend.write_tool_event(record)

        # The legacy sync method should NOT have been called directly
        mock_writer.write_tool_event.assert_not_called()
