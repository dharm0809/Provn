"""WALBackend — StorageBackend wrapping the local SQLite WAL writer."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.wal.writer import WALWriter

logger = logging.getLogger(__name__)


class WALBackend:
    """StorageBackend implementation backed by local SQLite WAL."""

    name = "wal"

    def __init__(self, wal_writer: WALWriter) -> None:
        self._writer = wal_writer

    async def write_execution(self, record: dict) -> bool:
        try:
            self._writer.write_and_fsync(record)
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
            self._writer.write_attempt(**record)
        except Exception:
            logger.warning(
                "WAL write_attempt failed request_id=%s",
                record.get("request_id"),
                exc_info=True,
            )

    async def write_tool_event(self, record: dict) -> None:
        try:
            self._writer.write_tool_event(record)
        except Exception:
            logger.warning(
                "WAL write_tool_event failed event_id=%s",
                record.get("event_id"),
                exc_info=True,
            )

    async def close(self) -> None:
        self._writer.close()
