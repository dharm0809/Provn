"""StorageRouter — fans out writes to all registered backends independently."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from gateway.storage.backend import StorageBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriteResult:
    """Outcome of an execution write across all backends."""

    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class StorageRouter:
    """Fans out writes to all registered StorageBackend instances."""

    # Name of the WAL backend. Used to find the local writer that needs
    # to be told when a remote/durable sink has acknowledged the record.
    _WAL_BACKEND_NAME = "wal"
    # Name of the durable sink whose success triggers WAL mark_delivered.
    # Treated as "delivery acknowledged" for the local SQLite WAL.
    _DURABLE_BACKEND_NAME = "walacor"

    def __init__(self, backends: list[StorageBackend]) -> None:
        self._backends = list(backends)
        # Strong refs to in-flight remote attempt-write tasks. The durable
        # WAL leg is awaited inline; the remote leg is decoupled into a
        # task so a slow Walacor can never trip the caller's bounded
        # timeout and discard an attempt that is already durably recorded
        # locally. Without a strong ref the loop could GC the task mid-POST.
        self._pending_remote_attempts: set = set()

    @property
    def backend_names(self) -> list[str]:
        return [b.name for b in self._backends]

    def _wal_backend(self) -> StorageBackend | None:
        """Return the WAL backend if one is registered, else None."""
        for b in self._backends:
            if b.name == self._WAL_BACKEND_NAME:
                return b
        return None

    def _mark_wal_delivered(self, execution_id: str) -> None:
        """Best-effort: tell the WAL backend the record is durably stored.

        Called once the Walacor backend (the durable sink) acknowledges
        the write. Without this the local SQLite WAL would grow forever
        in Walacor-backed deployments — the legacy DeliveryWorker only
        runs when a separate control-plane URL is configured and POSTs
        to a control-plane aggregator endpoint, never the Walacor API.

        Alternative considered (and rejected): introduce a generic
        ``ack_callback`` hook on StorageBackend that any future durable
        sink could fire. Rejected for now because the only durable sink
        is Walacor and the extra indirection adds complexity without a
        second consumer; we can promote this when a new backend lands.
        """
        if not execution_id:
            return
        wal = self._wal_backend()
        if wal is None or not hasattr(wal, "mark_delivered"):
            return
        try:
            wal.mark_delivered(execution_id)  # type: ignore[attr-defined]
        except Exception:
            logger.warning(
                "StorageRouter mark_delivered failed for execution_id=%s",
                execution_id,
                exc_info=True,
            )

    async def write_execution(self, record: dict) -> WriteResult:
        """Parallel fan-out execution write. Returns WriteResult with per-backend outcomes.

        When both the WAL and Walacor backends are registered and the
        Walacor write succeeds, the local WAL row is marked delivered.
        This is the actual delivery acknowledgement path for Walacor-
        backed deployments — DeliveryWorker is only used when a separate
        control-plane aggregator is configured (see _init_wal).
        """
        if not self._backends:
            return WriteResult()

        async def _write_one(backend: StorageBackend) -> tuple[str, bool]:
            try:
                ok = await backend.write_execution(record)
                return (backend.name, ok)
            except Exception:
                logger.error(
                    "Storage backend %s write_execution failed for execution_id=%s",
                    backend.name,
                    record.get("execution_id"),
                    exc_info=True,
                )
                return (backend.name, False)

        results = await asyncio.gather(*(_write_one(b) for b in self._backends))
        succeeded = [name for name, ok in results if ok]
        failed = [name for name, ok in results if not ok]
        if self._backends and not succeeded:
            logger.error(
                "ALL storage backends failed for execution_id=%s",
                record.get("execution_id"),
            )
        # Acknowledge WAL row once durable sink confirms write.
        if self._DURABLE_BACKEND_NAME in succeeded:
            self._mark_wal_delivered(str(record.get("execution_id") or ""))
        return WriteResult(succeeded=succeeded, failed=failed)

    async def write_attempt(self, record: dict) -> None:
        """Fan-out attempt write. Fire-and-forget — never raises.

        Ordering matters for the completeness invariant ("every request
        gets an attempt record"). The WAL backend write is a microsecond
        thread-enqueue; the Walacor backend write is a remote HTTP POST
        that can take seconds — or stall — under load. The caller
        (``_write_attempt_bg``) wraps this whole coroutine in a single
        bounded ``asyncio.wait_for``. If both backends were awaited
        together (``gather``), a slow Walacor would trip that timeout and
        abandon the *entire* operation — discarding the durable local WAL
        row that had already been instant to write. That is exactly how a
        stress test dropped ~9k attempt records: not WAL capacity, but the
        durable write being chained to a slow remote dependency under one
        shared deadline.

        Fix: write the durable WAL backend first and await it, so a
        subsequent timeout abort of the remote fan-out can no longer eat
        the committed audit row. Behaviour is otherwise unchanged — every
        registered backend still receives the write, the dual-write
        contract holds, and deployments with no WAL backend (control-plane
        aggregator mode) take the original byte-identical gather path.
        """
        if not self._backends:
            return

        async def _write_one(backend: StorageBackend) -> None:
            try:
                await backend.write_attempt(record)
            except Exception:
                logger.warning(
                    "Storage backend %s write_attempt failed for request_id=%s",
                    backend.name,
                    record.get("request_id"),
                    exc_info=True,
                )

        wal = self._wal_backend()
        if wal is None:
            # No durable local sink — preserve original parallel fan-out.
            await asyncio.gather(*(_write_one(b) for b in self._backends))
            return
        # Durable local row first (microsecond thread-enqueue) and AWAITED:
        # this is the completeness invariant — once this returns the audit
        # row is recorded regardless of anything remote.
        await _write_one(wal)
        # Remote leg decoupled: a slow/stalled Walacor must not keep this
        # coroutine alive long enough for the caller's bounded timeout
        # (_write_attempt_bg) to cancel it and emit a misleading "attempt
        # skipped" — the attempt is NOT skipped, it is durably in the WAL.
        # Same decoupling principle as commit 520cba5 for executions.
        others = [b for b in self._backends if b is not wal]
        for b in others:
            t = asyncio.create_task(_write_one(b))
            self._pending_remote_attempts.add(t)
            t.add_done_callback(self._pending_remote_attempts.discard)

    async def write_tool_event(self, record: dict) -> None:
        """Parallel fan-out tool event write. Fire-and-forget — never raises.

        When both the WAL and Walacor backends are registered and the
        Walacor write succeeds, the local WAL row keyed by ``event_id``
        is marked delivered. Tool events share the ``wal_records`` table
        and ``delivered`` column with executions, so without this hook
        they would also accumulate forever on every restart.

        The per-backend return value is the success signal: True means
        the durable sink acknowledged, False means it swallowed an
        exception. The protocol-level "must not raise" guarantee for
        ``write_tool_event`` is preserved — the bool only carries
        delivery information that previously had no return channel.
        """
        if not self._backends:
            return

        async def _write_one(backend: StorageBackend) -> tuple[str, bool]:
            try:
                ok = await backend.write_tool_event(record)
                # Legacy backends that still return None are treated as
                # success — preserves the pre-existing contract.
                return (backend.name, ok is not False)
            except Exception:
                logger.warning(
                    "Storage backend %s write_tool_event failed for event_id=%s",
                    backend.name,
                    record.get("event_id"),
                    exc_info=True,
                )
                return (backend.name, False)

        results = await asyncio.gather(*(_write_one(b) for b in self._backends))
        succeeded = [name for name, ok in results if ok]
        if self._DURABLE_BACKEND_NAME in succeeded:
            # tool events are keyed by event_id in wal_records.execution_id
            self._mark_wal_delivered(str(record.get("event_id") or ""))

    async def close(self) -> None:
        """Close all backends. Errors logged but not raised."""
        for backend in self._backends:
            try:
                await backend.close()
            except Exception:
                logger.warning("Storage backend %s close failed", backend.name, exc_info=True)
