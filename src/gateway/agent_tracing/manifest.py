"""Pillar 4 of agent tracing: signed AgentRunManifest.

Schema follows ``docs/research/AGENT-TRACING-RESEARCH.md`` §10.3 Pillar 4. Each
manifest collapses one agent run — the LLM calls we observed, the tool events
we reconstructed, and the chain hash over every message we saw — into a single
artifact, Ed25519-signed and ready to anchor in Walacor under its own ETId.

This is the "moat" piece: the manifest is signed by a neutral component the
customer's platform team operates, not by the agent operator. That delivers
the Article-12-grade audit artifact MAIF / ExecMesh / the Audit-Trail-Paradox
literature has been describing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from gateway.crypto.signing import sign_bytes


# ── Sub-record types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMCallRef:
    record_id: str
    # Gateway-side SHA-256 over the canonical execution record. Populated
    # synchronously by _apply_session_chain at write time, so the manifest
    # always carries a content-bound hash for every LLM call it references —
    # independent of whether Walacor has anchored the record yet.
    record_hash: str | None
    # Walacor blockchain anchor hash. Populated post-anchor by the
    # lineage normalize step (env.DH); on sandbox this is permanently
    # null per the well-known sandbox-doesn't-anchor constraint.
    # A v1.1 background enrichment worker will fold this back into the
    # manifest as a side-record (we never re-sign the original).
    walacor_dh: str | None
    model: str | None
    timestamp: str


@dataclass(frozen=True)
class ReconstructedToolRef:
    kind: str
    tool_name: str | None
    tool_call_id: str | None
    tc_hash: str | None
    tr_hash: str | None
    turn_seq: int | None


@dataclass(frozen=True)
class FrameworkGuess:
    name: str          # e.g. "openai-agents-sdk", "claude-agent-sdk", "langchain-react", "unknown"
    confidence: float  # 0.0..1.0


# ── Top-level manifest ────────────────────────────────────────────────────────


@dataclass
class AgentRunManifest:
    """One signed artifact summarising a complete agent run."""

    run_id: str
    tenant_id: str
    caller_identity: dict[str, Any]
    trace_id: str | None
    framework_guess: FrameworkGuess
    start_ts: str
    end_ts: str
    end_reason: str  # "inactivity" | "explicit_close" | "ttl"
    llm_calls: list[LLMCallRef] = field(default_factory=list)
    reconstructed_tool_events: list[ReconstructedToolRef] = field(default_factory=list)
    message_chain_hash: str = ""
    intent_drift_score: float | None = None  # populated by Pillar 5 (v2)
    signature: str | None = None

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "caller_identity": dict(self.caller_identity),
            "trace_id": self.trace_id,
            "framework_guess": {
                "name": self.framework_guess.name,
                "confidence": self.framework_guess.confidence,
            },
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "end_reason": self.end_reason,
            # Aggregated counts — declared in the registered Walacor schema
            # so queries can filter without parsing the JSON arrays.
            "llm_call_count": len(self.llm_calls),
            "tool_event_count": len(self.reconstructed_tool_events),
            "llm_calls": [c.__dict__ for c in self.llm_calls],
            "reconstructed_tool_events": [
                e.__dict__ for e in self.reconstructed_tool_events
            ],
            "message_chain_hash": self.message_chain_hash,
            "intent_drift_score": self.intent_drift_score,
            "signature": self.signature,
        }

    def canonical_bytes(self) -> bytes:
        """Stable byte string used as the signing input.

        ``signature`` is excluded so the same payload yields the same bytes
        before and after signing.
        """
        d = self.to_dict()
        d.pop("signature", None)
        return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


# ── Helpers ──────────────────────────────────────────────────────────────────


def message_chain_hash(messages: Iterable[Any]) -> str:
    """SHA-256 over a chain of all observed messages.

    The doc names this a "merkle_root"; for v1 we use a chained sha256 — same
    integrity property (any single bit change ripples to the root), simpler
    code, and consistent with the gateway's existing ID-pointer chain ethos.
    """
    h = hashlib.sha256()
    for m in messages:
        canon = json.dumps(m, sort_keys=True, separators=(",", ":"), default=str)
        h.update(canon.encode("utf-8"))
        h.update(b"\x00")  # separator so concatenation isn't ambiguous
    return h.hexdigest()


def sign_manifest(manifest: AgentRunManifest) -> AgentRunManifest:
    """Attach an Ed25519 signature in-place; returns the same manifest.

    Fail-open: when the gateway has no signing key configured, signature
    stays ``None`` and the manifest is still anchored — verifiability is
    reported as "unsigned" rather than failing the run.
    """
    sig = sign_bytes(manifest.canonical_bytes())
    manifest.signature = sig
    return manifest
