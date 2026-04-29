"""Failure-mode tests for the dual-write StorageRouter.

The router fans writes out to every registered backend independently
(WAL + Walacor in production). Per the architectural invariant
documented in CLAUDE.md, dual-write is BOTH/AND — never either/or — so
the per-backend outcome must be visible to callers and a backend
failure must NEVER be silently swallowed at the log level.

These tests pin the exact surface for the four scenarios called out
in the dual-write spec:

  1. WAL succeeds, Walacor fails  → succeeded=["wal"], failed=["walacor"]
  2. WAL fails,    Walacor succeeds → succeeded=["walacor"], failed=["wal"]
  3. Both fail                       → succeeded=[], failed=both
  4. Tool events                     → fan-out matches the same shape

NOTE on current semantics: `write_execution` returns a `WriteResult`;
it does NOT raise even when every backend fails. Callers therefore
must treat an empty `succeeded` list as the "all-fail" signal. If the
router is later changed to raise on total failure, the assertions
below should be updated.
"""

from __future__ import annotations

import logging

import pytest

from gateway.storage.router import StorageRouter, WriteResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeBackend:
    """Bare-minimum StorageBackend duck for testing."""

    def __init__(
        self,
        name: str,
        *,
        execution_raises: Exception | None = None,
        tool_event_raises: Exception | None = None,
    ) -> None:
        self._name = name
        self._execution_raises = execution_raises
        self._tool_event_raises = tool_event_raises
        self.executions: list[dict] = []
        self.tool_events: list[dict] = []
        self.attempts: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    async def write_execution(self, record: dict) -> bool:
        if self._execution_raises is not None:
            raise self._execution_raises
        self.executions.append(record)
        return True

    async def write_attempt(self, record: dict) -> None:
        self.attempts.append(record)

    async def write_tool_event(self, record: dict) -> None:
        if self._tool_event_raises is not None:
            raise self._tool_event_raises
        self.tool_events.append(record)

    async def close(self) -> None:
        return None


# ── Scenario 1: WAL succeeds, Walacor raises ────────────────────────────


@pytest.mark.anyio
async def test_walacor_failure_does_not_mask_wal_success(caplog):
    """WAL write succeeds, Walacor raises → router reports succeeded=['wal'],
    failed=['walacor'] AND logs the Walacor failure (so ops can see it)."""
    wal = _FakeBackend("wal")
    walacor = _FakeBackend("walacor", execution_raises=RuntimeError("Walacor 502"))
    router = StorageRouter([wal, walacor])

    with caplog.at_level(logging.ERROR, logger="gateway.storage.router"):
        result = await router.write_execution({"execution_id": "exec-1"})

    # WAL is authoritative — its success alone keeps the request "successful"
    assert "wal" in result.succeeded
    assert "walacor" in result.failed
    assert wal.executions == [{"execution_id": "exec-1"}]
    # Walacor never appended (raised before)
    assert walacor.executions == []

    # Walacor failure must be logged at ERROR — silent failures violate
    # the dual-write invariant.
    walacor_errors = [
        rec for rec in caplog.records
        if rec.levelno >= logging.ERROR and "walacor" in rec.getMessage().lower()
    ]
    assert walacor_errors, (
        "Expected an ERROR log when Walacor backend raised, "
        "got none — silent failure would mask anchor loss."
    )


# ── Scenario 2: WAL raises, Walacor succeeds ────────────────────────────


@pytest.mark.anyio
async def test_wal_failure_is_surfaced_clearly(caplog):
    """WAL raises, Walacor succeeds → router reports failed=['wal'].

    WAL is the authoritative local store; a WAL failure must be loud.
    """
    wal = _FakeBackend("wal", execution_raises=RuntimeError("disk full"))
    walacor = _FakeBackend("walacor")
    router = StorageRouter([wal, walacor])

    with caplog.at_level(logging.ERROR, logger="gateway.storage.router"):
        result = await router.write_execution({"execution_id": "exec-2"})

    assert result.succeeded == ["walacor"]
    assert result.failed == ["wal"]
    assert wal.executions == []  # raised before append
    assert walacor.executions == [{"execution_id": "exec-2"}]

    wal_errors = [
        rec for rec in caplog.records
        if rec.levelno >= logging.ERROR and "wal" in rec.getMessage().lower()
    ]
    assert wal_errors, (
        "WAL failure must be logged at ERROR — WAL is the authoritative "
        "local store; silent loss is unacceptable."
    )


# ── Scenario 3: Both backends raise ─────────────────────────────────────


