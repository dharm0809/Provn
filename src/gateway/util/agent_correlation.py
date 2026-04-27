"""Tier 0 of agent tracing: extract caller-supplied correlation IDs from a request.

Pure parser — no I/O, no settings. The orchestrator and completeness middleware
both call this so attempt rows and execution rows agree on the same fields.

Sources, in order of precedence:
1. W3C ``traceparent`` / ``tracestate`` headers (RFC 9110, version 00 only).
2. ``body.metadata`` LiteLLM-compatible bag:
   ``trace_id``, ``parent_span_id``, ``parent_observation_id``,
   ``agent_run_id``, ``agent_name``, ``parent_record_id``.
3. OpenAI Responses-API body fields: ``previous_response_id``, ``conversation_id``.

Header values win over body when both are present (the header is the W3C-blessed
propagation channel).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Mapping

# traceparent: version "-" trace-id "-" parent-id "-" trace-flags
# version=00, trace-id=32 hex, parent-id=16 hex, flags=2 hex (RFC 9110).
_TRACEPARENT_RE = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
    re.IGNORECASE,
)

# Bound the strings we persist so a hostile caller can't blow up the WAL.
_MAX_FIELD_LEN = 256


def _clip(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        v = str(v)
    v = v.strip()
    if not v:
        return None
    return v[:_MAX_FIELD_LEN]


@dataclass(frozen=True)
class AgentCorrelation:
    """Caller-declared agent-tracing context for one request.

    All fields are optional — uninstrumented agents leave them None and the
    gateway falls back to existing session-chain behaviour.
    """

    trace_id: str | None = None
    parent_span_id: str | None = None
    agent_run_id: str | None = None
    agent_name: str | None = None
    parent_observation_id: str | None = None
    parent_record_id: str | None = None
    previous_response_id: str | None = None
    conversation_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any(asdict(self).values())

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def parse_traceparent(header: str | None) -> tuple[str | None, str | None]:
    """Return (trace_id, parent_span_id) from a W3C traceparent header.

    Only version 00 is recognised. Malformed values silently return (None, None)
    — Tier 0 is fail-open: bad correlation IDs must never break a request.
    """
    if not header:
        return (None, None)
    m = _TRACEPARENT_RE.match(header.strip())
    if not m:
        return (None, None)
    return (m.group(1).lower(), m.group(2).lower())


def extract_correlation(
    headers: Mapping[str, str] | None,
    body: Mapping[str, Any] | None,
) -> AgentCorrelation:
    """Build an :class:`AgentCorrelation` from a request's headers + parsed body.

    ``headers`` is treated case-insensitively. ``body`` is the already-parsed
    JSON dict (or None for non-JSON bodies).
    """
    # Case-insensitive header lookup
    hdr: dict[str, str] = {}
    if headers:
        for k, v in headers.items():
            if isinstance(k, str) and isinstance(v, str):
                hdr[k.lower()] = v

    trace_id, parent_span_id = parse_traceparent(hdr.get("traceparent"))

    body_meta: Mapping[str, Any] = {}
    prev_response_id = None
    conversation_id = None
    if isinstance(body, Mapping):
        meta = body.get("metadata")
        if isinstance(meta, Mapping):
            body_meta = meta
        prev_response_id = _clip(body.get("previous_response_id"))
        conversation_id = _clip(body.get("conversation_id"))

    # Body fields fill in only what the header didn't already give us.
    if trace_id is None:
        trace_id = _clip(body_meta.get("trace_id"))
    if parent_span_id is None:
        parent_span_id = _clip(body_meta.get("parent_span_id"))

    return AgentCorrelation(
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        agent_run_id=_clip(body_meta.get("agent_run_id")),
        agent_name=_clip(body_meta.get("agent_name")),
        parent_observation_id=_clip(body_meta.get("parent_observation_id")),
        parent_record_id=_clip(body_meta.get("parent_record_id")),
        previous_response_id=prev_response_id,
        conversation_id=conversation_id,
    )
