"""Alert event bus — async queue with fan-out to dispatchers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class AlertEvent:
    type: str  # "budget_threshold", "policy_violation", "error_spike", "chain_integrity"
    severity: str  # "info", "warning", "critical"
    message: str
    metadata: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AlertBus:
    """Non-blocking event bus with fan-out to dispatchers."""

    def __init__(self, maxsize: int = 1000):
        self._queue: asyncio.Queue[AlertEvent] = asyncio.Queue(maxsize=maxsize)
        self._dispatchers: list = []

    def add_dispatcher(self, dispatcher):
        self._dispatchers.append(dispatcher)

    async def emit(self, event: AlertEvent):
        """Non-blocking put to queue. Drop if full (fail-open)."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Alert queue full — dropping event: %s", event.type)

    async def process_one(self):
        """Process a single event from the queue."""
        event = await self._queue.get()
        for dispatcher in self._dispatchers:
            try:
                await dispatcher.dispatch(event)
            except Exception:
                logger.warning("Alert dispatcher failed for %s", event.type, exc_info=True)
        self._queue.task_done()

    async def run(self):
        """Background task: continuously process events."""
        while True:
            await self.process_one()
