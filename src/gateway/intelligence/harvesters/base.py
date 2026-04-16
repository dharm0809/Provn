"""Harvester framework: signal type, ABC, and async queue-backed runner.

Design
------
The orchestrator runs on the request hot path; harvesters may do slow work
(SQLite UPDATE, extra LLM call for teacher labeling). Decoupling via an
`asyncio.Queue` means:

  * `submit()` is synchronous + non-blocking — the orchestrator pays at
    most a `put_nowait` dict write. If the queue is full we DROP the
    signal (returning False) rather than block the user response. Signals
    are observational, so loss is acceptable.
  * The background `run()` loop consumes signals, filters by
    `target_model`, and dispatches to all matching harvesters
    concurrently via `asyncio.gather`. One raising harvester logs +
    continues — a peer harvester for the same signal is unaffected, and
    subsequent signals keep flowing.
  * Single-event-loop semantics only. Multi-thread access is not
    supported; the orchestrator and the runner live on the same loop.

The framework alone does nothing useful — it ships in Task 13 so the
orchestrator hook can be wired once and the per-model harvesters
(Tasks 14-16) slot in without touching the dispatch path again.
"""
from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarvesterSignal:
    """A single per-model observation emitted at the end of a request.

    Fields
    ------
    request_id
        The per-request UUID set by completeness middleware (see
        `gateway.util.request_context.request_id_var`). Matches the value
        recorded on the verdict row, so harvesters can `UPDATE ... WHERE
        request_id = ?` to back-write their divergence signal. `None`
        when the emitter had no request context (e.g. system tasks).
    model_name
        One of `ALLOWED_MODEL_NAMES` — identifies which per-model
        harvesters should receive this signal.
    prediction
        Short label describing what the model emitted (e.g. `"web_search"`
        for intent, `"complete"`/`"incomplete"` for schema_mapper, safety
        category name for safety).
    response_payload
        The full response metadata dict — harvesters that want the raw
        context (analyzer decisions, tool events, canonical shape) look
        here. Passed by reference; treat as read-only.
    context
        Extra per-signal shortcuts (session_id, prompt text, provider
        name, etc.). Kept separate from `response_payload` so harvesters
        don't have to grovel through the metadata dict for common keys.
    """
    request_id: str | None
    model_name: str
    prediction: str
    response_payload: Any
    context: Mapping[str, Any]


class Harvester(abc.ABC):
    """Abstract base for per-model harvesters.

    Each subclass fixes `target_model` to a single canonical name and
    implements `process` to inspect the signal and write back a
    divergence label to the verdict log. Subclasses MUST NOT block the
    event loop on long SQL — use `asyncio.to_thread` for SQLite work.
    """

    # Class attribute — the model whose signals this harvester cares about.
    target_model: str = ""

    @abc.abstractmethod
    async def process(self, signal: HarvesterSignal) -> None:
        """Consume a signal. Exceptions are logged by the runner, not by the caller."""


class HarvesterRunner:
    """Queue-backed dispatcher.

    Lifecycle
    ---------
    1. `__init__` — create the queue (bounded) and register an initial
       harvester list.
    2. `start()` — schedule the background `run()` task on the current
       event loop. Safe to call exactly once.
    3. `submit(signal)` — hot-path callers push signals; returns `False`
       if the queue is full (signal is dropped).
    4. `register(harvester)` — add harvesters after start (Task 14-16
       registration paths do this).
    5. `stop()` — signal the loop to exit after draining, then `await`
       the task. Idempotent.

    The runner is intentionally simple: no retry, no dead-letter queue,
    no per-harvester timeout. Signals are statistical telemetry; a lost
    one is irrelevant to distillation outcomes (see the VerdictFlushWorker
    docstring for the same argument).
    """

    def __init__(
        self,
        harvesters: list[Harvester] | None = None,
        *,
        max_queue: int = 1000,
    ) -> None:
        self._harvesters: list[Harvester] = list(harvesters) if harvesters else []
        # Bounded queue — on overflow, `submit` drops the signal rather
        # than block the orchestrator. 1000 tolerates a short burst even
        # if a harvester is momentarily slow.
        self._queue: asyncio.Queue[HarvesterSignal | None] = asyncio.Queue(
            maxsize=max_queue
        )
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._inflight: set[asyncio.Task] = set()

    def register(self, harvester: Harvester) -> None:
        """Register a harvester. Safe to call before or after `start()`."""
        self._harvesters.append(harvester)

    def submit(self, signal: HarvesterSignal) -> bool:
        """Non-blocking enqueue. Returns False if the queue is full (dropped)."""
        if self._stopping:
            return False
        try:
            self._queue.put_nowait(signal)
            return True
        except asyncio.QueueFull:
            # Dropped signals are expected under sustained overload.
            # `debug` not `warning` — noisy log at scale.
            logger.debug(
                "harvester queue full (size=%d), dropping signal model=%r",
                self._queue.maxsize, signal.model_name,
            )
            return False

    def start(self) -> None:
        """Launch the background consumer task on the current event loop."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="harvester-runner")

    async def run(self) -> None:
        """Consume the queue; dispatch each signal to matching harvesters."""
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                raise
            if item is None:
                # Sentinel from `stop()` — exit the loop.
                return
            try:
                await self._dispatch(item)
            except Exception:
                # Dispatch internals are already defensive, but guard the
                # outermost frame too so one pathological signal cannot
                # terminate the runner.
                logger.exception("harvester dispatch crashed on signal %r", item.model_name)

    async def _dispatch(self, signal: HarvesterSignal) -> None:
        matching = [h for h in self._harvesters if h.target_model == signal.model_name]
        if not matching:
            return
        # Fan out to matching harvesters concurrently. Wrapping each in
        # `_safe_process` isolates failures so one bad harvester cannot
        # starve its peers.
        tasks = [asyncio.create_task(self._safe_process(h, signal)) for h in matching]
        self._inflight.update(tasks)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for t in tasks:
                self._inflight.discard(t)

    async def _safe_process(self, harvester: Harvester, signal: HarvesterSignal) -> None:
        try:
            await harvester.process(signal)
        except Exception:
            logger.warning(
                "harvester %s failed on model=%r request_id=%r",
                type(harvester).__name__, signal.model_name, signal.request_id,
                exc_info=True,
            )

    async def join_inflight(self) -> None:
        """Await any currently-dispatching harvester tasks.

        Useful for deterministic test teardown where the caller wants to
        observe the post-dispatch state. Not used on the hot path.
        """
        if not self._inflight:
            # Yield once so a signal that was just pulled off the queue
            # but has not yet begun dispatch gets a chance to run.
            await asyncio.sleep(0)
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    async def stop(self) -> None:
        """Drain the queue, then stop the runner. Idempotent."""
        if self._task is None:
            return
        if self._stopping:
            # Second call: just wait for the prior stop to finish.
            if not self._task.done():
                try:
                    await self._task
                except Exception:
                    pass
            return
        self._stopping = True
        # Inject a sentinel so `await self._queue.get()` wakes up even if
        # no signals are pending.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            # Queue is saturated; cancel as a fallback so the task exits.
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("harvester runner task raised on shutdown", exc_info=True)
        self._task = None
