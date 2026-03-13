"""Tests that WALBackend delegates sync SQLite calls via asyncio.to_thread."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.storage.wal_backend import WALBackend


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture
def mock_writer():
    writer = MagicMock()
    writer.write_and_fsync = MagicMock()
    writer.write_attempt = MagicMock()
    writer.write_tool_event = MagicMock()
    writer.close = MagicMock()
    return writer


@pytest.fixture
def backend(mock_writer):
    return WALBackend(mock_writer)


class TestWriteExecutionUsesToThread:
    @pytest.mark.anyio
    async def test_write_execution_calls_to_thread(self, backend, mock_writer):
        record = {"execution_id": "exec-1", "data": "test"}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = None
            result = await backend.write_execution(record)

        assert result is True
        mock_to_thread.assert_awaited_once_with(mock_writer.write_and_fsync, record)

    @pytest.mark.anyio
    async def test_write_execution_failure_returns_false(self, backend):
        record = {"execution_id": "exec-fail"}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.side_effect = RuntimeError("disk full")
            result = await backend.write_execution(record)

        assert result is False


class TestWriteAttemptUsesToThread:
    @pytest.mark.anyio
    async def test_write_attempt_calls_to_thread(self, backend, mock_writer):
        record = {"request_id": "req-1", "status": 200}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = None
            await backend.write_attempt(record)

        mock_to_thread.assert_awaited_once_with(mock_writer.write_attempt, **record)

    @pytest.mark.anyio
    async def test_write_attempt_failure_does_not_raise(self, backend):
        record = {"request_id": "req-fail"}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.side_effect = RuntimeError("db locked")
            # Should not raise — exception is caught and logged
            await backend.write_attempt(record)


class TestWriteToolEventUsesToThread:
    @pytest.mark.anyio
    async def test_write_tool_event_calls_to_thread(self, backend, mock_writer):
        record = {"event_id": "evt-1", "tool_name": "web_search"}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = None
            await backend.write_tool_event(record)

        mock_to_thread.assert_awaited_once_with(mock_writer.write_tool_event, record)

    @pytest.mark.anyio
    async def test_write_tool_event_failure_does_not_raise(self, backend):
        record = {"event_id": "evt-fail"}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.side_effect = RuntimeError("write error")
            # Should not raise — exception is caught and logged
            await backend.write_tool_event(record)


class TestSyncMethodsNotCalledDirectly:
    """Verify that the sync writer methods are NOT called directly (only via to_thread)."""

    @pytest.mark.anyio
    async def test_write_and_fsync_not_called_directly(self, backend, mock_writer):
        record = {"execution_id": "exec-direct"}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = None
            await backend.write_execution(record)

        # The sync method should NOT have been called directly
        mock_writer.write_and_fsync.assert_not_called()

    @pytest.mark.anyio
    async def test_write_attempt_not_called_directly(self, backend, mock_writer):
        record = {"request_id": "req-direct", "status": 200}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = None
            await backend.write_attempt(record)

        # The sync method should NOT have been called directly
        mock_writer.write_attempt.assert_not_called()

    @pytest.mark.anyio
    async def test_write_tool_event_not_called_directly(self, backend, mock_writer):
        record = {"event_id": "evt-direct"}
        with patch("gateway.storage.wal_backend.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = None
            await backend.write_tool_event(record)

        # The sync method should NOT have been called directly
        mock_writer.write_tool_event.assert_not_called()
