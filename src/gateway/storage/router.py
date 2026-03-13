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

    def __init__(self, backends: list[StorageBackend]) -> None:
        self._backends = list(backends)

    @property
    def backend_names(self) -> list[str]:
        return [b.name for b in self._backends]

    async def write_execution(self, record: dict) -> WriteResult:
        """Parallel fan-out execution write. Returns WriteResult with per-backend outcomes."""
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
        """Parallel fan-out tool event write. Fire-and-forget — never raises."""
        if not self._backends:
            return

        async def _write_one(backend: StorageBackend) -> None:
            try:
                await backend.write_tool_event(record)
            except Exception:
                logger.warning(
                    "Storage backend %s write_tool_event failed for event_id=%s",
                    backend.name,
                    record.get("event_id"),
                    exc_info=True,
                )

        await asyncio.gather(*(_write_one(b) for b in self._backends))

    async def close(self) -> None:
        """Close all backends. Errors logged but not raised."""
        for backend in self._backends:
            try:
                await backend.close()
            except Exception:
                logger.warning("Storage backend %s close failed", backend.name, exc_info=True)
