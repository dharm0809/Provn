"""WALBackend — StorageBackend wrapping the local SQLite WAL writer."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.wal.batch_writer import BatchWriter
    from gateway.wal.writer import WALWriter

logger = logging.getLogger(__name__)


class WALBackend:
    """StorageBackend implementation backed by local SQLite WAL.

    Write paths use the WALWriter's dedicated background thread via
    enqueue_* methods (fire-and-forget queue puts), eliminating the
    asyncio.to_thread dispatch overhead of the previous design.
    """

    name = "wal"

    def __init__(self, wal_writer: WALWriter, batch_writer: BatchWriter | None = None) -> None:
        self._writer = wal_writer
        self._batch_writer = batch_writer

    async def write_execution(self, record: dict) -> bool:
        try:
            if self._batch_writer:
                await self._batch_writer.enqueue(record)
            else:
                self._writer.enqueue_write_execution(record)
            return True
        except Exception:
            logger.error(
                "WAL write_execution failed execution_id=%s",
                record.get("execution_id"),
                exc_info=True,
            )
            return False

    async def write_attempt(self, record: dict) -> None:
        try:
            self._writer.enqueue_write_attempt(**record)
        except Exception:
            logger.warning(
                "WAL write_attempt failed request_id=%s",
                record.get("request_id"),
                exc_info=True,
            )

    async def write_tool_event(self, record: dict) -> bool | None:
        """Enqueue a tool event onto the dedicated writer thread.

        Returns ``True`` on successful enqueue, ``False`` on failure
        (must not raise — the protocol-level guarantee is preserved).
        Symmetrical with :meth:`WalacorBackend.write_tool_event` so
        ``StorageRouter`` can distinguish actual success from a swallowed
        exception when deciding whether to fire the WAL mark_delivered
        ack hook.
        """
        try:
            self._writer.enqueue_write_tool_event(record)
            return True
        except Exception:
            logger.warning(
                "WAL write_tool_event failed event_id=%s",
                record.get("event_id"),
                exc_info=True,
            )
            return False

    def mark_delivered(self, execution_id: str) -> None:
        """Mark a local WAL row as delivered — called once the durable
        sink (Walacor backend) acknowledges the write.

        Without this hook the local SQLite WAL grows forever even when
        Walacor is healthy, because the legacy DeliveryWorker only
        targets the control-plane aggregator endpoint and is not started
        in Walacor-backed deployments.  StorageRouter invokes this from
        ``write_execution`` / ``write_tool_event`` on successful Walacor
        writes; for control-plane-fronted deployments the
        ``DeliveryWorker`` continues to call ``WALWriter.mark_delivered``
        directly after a successful POST.
        """
        try:
            self._writer.mark_delivered(execution_id)
        except Exception:
            logger.warning(
                "WAL mark_delivered failed execution_id=%s",
                execution_id,
                exc_info=True,
            )

    async def close(self) -> None:
        self._writer.close()
