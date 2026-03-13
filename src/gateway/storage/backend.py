"""StorageBackend protocol — interface for audit record storage backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Interface for audit record storage backends.

    Each backend handles its own field mapping, serialization, and error
    handling internally. The StorageRouter fans out writes to all backends.
    """

    @property
    def name(self) -> str:
        """Unique identifier for this backend (e.g. 'wal', 'walacor')."""
        ...

    async def write_execution(self, record: dict) -> bool:
        """Write an execution record. Returns True on success, False on failure."""
        ...

    async def write_attempt(self, record: dict) -> None:
        """Write an attempt record. Best-effort — must not raise."""
        ...

    async def write_tool_event(self, record: dict) -> None:
        """Write a tool event record. Best-effort — must not raise."""
        ...

    async def close(self) -> None:
        """Graceful shutdown."""
        ...
