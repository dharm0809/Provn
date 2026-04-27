"""Pillar 2 of agent tracing: content fingerprinting + intra-tenant stitching.

Hashes every tool call and tool result on a stable canonical form so that the
same content recurring across two callers can be detected and (carefully)
stitched into a single agent run. The §11.1 corroboration rule guards against
semantic collisions (``search("weather")`` from two unrelated agents).

Fingerprint definitions (§10.3 Pillar 2):
    tc_hash = sha256(tool_name + canonical_json(args))
    tr_hash = sha256(content_normalized)

Stitch policy: never auto-stitch on a single low-entropy ``tc_hash``. Require
ANY of (a) ``tr_hash`` match, (b) two-or-more sequenced ``tc_hash`` matches,
(c) ``traceparent`` overlap, (d) shared ``caller_identity``. A single
``tc_hash`` match becomes a "possible related run" suggestion, never an
automatic edge.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


# ── Hashing primitives ────────────────────────────────────────────────────────


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hash_tool_call(tool_name: str | None, arguments: Any) -> str:
    """Stable SHA-256 over ``tool_name`` + canonical-JSON ``arguments``.

    ``arguments`` may arrive as a string (OpenAI shape — already JSON-encoded)
    or a dict (Anthropic shape). Decode-then-canonicalise so the two shapes
    produce identical hashes for identical semantic payloads.
    """
    args_obj: Any = arguments
    if isinstance(arguments, str):
        try:
            args_obj = json.loads(arguments)
        except Exception:
            args_obj = arguments  # fall back to the raw string
    return _sha256_hex(f"{tool_name or ''}|{_canonical_json(args_obj)}")


def hash_tool_result(content: Any) -> str:
    """Stable SHA-256 over a canonical-JSON normalisation of the result content."""
    return _sha256_hex(_canonical_json(content))


# ── Persistence ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FingerprintRow:
    fp_id: str          # uuid hex
    tenant_id: str
    record_id: str | None     # execution_id this fingerprint was observed inside
    caller_key: str | None    # narrow scope used by the reconstructor
    tool_call_id: str | None
    tool_name: str | None
    tc_hash: str | None       # null for tool_result rows
    tr_hash: str | None       # null for tool_call rows
    trace_id: str | None
    agent_run_id: str | None
    kind: str                 # "tool_call" | "tool_result"
    seen_at: str              # ISO 8601


def fingerprints_from_recon_events(
    events: Iterable[Any],
    *,
    tenant_id: str,
    record_id: str | None,
    caller_key: str | None,
    trace_id: str | None,
    agent_run_id: str | None,
    seen_at: str,
) -> list[FingerprintRow]:
    """Materialise fingerprint rows from a list of ReconstructionEvent objects.

    Tool-call events carry an ``args_hash`` already (computed by the engine);
    we treat that as the ``tc_hash``. Tool-result events carry ``content_hash``
    which becomes the ``tr_hash``. ``new_user_turn`` events do not produce a
    fingerprint row.
    """
    import uuid as _uuid
    out: list[FingerprintRow] = []
    for ev in events:
        kind = getattr(ev, "kind", None)
        if kind == "tool_call_observed":
            out.append(FingerprintRow(
                fp_id=_uuid.uuid4().hex,
                tenant_id=tenant_id,
                record_id=record_id,
                caller_key=caller_key,
                tool_call_id=getattr(ev, "tool_call_id", None),
                tool_name=getattr(ev, "tool_name", None),
                tc_hash=getattr(ev, "args_hash", None),
                tr_hash=None,
                trace_id=trace_id,
                agent_run_id=agent_run_id,
                kind="tool_call",
                seen_at=seen_at,
            ))
        elif kind == "tool_result_observed":
            out.append(FingerprintRow(
                fp_id=_uuid.uuid4().hex,
                tenant_id=tenant_id,
                record_id=record_id,
                caller_key=caller_key,
                tool_call_id=getattr(ev, "tool_call_id", None),
                tool_name=None,
                tc_hash=None,
                tr_hash=getattr(ev, "content_hash", None),
                trace_id=trace_id,
                agent_run_id=agent_run_id,
                kind="tool_result",
                seen_at=seen_at,
            ))
    return out


# ── Corroboration query ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class StitchCandidate:
    """One row returned by :func:`find_stitches` describing a related caller.

    ``confidence`` is one of ``"high"`` (rule a, c, or d), ``"medium"`` (rule
    b — multiple sequenced tc_hash matches), or ``"low"`` (single tc_hash —
    suggestion only, never an automatic edge).
    """

    other_caller_key: str
    other_record_id: str | None
    confidence: str
    reasons: tuple[str, ...]
    matched_tc_hashes: tuple[str, ...] = ()
    matched_tr_hashes: tuple[str, ...] = ()


def find_stitches(
    *,
    tenant_id: str,
    caller_key: str,
    tc_hashes: Sequence[str],
    tr_hashes: Sequence[str],
    trace_id: str | None,
    other_rows: Iterable[Mapping[str, Any]],
) -> list[StitchCandidate]:
    """Apply the §11.1 corroboration rule against a candidate set of rows.

    ``other_rows`` is the intra-tenant fingerprint corpus (excluding our own
    caller). Each row should expose at minimum: ``caller_key``, ``record_id``,
    ``tc_hash``, ``tr_hash``, ``trace_id``.

    Returns one :class:`StitchCandidate` per other caller, sorted high→low
    confidence then by total matches.
    """
    if tenant_id is None:  # defensive — caller controls scope
        return []
    tc_set = {h for h in tc_hashes if h}
    tr_set = {h for h in tr_hashes if h}

    by_caller: dict[str, dict[str, Any]] = {}
    for row in other_rows:
        if row.get("caller_key") == caller_key:
            continue  # exclude self
        ck = str(row.get("caller_key") or "")
        bucket = by_caller.setdefault(ck, {
            "record_id": row.get("record_id"),
            "tc_hits": set(),
            "tr_hits": set(),
            "trace_match": False,
        })
        rh_tc = row.get("tc_hash")
        rh_tr = row.get("tr_hash")
        if rh_tc and rh_tc in tc_set:
            bucket["tc_hits"].add(rh_tc)
        if rh_tr and rh_tr in tr_set:
            bucket["tr_hits"].add(rh_tr)
        if trace_id and row.get("trace_id") == trace_id:
            bucket["trace_match"] = True

    candidates: list[StitchCandidate] = []
    for ck, b in by_caller.items():
        reasons: list[str] = []
        if b["tr_hits"]:
            reasons.append("tr_hash_match")
        if len(b["tc_hits"]) >= 2:
            reasons.append("multi_tc_hash_match")
        if b["trace_match"]:
            reasons.append("traceparent_match")
        # Single tc_hash: suggestion-only, lowest tier.
        single_tc_only = (
            not reasons and len(b["tc_hits"]) == 1
        )
        if reasons:
            confidence = "high" if (b["tr_hits"] or b["trace_match"]) else "medium"
        elif single_tc_only:
            confidence = "low"
            reasons.append("single_tc_hash")
        else:
            continue
        candidates.append(StitchCandidate(
            other_caller_key=ck,
            other_record_id=b.get("record_id"),
            confidence=confidence,
            reasons=tuple(reasons),
            matched_tc_hashes=tuple(sorted(b["tc_hits"])),
            matched_tr_hashes=tuple(sorted(b["tr_hits"])),
        ))

    _RANK = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (
        _RANK[c.confidence],
        -(len(c.matched_tc_hashes) + len(c.matched_tr_hashes)),
    ))
    return candidates
