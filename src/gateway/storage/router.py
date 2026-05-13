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
        """Parallel fan-out attempt write. Fire-and-forget — never raises."""
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

        await asyncio.gather(*(_write_one(b) for b in self._backends))

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
