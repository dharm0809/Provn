"""Per-run aggregator that materialises :class:`AgentRunManifest` objects.

State model:
    Open runs live in an in-memory dict keyed by ``(tenant_id, run_key)``,
    where ``run_key`` is the first available of ``agent_run_id`` →
    ``trace_id`` → ``caller_key``. Every observe() appends to the open run
    if one exists, or opens a new one.

Run-end detection (§10.3 Pillar 4 / §11.4 kill criterion #3):
    A run closes when any of the following fire:
      1. final-assistant + 30 s of inactivity (default)
      2. explicit close via :meth:`close_run`
      3. 30-min TTL since first observation

    All three conditions are evaluated on every :meth:`sweep` call (cheap;
    most runs sit untouched between calls). The orchestrator invokes
    :meth:`sweep` at the tail of each request so detection is opportunistic
    rather than requiring its own background task — keeps the threading
    model simple and gives reliable test determinism.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from gateway.agent_tracing.manifest import (
    AgentRunManifest,
    FrameworkGuess,
    LLMCallRef,
    ReconstructedToolRef,
    message_chain_hash,
    sign_manifest,
)
from gateway.metrics.prometheus import (
    agent_run_aggregator_open_runs,
    agent_run_manifests_total,
)

logger = logging.getLogger(__name__)

_DEFAULT_INACTIVITY_S = 30.0
_DEFAULT_TTL_S = 30 * 60.0


# ── Framework classifier (rule-based v1) ─────────────────────────────────────
#
# §11.3 v1 scope: rule-based regex matcher only — ONNX classifier deferred
# to v2. Returns ("unknown", 0.0) when nothing fires.

_UA_RULES: tuple[tuple[str, str, float], ...] = (
    ("openai-agents-python", "openai-agents-sdk", 0.95),
    ("openai-agents/", "openai-agents-sdk", 0.95),
    ("claude-agent-sdk", "claude-agent-sdk", 0.95),
    ("crewai", "crewai", 0.85),
    ("langgraph", "langgraph", 0.85),
    ("langchain", "langchain", 0.7),
    ("llamaindex", "llamaindex", 0.7),
)

_TOOL_NAME_RULES: tuple[tuple[frozenset[str], str, float], ...] = (
    (frozenset({"Read", "Bash", "Edit", "Glob", "Grep"}), "claude-agent-sdk", 0.85),
)


def classify_framework(
    *,
    user_agent: str | None,
    tool_names: Iterable[str] = (),
) -> FrameworkGuess:
    ua = (user_agent or "").lower()
    for needle, name, conf in _UA_RULES:
        if needle in ua:
            return FrameworkGuess(name=name, confidence=conf)
    seen = {t for t in tool_names if t}
    for required, name, conf in _TOOL_NAME_RULES:
        if required.issubset(seen):
            return FrameworkGuess(name=name, confidence=conf)
    return FrameworkGuess(name="unknown", confidence=0.0)


# ── Open-run state ───────────────────────────────────────────────────────────


@dataclass
class _OpenRun:
    run_id: str
    tenant_id: str
    caller_identity: dict[str, Any]
    trace_id: str | None
    user_agent: str | None
    start_ts: float
    last_activity_ts: float
    last_assistant_final_ts: float | None = None
    llm_calls: list[LLMCallRef] = field(default_factory=list)
    tool_events: list[ReconstructedToolRef] = field(default_factory=list)
    messages_seen: list[Any] = field(default_factory=list)


# ── Public aggregator ────────────────────────────────────────────────────────


class AgentRunAggregator:
    """Tracks open agent runs and produces signed manifests on close."""

    def __init__(
        self,
        inactivity_seconds: float = _DEFAULT_INACTIVITY_S,
        ttl_seconds: float = _DEFAULT_TTL_S,
    ) -> None:
        self.inactivity_seconds = inactivity_seconds
        self.ttl_seconds = ttl_seconds
        self._open: dict[tuple[str, str], _OpenRun] = {}
        # Manifests that observe() finalised in-line (because the request
        # itself triggered run-end). Drained on the next sweep() so the
        # existing call-sites collect them alongside sweep-finalised runs.
        self._pending_finalised: list[AgentRunManifest] = []
        self._lock = threading.Lock()

    # ── observe ──────────────────────────────────────────────────────────────

    def observe(
        self,
        *,
        tenant_id: str,
        run_key: str,
        record_id: str,
        model: str | None,
        timestamp_iso: str,
        now: float,
        messages: list[Any],
        recon_events: Iterable[Any] = (),
        caller_identity: dict[str, Any] | None = None,
        trace_id: str | None = None,
        user_agent: str | None = None,
        walacor_dh: str | None = None,
        record_hash: str | None = None,
        is_final_assistant: bool = False,
    ) -> None:
        if not run_key:
            return
        key = (tenant_id, run_key)
        with self._lock:
            run = self._open.get(key)
            # Inactivity check BEFORE we mutate state. Without this, the very
            # request that should trigger run-end (because it arrived after
            # the inactivity window) instead extends the existing run by
            # bumping last_activity_ts. The pre-existing run is closed first
            # and a fresh one opens for the new turn — which is the correct
            # semantics for "the next conversation from the same caller".
            if (
                run is not None
                and run.last_assistant_final_ts is not None
                and (now - run.last_assistant_final_ts) >= self.inactivity_seconds
            ):
                self._pending_finalised.append(self._finalise(run, "inactivity", now))
                self._open.pop(key, None)
                run = None
            if run is None:
                run = _OpenRun(
                    run_id=uuid.uuid4().hex,
                    tenant_id=tenant_id,
                    caller_identity=dict(caller_identity or {}),
                    trace_id=trace_id,
                    user_agent=user_agent,
                    start_ts=now,
                    last_activity_ts=now,
                )
                self._open[key] = run
            run.last_activity_ts = now
            run.llm_calls.append(LLMCallRef(
                record_id=record_id,
                record_hash=record_hash,
                walacor_dh=walacor_dh,
                model=model,
                timestamp=timestamp_iso,
            ))
            for ev in recon_events:
                run.tool_events.append(ReconstructedToolRef(
                    kind=getattr(ev, "kind", "") or "",
                    tool_name=getattr(ev, "tool_name", None),
                    tool_call_id=getattr(ev, "tool_call_id", None),
                    tc_hash=getattr(ev, "args_hash", None),
                    tr_hash=getattr(ev, "content_hash", None),
                    turn_seq=getattr(ev, "turn_seq", None),
                ))
            run.messages_seen.extend(messages or [])
            if is_final_assistant:
                run.last_assistant_final_ts = now

    # ── sweep / close ────────────────────────────────────────────────────────

    def sweep(self, now: float) -> list[AgentRunManifest]:
        """Close any runs whose inactivity or TTL expired. Also drains any
        manifests observe() finalised in-line. Returns finalised manifests;
        the caller is responsible for delivering them to Walacor + the WAL."""
        manifests: list[AgentRunManifest] = []
        to_close: list[tuple[tuple[str, str], _OpenRun, str]] = []
        with self._lock:
            if self._pending_finalised:
                manifests.extend(self._pending_finalised)
                self._pending_finalised.clear()
            for key, run in list(self._open.items()):
                if (now - run.start_ts) >= self.ttl_seconds:
                    to_close.append((key, run, "ttl"))
                    continue
                if (
                    run.last_assistant_final_ts is not None
                    and (now - run.last_assistant_final_ts) >= self.inactivity_seconds
                ):
                    to_close.append((key, run, "inactivity"))
            for key, run, reason in to_close:
                self._open.pop(key, None)
                manifests.append(self._finalise(run, reason, now))
        return manifests

    def close_run(self, *, tenant_id: str, run_key: str, now: float) -> AgentRunManifest | None:
        with self._lock:
            run = self._open.pop((tenant_id, run_key), None)
        if run is None:
            return None
        return self._finalise(run, "explicit_close", now)

    def open_runs(self) -> int:
        with self._lock:
            return len(self._open)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._open.clear()

    # ── internal ─────────────────────────────────────────────────────────────

    def _finalise(self, run: _OpenRun, reason: str, now: float) -> AgentRunManifest:
        from datetime import datetime, timezone

        end_ts = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        start_ts = datetime.fromtimestamp(run.start_ts, tz=timezone.utc).isoformat()
        guess = classify_framework(
            user_agent=run.user_agent,
            tool_names=[ev.tool_name or "" for ev in run.tool_events],
        )
        manifest = AgentRunManifest(
            run_id=run.run_id,
            tenant_id=run.tenant_id,
            caller_identity=run.caller_identity,
            trace_id=run.trace_id,
            framework_guess=guess,
            start_ts=start_ts,
            end_ts=end_ts,
            end_reason=reason,
            llm_calls=list(run.llm_calls),
            reconstructed_tool_events=list(run.tool_events),
            message_chain_hash=message_chain_hash(run.messages_seen),
        )
        sign_manifest(manifest)
        agent_run_manifests_total.labels(end_reason=reason).inc()
        # Best-effort gauge update; sweep() callers re-set this on every
        # invocation, but observe()-side finalisation should also reflect
        # the change immediately.
        try:
            agent_run_aggregator_open_runs.set(len(self._open))
        except Exception:  # pragma: no cover — gauge set is infallible
            pass
        return manifest


# ── Process-singleton hook ───────────────────────────────────────────────────

_aggregator: AgentRunAggregator | None = None
_lock = threading.Lock()


def get_aggregator() -> AgentRunAggregator:
    global _aggregator
    if _aggregator is None:
        with _lock:
            if _aggregator is None:
                _aggregator = AgentRunAggregator()
    return _aggregator


def reset_for_tests() -> None:
    global _aggregator
    with _lock:
        _aggregator = None