@pytest.mark.anyio
async def test_both_backends_failing_reports_both_and_logs_all_fail(caplog):
    """Both backends raise → both appear in `failed`, succeeded is empty,
    and the router emits an 'ALL storage backends failed' rollup ERROR.

    TODO: revisit semantic — current router returns a WriteResult instead
    of raising even when EVERY backend fails. Callers must check
    `succeeded == []` to detect the catastrophic case. If we change the
    router to raise on total failure, update this test to `pytest.raises`.
    """
    wal = _FakeBackend("wal", execution_raises=RuntimeError("disk full"))
    walacor = _FakeBackend(
        "walacor", execution_raises=RuntimeError("Walacor unreachable")
    )
    router = StorageRouter([wal, walacor])

    with caplog.at_level(logging.ERROR, logger="gateway.storage.router"):
        result = await router.write_execution({"execution_id": "exec-3"})

    # Current contract: returns WriteResult; does not raise.
    assert isinstance(result, WriteResult)
    assert result.succeeded == []
    assert sorted(result.failed) == ["wal", "walacor"]

    rollup = [
        rec for rec in caplog.records
        if rec.levelno >= logging.ERROR
        and "ALL storage backends failed" in rec.getMessage()
    ]
    assert rollup, (
        "Expected an 'ALL storage backends failed' rollup log when "
        "every backend rejected the write."
    )


# ── Scenario 4: Tool events fan out the same way ────────────────────────


@pytest.mark.anyio
async def test_tool_event_fan_out_walacor_failure_does_not_mask_wal(caplog):
    """Tool event fan-out: WAL succeeds, Walacor raises → WAL still
    captured the event. write_tool_event is fire-and-forget (returns
    None) but a WARNING-or-higher log must be emitted on backend
    failure so the event isn't silently lost."""
    wal = _FakeBackend("wal")
    walacor = _FakeBackend(
        "walacor", tool_event_raises=RuntimeError("Walacor 502")
    )
    router = StorageRouter([wal, walacor])

    with caplog.at_level(logging.WARNING, logger="gateway.storage.router"):
        await router.write_tool_event({"event_id": "evt-1"})

    assert wal.tool_events == [{"event_id": "evt-1"}]
    assert walacor.tool_events == []

    walacor_warns = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING and "walacor" in rec.getMessage().lower()
    ]
    assert walacor_warns, (
        "Walacor tool-event failure must surface at WARNING+ — silent "
        "failure violates dual-write invariant."
    )


@pytest.mark.anyio
async def test_tool_event_fan_out_wal_failure_is_logged(caplog):
    """Mirror of #4 with the WAL side failing instead."""
    wal = _FakeBackend("wal", tool_event_raises=RuntimeError("disk full"))
    walacor = _FakeBackend("walacor")
    router = StorageRouter([wal, walacor])

    with caplog.at_level(logging.WARNING, logger="gateway.storage.router"):
        await router.write_tool_event({"event_id": "evt-2"})

    assert wal.tool_events == []
    assert walacor.tool_events == [{"event_id": "evt-2"}]

    wal_warns = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING and "wal" in rec.getMessage().lower()
    ]
    assert wal_warns, "WAL tool-event failure must surface at WARNING+."


@pytest.mark.anyio
async def test_tool_event_both_fail_is_logged_for_each_backend(caplog):
    """Both backends fail on tool events → BOTH must log their own
    failure. write_tool_event does not raise (fire-and-forget) but the
    audit trail must call out every backend independently."""
    wal = _FakeBackend("wal", tool_event_raises=RuntimeError("disk full"))
    walacor = _FakeBackend(
        "walacor", tool_event_raises=RuntimeError("Walacor unreachable")
    )
    router = StorageRouter([wal, walacor])

    with caplog.at_level(logging.WARNING, logger="gateway.storage.router"):
        await router.write_tool_event({"event_id": "evt-3"})

    assert wal.tool_events == []
    assert walacor.tool_events == []

    backends_logged = {
        "wal": False,
        "walacor": False,
    }
    for rec in caplog.records:
        if rec.levelno < logging.WARNING:
            continue
        msg = rec.getMessage().lower()
        if "wal " in msg or msg.startswith("storage backend wal"):
            backends_logged["wal"] = True
        if "walacor" in msg:
            backends_logged["walacor"] = True

    assert backends_logged["wal"], "WAL tool-event failure not logged"
    assert backends_logged["walacor"], "Walacor tool-event failure not logged"
