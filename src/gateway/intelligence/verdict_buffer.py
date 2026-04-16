"""In-memory bounded buffer for ONNX verdicts on the hot path.

Fire-and-forget enqueue (never blocks, never raises). On overflow, drops the
oldest entry and increments `dropped_total` — the flusher (Task 6) batch-writes
to SQLite at a cadence the buffer can absorb. A bounded deque is the right data
structure: O(1) append, O(1) popleft, implicit FIFO order.

Thread safety: single asyncio event loop only. `record()` and `drain()` are
lockless, which is correct under CPython's GIL + single-loop semantics — deque
`append`/`popleft`/`len` are atomic at the C level and no method suspends
mid-operation. Calling from threads or from multiple event loops is UNSAFE and
will race on the overflow branch.

Sampling bias: under sustained overflow, the buffer systematically favors
recent verdicts over the start-of-burst sample. That's intentional (distillation
cares about current traffic patterns), but analysis consumers downstream should
be aware when reasoning about offline replay or stratified sampling.
"""
from __future__ import annotations

from collections import deque

from gateway.intelligence.types import ModelVerdict


class VerdictBuffer:
    def __init__(self, max_size: int = 10_000) -> None:
        # Use a raw deque WITHOUT maxlen so we can count drops explicitly.
        # A maxlen-bounded deque silently evicts on append, which loses the
        # drop counter we need for the `verdict_buffer_dropped_total` metric.
        self._buf: deque[ModelVerdict] = deque()
        self._dropped = 0
        # Clamp to a sane minimum so record() never has to popleft() from an
        # empty buffer — that would raise IndexError and violate the "never
        # raises on the hot path" contract. Also coerces floats to int.
        self._max = max(1, int(max_size))

    def record(self, verdict: ModelVerdict) -> None:
        if len(self._buf) >= self._max:
            # Drop oldest to keep newest — newest verdicts are most useful for
            # distillation since they reflect current traffic patterns.
            self._buf.popleft()
            self._dropped += 1
        self._buf.append(verdict)

    def drain(self, max_batch: int = 500) -> list[ModelVerdict]:
        out: list[ModelVerdict] = []
        while self._buf and len(out) < max_batch:
            out.append(self._buf.popleft())
        return out

    @property
    def dropped_total(self) -> int:
        return self._dropped

    @property
    def size(self) -> int:
        return len(self._buf)
