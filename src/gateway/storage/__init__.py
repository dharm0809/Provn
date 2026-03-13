"""Storage abstraction layer: pluggable backends with fan-out routing."""

from gateway.storage.backend import StorageBackend
from gateway.storage.router import StorageRouter, WriteResult

__all__ = ["StorageBackend", "StorageRouter", "WriteResult"]
