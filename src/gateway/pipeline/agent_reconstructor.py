"""Pillar 1 of agent tracing: wire reconstruction via message-array diffing.

Lifted from ``scripts/spikes/agent_reconstructor_spike.py`` and adapted for the
production gateway. The engine itself is unchanged in shape — the spike already
validated load (§12) — but it now lives behind a process-singleton with
prometheus counters, a hard per-caller payload ceiling, and a 5-minute sliding
window.

Design (from ``docs/research/AGENT-TRACING-RESEARCH.md`` §10.3 Pillar 1):

For every chat-completions request we cache the last-seen ``messages[]`` per
caller. On the next request we diff: every message present now that wasn't
present last time is an *agent observable* — a tool call the agent emitted,
a tool result the agent ran app-side, or a fresh user turn. The diff runs
purely off the wire; no SDK cooperation is required.

Bounds enforced here (§12.5–§12.7):
  - LRU cache, byte-capped at 256 MB by default; eviction is graceful — the
    next request from an evicted caller just produces no diff.
  - Per-caller payload ceiling of 500 KB; pathological histories are skipped
    with a counter bump and a logged warning so they don't skew cache math.
  - 5-minute sliding window — older entries are dropped on access.
  - All counters surface in ``/v1/connections`` via a dedicated tile and as
    Prometheus metrics, so eviction pressure is operationally visible the
    moment it appears.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping

from gateway.metrics.prometheus import (
    agent_reconstructor_cache_bytes,
    agent_reconstructor_cache_entries,
    agent_reconstructor_evictions_total,
    agent_reconstructor_events_total,
    agent_reconstructor_oversize_skipped_total,
)

logger = logging.getLogger(__name__)

# Per-caller cache entry hard ceiling. Pathological callers submitting megabytes
# of history would otherwise dominate the LRU; cap-and-skip keeps the math sane.
_MAX_PAYLOAD_BYTES = 500 * 1024
_DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_DEFAULT_WINDOW_SECONDS = 300.0


@dataclass(frozen=True)
class ReconstructionEvent:
    """One agent observable inferred between two requests from the same caller."""

    kind: str  # "tool_call_observed" | "tool_result_observed" | "new_user_turn"
    caller: str
    turn_seq: int
    tool_name: str | None = None
    tool_call_id: str | None = None
    content_hash: str | None = None
    args_hash: str | None = None  # Pillar 2 (tool-call args fingerprint)


@dataclass
class _CachedTurn:
    messages: list[dict[str, Any]]
    timestamp: float
    bytes: int


@dataclass
class EngineStats:
    requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    evictions: int = 0
    oversize_skipped: int = 0
    events_emitted: int = 0
    cache_bytes_now: int = 0
    cache_bytes_peak: int = 0
    cache_entries: int = 0
    last_event_ts: float | None = None

    def as_tile_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "hit_rate": (self.cache_hits / self.requests) if self.requests else 0.0,
            "evictions": self.evictions,
            "oversize_skipped": self.oversize_skipped,
            "events_emitted": self.events_emitted,
            "cache_entries": self.cache_entries,
            "cache_bytes_now": self.cache_bytes_now,
            "cache_bytes_peak": self.cache_bytes_peak,
            "last_event_ts": self.last_event_ts,
        }


def _content_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _diff_messages(
    prior: list[dict[str, Any]],
    current: list[dict[str, Any]],
    caller: str,
) -> list[ReconstructionEvent]:
    prior_hashes = {_content_hash(m) for m in prior}
    events: list[ReconstructionEvent] = []
    for idx, m in enumerate(current):
        if _content_hash(m) in prior_hashes:
            continue
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function") if isinstance(tc, dict) else {}
                fn = fn or {}
                args = fn.get("arguments")
                events.append(
                    ReconstructionEvent(
                        kind="tool_call_observed",
                        caller=caller,
                        turn_seq=idx,
                        tool_name=fn.get("name") or tc.get("name"),
                        tool_call_id=tc.get("id"),
                        args_hash=_content_hash(args) if args is not None else None,
                    )
                )
        elif role == "tool":
            events.append(
                ReconstructionEvent(
                    kind="tool_result_observed",
                    caller=caller,
                    turn_seq=idx,
                    tool_call_id=m.get("tool_call_id"),
                    content_hash=_content_hash(m.get("content", "")),
                )
            )
        elif role == "user":
            events.append(
                ReconstructionEvent(
                    kind="new_user_turn",
                    caller=caller,
                    turn_seq=idx,
                    content_hash=_content_hash(m.get("content", "")),
                )
            )
    return events


class MessageDiffEngine:
    """Per-caller LRU cache of last-seen ``messages[]`` with diff-on-observe.

    The engine is thread-safe (held under a single :class:`threading.Lock`)
    because the orchestrator may observe from arbitrary workers under
    BaseHTTPMiddleware's anyio task model.
    """

    def __init__(
        self,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        max_payload_bytes: int = _MAX_PAYLOAD_BYTES,
    ) -> None:
        self.max_bytes = max_bytes
        self.window_seconds = window_seconds
        self.max_payload_bytes = max_payload_bytes
        self._cache: OrderedDict[str, _CachedTurn] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.stats = EngineStats()

    def observe(
        self,
        caller: str,
        messages: list[dict[str, Any]] | None,
        now: float,
    ) -> list[ReconstructionEvent]:
        if not caller or not isinstance(messages, list):
            return []
        with self._lock:
            self.stats.requests += 1
            encoded = json.dumps(messages, separators=(",", ":")).encode("utf-8")
            payload_size = len(encoded)
            if payload_size > self.max_payload_bytes:
                self.stats.oversize_skipped += 1
                agent_reconstructor_oversize_skipped_total.inc()
                logger.warning(
                    "agent_reconstructor: caller=%s submitted %d-byte messages[] "
                    "exceeding %d-byte ceiling — skipping diff",
                    caller, payload_size, self.max_payload_bytes,
                )
                # Also drop any prior entry for this caller — we can't compare
                # safely with a partial signal.
                self._evict(caller)
                return []

            prior = self._cache.get(caller)
            within_window = (
                prior is not None and (now - prior.timestamp) <= self.window_seconds
            )
            events: list[ReconstructionEvent] = []
            if prior and within_window:
                self.stats.cache_hits += 1
                events = _diff_messages(prior.messages, messages, caller)
            else:
                self.stats.cache_misses += 1
                if prior:
                    self._evict(caller)

            new_entry = _CachedTurn(messages=list(messages), timestamp=now, bytes=payload_size)
            old = self._cache.pop(caller, None)
            if old is not None:
                self._bytes -= old.bytes
            self._bytes += new_entry.bytes
            self._cache[caller] = new_entry
            self._cache.move_to_end(caller)

            while self._bytes > self.max_bytes and len(self._cache) > 1:
                oldest = next(iter(self._cache))
                if oldest == caller:
                    break
                self._evict(oldest)
                self.stats.evictions += 1
                agent_reconstructor_evictions_total.inc()

            self.stats.cache_bytes_now = self._bytes
            self.stats.cache_entries = len(self._cache)
            if self._bytes > self.stats.cache_bytes_peak:
                self.stats.cache_bytes_peak = self._bytes
            # Mirror in-flight cache size to the Prometheus gauges so the
            # §11.4 cache>1GB kill criterion has an alertable signal — the
            # tile JSON only shows it after a UI poll.
            agent_reconstructor_cache_bytes.set(self._bytes)
            agent_reconstructor_cache_entries.set(len(self._cache))
            if events:
                self.stats.events_emitted += len(events)
                self.stats.last_event_ts = now
                agent_reconstructor_events_total.inc(len(events))
            return events

    def _evict(self, caller: str) -> None:
        entry = self._cache.pop(caller, None)
        if entry is not None:
            self._bytes -= entry.bytes

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.stats.as_tile_dict()

    def reset(self) -> None:
        """Test-only — clear cache and counters."""
        with self._lock:
            self._cache.clear()
            self._bytes = 0
            self.stats = EngineStats()


# ── Process-wide singleton ────────────────────────────────────────────────────

_engine: MessageDiffEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> MessageDiffEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = MessageDiffEngine()
    return _engine


def reset_engine_for_tests() -> None:
    global _engine
    with _engine_lock:
        _engine = None


def caller_key_for(metadata: Mapping[str, Any] | None, tenant_id: str | None) -> str:
    """Stable cache key for a request.

    Prefer the caller's identity (user, then api_key id, then team), then fall
    back to the tenant. Keying any narrower would split a single agent's loop
    across cache slots; keying broader would collide unrelated callers.
    """
    if isinstance(metadata, Mapping):
        for k in ("user", "api_key_id", "team"):
            v = metadata.get(k)
            if isinstance(v, str) and v:
                return f"{tenant_id or ''}:{k}:{v}"
    return f"{tenant_id or ''}:tenant"
