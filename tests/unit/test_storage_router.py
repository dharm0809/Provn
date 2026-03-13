"""Unit tests for StorageRouter fan-out logic."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from gateway.storage.backend import StorageBackend
from gateway.storage.router import StorageRouter, WriteResult


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


class FakeBackend:
    """Minimal StorageBackend for testing."""

    def __init__(self, name: str, fail_execution: bool = False, fail_attempt: bool = False, fail_tool: bool = False):
        self._name = name
        self._fail_execution = fail_execution
        self._fail_attempt = fail_attempt
        self._fail_tool = fail_tool
        self.closed = False
        self.executions: list[dict] = []
        self.attempts: list[dict] = []
        self.tool_events: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    async def write_execution(self, record: dict) -> bool:
        if self._fail_execution:
            raise RuntimeError("execution write failed")
        self.executions.append(record)
        return True

    async def write_attempt(self, record: dict) -> None:
        if self._fail_attempt:
            raise RuntimeError("attempt write failed")
        self.attempts.append(record)

    async def write_tool_event(self, record: dict) -> None:
        if self._fail_tool:
            raise RuntimeError("tool event write failed")
        self.tool_events.append(record)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_write_execution_fan_out_both_succeed():
    b1 = FakeBackend("wal")
    b2 = FakeBackend("walacor")
    router = StorageRouter([b1, b2])
    result = await router.write_execution({"execution_id": "e1"})
    assert result.succeeded == ["wal", "walacor"]
    assert result.failed == []
    assert b1.executions == [{"execution_id": "e1"}]
    assert b2.executions == [{"execution_id": "e1"}]


@pytest.mark.anyio
async def test_write_execution_one_fails():
    b1 = FakeBackend("wal")
    b2 = FakeBackend("walacor", fail_execution=True)
    router = StorageRouter([b1, b2])
    result = await router.write_execution({"execution_id": "e2"})
    assert result.succeeded == ["wal"]
    assert result.failed == ["walacor"]
    assert b1.executions == [{"execution_id": "e2"}]


@pytest.mark.anyio
async def test_write_execution_all_fail():
    b1 = FakeBackend("wal", fail_execution=True)
    b2 = FakeBackend("walacor", fail_execution=True)
    router = StorageRouter([b1, b2])
    result = await router.write_execution({"execution_id": "e3"})
    assert result.succeeded == []
    assert result.failed == ["wal", "walacor"]


@pytest.mark.anyio
async def test_write_attempt_fire_and_forget():
    b1 = FakeBackend("wal")
    b2 = FakeBackend("walacor", fail_attempt=True)
    router = StorageRouter([b1, b2])
    # Should NOT raise despite b2 failing
    await router.write_attempt({"request_id": "r1"})
    assert b1.attempts == [{"request_id": "r1"}]


@pytest.mark.anyio
async def test_write_tool_event_fire_and_forget():
    b1 = FakeBackend("wal", fail_tool=True)
    b2 = FakeBackend("walacor")
    router = StorageRouter([b1, b2])
    await router.write_tool_event({"event_id": "t1"})
    assert b2.tool_events == [{"event_id": "t1"}]


@pytest.mark.anyio
async def test_close_all_backends():
    b1 = FakeBackend("wal")
    b2 = FakeBackend("walacor")
    router = StorageRouter([b1, b2])
    await router.close()
    assert b1.closed is True
    assert b2.closed is True


@pytest.mark.anyio
async def test_empty_backends_list():
    router = StorageRouter([])
    result = await router.write_execution({"execution_id": "e4"})
    assert result.succeeded == []
    assert result.failed == []
    await router.write_attempt({"request_id": "r2"})  # no-op, no error
    await router.write_tool_event({"event_id": "t2"})  # no-op, no error


def test_backend_names():
    b1 = FakeBackend("wal")
    b2 = FakeBackend("walacor")
    router = StorageRouter([b1, b2])
    assert router.backend_names == ["wal", "walacor"]
