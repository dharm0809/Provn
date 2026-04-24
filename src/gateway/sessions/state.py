"""Session envelope state: in-memory accumulator for Phase 24 session-scoped Walacor envelopes.

A SessionEnvelopeState collects every turn inside one logical conversation
(session_id) into a single append-only structure. It is serialised for Walacor
submission (ETId 9000014) and mirrored to the local WAL table
`gateway_sessions_envelope` for crash durability. Each turn embeds the
execution's `record_hash` verbatim so the session envelope inherits the
per-turn session-chain integrity without recomputing any hash.

Phase A scope: data model + basic operations (add_turn, cap-check, redact).
Phase B: crash replay. Phase C: debounce + idle sweeper + rollover child.
Phase D: redaction API + dashboard. Phase E: production tier checks.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

import gateway.util.json_utils as json


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclasses.dataclass
class SessionEnvelopeState:
    """In-memory rollup of every turn for a single session.

    All fields map 1:1 onto the Walacor `walacor_gw_sessions` schema (ETId
    9000014) via :meth:`to_walacor_record`. Fields are kept flat/JSON-safe.

    Note: Phase A does NOT mint a child envelope when rollover caps are hit —
    see :meth:`is_at_cap`. The writer simply stops accumulating, marks status
    as ``rolled_over`` and logs a warning. Full rollover-to-child wiring is
    Phase C.
    """

    session_id: str
    tenant_id: str
    gateway_id: str

    # Participant / identity fields (denormalised from CallerIdentity)
    participant_user_id: str | None = None
    participant_email: str | None = None
    participant_team: str | None = None
    participant_roles: list[str] = dataclasses.field(default_factory=list)
    participant_source: str | None = None

    # Lifecycle
    created_at: str = dataclasses.field(default_factory=_now_iso)
    updated_at: str = dataclasses.field(default_factory=_now_iso)
    last_turn_at: str | None = None
    turn_count: int = 0
    status: str = "open"  # open | idle_closed | rolled_over

    # Rollup content
    turns: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    # Running token totals
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    # Session chain carry-through
    first_record_hash: str | None = None
    latest_record_hash: str | None = None
    latest_sequence_number: int | None = None

    # Rollover linkage (Phase C will populate this when a rolled-over
    # session spawns a successor).
    parent_session_envelope_id: str | None = None

    # Free-form metadata (e.g. policy_version, enforcement_mode)
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    # ── Turn ingestion ────────────────────────────────────────────────

    def add_turn(
        self,
        execution_record: dict[str, Any],
        tool_events_count: int,
        identity: Any | None,
    ) -> None:
        """Append an execution record to the envelope as a verbatim turn entry.

        The turn entry holds full prompt/response text — per the Phase 24
        design decision. Redaction is append-only (see :meth:`redact_turn`).
        ``record_hash`` is embedded verbatim from the execution record —
        the envelope does NOT recompute any hash.
        """
        exec_id = execution_record.get("execution_id")
        sequence = execution_record.get("sequence_number")
        timestamp = execution_record.get("timestamp") or _now_iso()
        record_hash = execution_record.get("record_hash")

        turn = {
            "execution_id": exec_id,
            "sequence_number": sequence,
            "timestamp": timestamp,
            "role": "assistant",  # gateway-side, one execution = one model reply
            "model_id": execution_record.get("model_id"),
            "provider": execution_record.get("provider"),
            "prompt_text": execution_record.get("prompt_text") or "",
            "response_content": execution_record.get("response_content") or "",
            "thinking_content": execution_record.get("thinking_content") or "",
            "prompt_tokens": execution_record.get("prompt_tokens") or 0,
            "completion_tokens": execution_record.get("completion_tokens") or 0,
            "total_tokens": execution_record.get("total_tokens") or 0,
            "latency_ms": execution_record.get("latency_ms"),
            "record_hash": record_hash,
            "policy_result": execution_record.get("policy_result"),
            "tool_events_count": int(tool_events_count or 0),
            "redacted": False,
            "redaction_reason": None,
        }
        self.turns.append(turn)

        # Running totals
        self.prompt_tokens += int(turn["prompt_tokens"] or 0)
        self.completion_tokens += int(turn["completion_tokens"] or 0)
        self.total_tokens += int(turn["total_tokens"] or 0)

        self.turn_count = len(self.turns)
        self.last_turn_at = timestamp
        self.updated_at = _now_iso()

        if record_hash is not None:
            if self.first_record_hash is None:
                self.first_record_hash = record_hash
            self.latest_record_hash = record_hash
        if sequence is not None:
            self.latest_sequence_number = sequence

        # Participant info is learned lazily from the first identified turn.
        if identity is not None and self.participant_user_id is None:
            self.participant_user_id = getattr(identity, "user_id", None)
            self.participant_email = getattr(identity, "email", None) or None
            self.participant_team = getattr(identity, "team", None)
            roles = getattr(identity, "roles", None) or []
            self.participant_roles = list(roles)
            self.participant_source = getattr(identity, "source", None)

    # ── Caps / rollover ──────────────────────────────────────────────

    def is_at_cap(self, max_turns: int, max_tokens: int) -> bool:
        """True if the envelope has reached its rollover cap (turns OR tokens)."""
        if max_turns > 0 and self.turn_count >= max_turns:
            return True
        if max_tokens > 0 and self.total_tokens >= max_tokens:
            return True
        return False

    # ── Redaction (Phase D fills in the HTTP API) ─────────────────────

    def redact_turn(self, execution_id: str, reason: str) -> bool:
        """Tombstone a turn's content, preserving record_hash + metadata.

        Returns True if a matching turn was found and scrubbed, False otherwise.
        The envelope itself becomes the permanent append-only record of the
        redaction event via :attr:`updated_at`.
        """
        for turn in self.turns:
            if turn.get("execution_id") == execution_id:
                turn["prompt_text"] = ""
                turn["response_content"] = ""
                turn["thinking_content"] = ""
                turn["redacted"] = True
                turn["redaction_reason"] = reason
                self.updated_at = _now_iso()
                return True
        return False

    # ── Serialisation ────────────────────────────────────────────────

    def to_wal_row(self) -> dict[str, Any]:
        """Flat dict suitable for INSERT OR REPLACE into gateway_sessions_envelope."""
        return {
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "gateway_id": self.gateway_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_turn_at": self.last_turn_at,
            "turn_count": self.turn_count,
            "status": self.status,
            "envelope_json": json.dumps(self.to_walacor_record(), default=str),
            "latest_record_hash": self.latest_record_hash,
            "latest_sequence_number": self.latest_sequence_number,
            "synced_to_walacor": 0,  # Phase B will flip this on successful submit
        }

    def to_walacor_record(self) -> dict[str, Any]:
        """Full record matching the walacor_gw_sessions schema (ETId 9000014)."""
        return {
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "gateway_id": self.gateway_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_turn_at": self.last_turn_at,
            "turn_count": self.turn_count,
            "status": self.status,
            "participant_user_id": self.participant_user_id,
            "participant_email": self.participant_email,
            "participant_team": self.participant_team,
            "participant_roles": json.dumps(self.participant_roles or [], default=str),
            "participant_source": self.participant_source,
            "turns_json": json.dumps(self.turns, default=str),
            "running_totals_json": json.dumps(
                {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "total_tokens": self.total_tokens,
                    "turn_count": self.turn_count,
                },
                default=str,
            ),
            "latest_record_hash": self.latest_record_hash,
            "first_record_hash": self.first_record_hash,
            "latest_sequence_number": self.latest_sequence_number,
            "parent_session_envelope_id": self.parent_session_envelope_id,
            "metadata_json": json.dumps(self.metadata or {}, default=str),
        }
