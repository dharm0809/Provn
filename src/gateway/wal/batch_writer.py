"""Batched WAL writer using asyncio.Queue for group commits."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class BatchWriter:
    """Buffers WAL write requests and flushes in batches.

    Flush triggers: batch reaches max_size, or flush_interval_ms elapses.
    Uses asyncio.Queue so callers await enqueue() and get fast returns.
    """

    def __init__(
        self,
        wal_writer: Any,  # WALWriter
        flush_interval_ms: int = 10,
        max_size: int = 50,
    ) -> None:
        self._writer = wal_writer
        self._flush_interval = flush_interval_ms / 1000.0
        self._max_size = max_size
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background flush loop."""
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())
        logger.info(
            "BatchWriter started (flush_ms=%d, max_size=%d)",
            int(self._flush_interval * 1000),
            self._max_size,
        )

    async def stop(self) -> None:
        """Stop the flush loop and flush any remaining records."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final flush
        await self._flush()
        logger.info("BatchWriter stopped")

    async def enqueue(self, record: dict[str, Any]) -> None:
        """Enqueue a record for batched writing."""
        await self._queue.put(record)

    async def _flush_loop(self) -> None:
        """Background loop: flush when batch is full or interval elapses."""
        while self._running:
            try:
                # Wait for first item with timeout
                try:
                    first = await asyncio.wait_for(
                        self._queue.get(), timeout=self._flush_interval
                    )
                except asyncio.TimeoutError:
                    continue

                batch = [first]
                # Drain up to max_size
                while len(batch) < self._max_size:
                    try:
                        item = self._queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

                # If batch isn't full, wait a bit for more items
                if len(batch) < self._max_size:
                    await asyncio.sleep(self._flush_interval)
                    while len(batch) < self._max_size:
                        try:
                            item = self._queue.get_nowait()
                            batch.append(item)
                        except asyncio.QueueEmpty:
                            break

                await self._write_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("BatchWriter flush_loop error", exc_info=True)

    async def _flush(self) -> None:
        """Flush all remaining items in the queue."""
        batch = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                batch.append(item)
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._write_batch(batch)

    async def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        """Write a batch of records using the WALWriter."""
        if not batch:
            return
        try:
            await asyncio.to_thread(self._writer.write_batch, batch)
            logger.debug("BatchWriter flushed %d records", len(batch))
        except Exception:
            logger.error(
                "BatchWriter write_batch failed (%d records)",
                len(batch),
                exc_info=True,
            )
            # Fallback: write individually
            for record in batch:
                try:
                    await asyncio.to_thread(self._writer.write_durable, record)
                except Exception:
                    logger.error(
                        "BatchWriter individual fallback failed for %s",
                        record.get("execution_id", "?"),
                        exc_info=True,
                    )

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()
