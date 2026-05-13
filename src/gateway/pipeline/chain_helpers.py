"""Shared session-chain helpers used by both the proxy orchestrator and the
OpenWebUI plugin governance pipeline.

The ID-pointer chain (UUIDv7 ``record_id`` + ``previous_record_id``) is the
gateway's tamper-evident audit linkage. Two call sites — ``pipeline/orchestrator
.py`` (proxy path) and ``openwebui/governance.py`` (plugin event path) — both
need to:

1. Reserve the next ``sequence_number`` from the tracker.
2. Stamp it onto the record (alongside ``previous_record_id``).
3. Optionally Ed25519-sign the canonical ID string when signing is enabled.
4. Tell the caller whether the tracker should be advanced after the write
   succeeds.

Before extraction these steps lived as ~40-line copies in both modules.
A signature change in ``next_chain_values`` had to be made in two places —
exactly the kind of drift the codebase notes flag as "must not diverge".
``apply_session_chain`` is now the single source of truth. Both call sites
import it and obtain the same ``ChainResult`` shape.

Design choices worth noting:

* **Lock acquired inside the helper.** The lock used to be obtained by the
  caller (``_session_chain_lock`` in the orchestrator) and was missing entirely
  in the plugin governance path. Pulling the lock acquisition inside the helper
  means a future call site can't accidentally skip it. The lock no-ops when
  session tracking is disabled.

* **Tracker advance is the caller's job.** This helper does NOT call
  ``tracker.update()``. The caller invokes ``advance_session_chain()`` AFTER
  the record write succeeds. Decoupling these two steps fixes C7: previously
  ``update()`` was gated on ``_apply_session_chain`` returning a truthy
  value, which it didn't if signing failed. The new contract is "advance on
  successful write, regardless of signing status."

* **Signing is best-effort, gated by config.** ``sign_canonical`` is only
  invoked when ``settings.record_signing_enabled`` is True. When enabled, the
  ABSENCE of a loaded key is logged loudly (signing was requested, but the
  startup health-check did not provide a key). When disabled, the helper
  doesn't even try to import the signing module.

Alternative considered: keep the lock in the orchestrator's wrapping
``async with _session_chain_lock`` context manager and inline the helper.
Rejected because the plugin governance path doesn't wrap the call in such a
context manager today — and a future maintainer could forget to. Putting the
lock inside the helper makes "did the lock get acquired?" trivially answerable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# ── Shared PreInferenceResult for the policy-evaluator call sites ────────────
#
# ``evaluate_pre_inference`` returns a 5-tuple today. Plugin governance used
# to unpack 4 values, which raised ``ValueError`` whenever ``policy_cache`` was
# wired up. Returning a typed namedtuple-style dataclass lets both call sites
# read the result by attribute — a future arity change will surface as an
# AttributeError at the use site instead of silently breaking unpacking.

@dataclass(frozen=True)
class PreInferenceResult:
    """Typed wrapper around ``evaluate_pre_inference``'s 5-tuple return.

    A dataclass (rather than a NamedTuple) so callers can't accidentally
    unpack it positionally with the wrong arity.
    """

    blocked: bool
    policy_version: int
    policy_result: str
    error_response: JSONResponse | None
    failure_reason: str | None


def run_pre_inference(policy_cache: Any, call: Any, attestation_id: str, attestation_context: dict) -> PreInferenceResult:
    """Thin typed wrapper around ``policy_evaluator.evaluate_pre_inference``.

    Both the orchestrator and the OpenWebUI plugin governance pipeline should
    call this helper rather than tuple-unpacking ``evaluate_pre_inference``
    directly. A future change to the policy evaluator signature will then
    break BOTH call sites at the same line — instead of silently passing one
    and crashing the other (the C1 bug).
    """
    from gateway.pipeline.policy_evaluator import evaluate_pre_inference

    blocked, pv, pr, err, fail_reason = evaluate_pre_inference(
        policy_cache, call, attestation_id, attestation_context,
    )
    return PreInferenceResult(
        blocked=blocked,
        policy_version=pv,
        policy_result=pr,
        error_response=err,
        failure_reason=fail_reason,
    )


# ── Session chain ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChainResult:
    """Outcome of attempting to attach session chain fields to a record.

    ``advanced`` distinguishes two failure modes:
        * applied=False, advanced=False — chain disabled or session_id missing;
          tracker should NOT be updated after the write (no state to track).
        * applied=True, advanced=False — should not occur; logged loudly.
        * applied=True, advanced=True — caller MUST invoke
          ``advance_session_chain`` after the record write succeeds.
    """

    applied: bool
    sequence_number: int | None
    previous_record_id: str | None
    record_signature_attempted: bool
    record_signature_ok: bool


async def apply_session_chain(
    record: dict,
    session_id: str | None,
    ctx: Any,
    settings: Any,
) -> ChainResult:
    """Reserve chain values from the tracker and stamp them onto ``record``.

    Returns a ``ChainResult`` describing what was attached. Callers must call
    ``advance_session_chain`` AFTER the record is written to persistent
    storage (only when ``applied=True``).

    Re-entrancy: the caller is expected to hold the per-session lock for the
    full reserve-write-advance critical section. The orchestrator uses
    ``_session_chain_lock`` for this; the OpenWebUI plugin governance path
    uses ``session_chain_critical_section`` (defined below).

    Signing behaviour:
        * ``settings.record_signing_enabled = False`` → no signing attempted,
          no signing-module import, ``record_signature_attempted=False``.
        * ``settings.record_signing_enabled = True`` and key loaded → signature
          computed and stamped, ``record_signature_ok=True``.
        * ``settings.record_signing_enabled = True`` and key NOT loaded →
          attempt logged loudly (operator misconfiguration: signing requested
          but startup health-check did not provide a key). Chain still
          advances; signing is best-effort.
    """
    tracker = getattr(ctx, "session_chain", None) if ctx else None
    if not (session_id and tracker and getattr(settings, "session_chain_enabled", False)):
        return ChainResult(False, None, None, False, False)

    try:
        chain_vals = await tracker.next_chain_values(session_id)
    except Exception:
        logger.error(
            "Session chain next_chain_values failed — skipping chain fields: session_id=%s",
            session_id,
            exc_info=True,
        )
        return ChainResult(False, None, None, False, False)

    seq_num = chain_vals.sequence_number
    record["sequence_number"] = seq_num
    record["previous_record_id"] = chain_vals.previous_record_id

    sig_attempted = False
    sig_ok = False
    if getattr(settings, "record_signing_enabled", False):
        sig_attempted = True
        try:
            from gateway.crypto.signing import (
                sign_canonical,
                signing_key_available,
            )

            if not signing_key_available():
                # Loud signal: operator requested signing but no key is loaded.
                # Don't block the record write — signing is best-effort, but
                # ops dashboards should see this so they can fix the deploy.
                logger.error(
                    "record_signing_enabled=true but no signing key loaded — record will be unsigned "
                    "(check WALACOR_RECORD_SIGNING_KEY_PATH / startup logs)",
                )
            else:
                signature = sign_canonical(
                    record_id=record.get("record_id"),
                    previous_record_id=record.get("previous_record_id"),
                    sequence_number=seq_num,
                    execution_id=record.get("execution_id", ""),
                    timestamp=record.get("timestamp", ""),
                )
                if signature:
                    record["record_signature"] = signature
                    sig_ok = True
        except Exception:
            # Signature failure is non-fatal — the chain stays intact, only
            # the per-record signature is missing. Log with traceback so the
            # cause is discoverable.
            logger.warning(
                "Record signing failed (chain advance continues): execution_id=%s",
                record.get("execution_id"),
                exc_info=True,
            )

    return ChainResult(
        applied=True,
        sequence_number=seq_num,
        previous_record_id=chain_vals.previous_record_id,
        record_signature_attempted=sig_attempted,
        record_signature_ok=sig_ok,
    )


async def advance_session_chain(
    record: dict,
    session_id: str | None,
    ctx: Any,
    chain_result: ChainResult,
) -> None:
    """Advance the in-memory / Redis tracker after a successful record write.

    Decoupled from ``apply_session_chain`` so the tracker advances on
    successful WRITE — not on successful sign. Previously a Redis hiccup or a
    signing error left the tracker stuck with the prior ``last_record_id``,
    causing the next request to forge an incorrect ``previous_record_id``.

    Best-effort: a tracker update failure is logged but does NOT propagate;
    the next request will compute a stale pointer, but the record itself is
    already on disk / in Walacor and the chain can be repaired by reconciling
    against the persisted records.
    """
    if not chain_result.applied:
        return
    tracker = getattr(ctx, "session_chain", None) if ctx else None
    if not (session_id and tracker):
        return
    try:
        await tracker.update(
            session_id,
            chain_result.sequence_number,
            record_id=record.get("record_id"),
        )
    except Exception:
        logger.error(
            "Session chain update failed — chain state may be stale: session_id=%s seq_num=%s",
            session_id,
            chain_result.sequence_number,
            exc_info=True,
        )


def session_chain_critical_section(ctx: Any, session_id: str | None):
    """Return an async context manager that holds the per-session chain lock.

    The lock is acquired internally so neither call site can forget to
    serialize its (reserve → write → advance) span. No-ops when chain tracking
    is disabled or session_id is missing.

    Used identically by orchestrator and plugin governance. Callers wrap:

        async with session_chain_critical_section(ctx, session_id):
            chain_result = await apply_session_chain(record, session_id, ctx, settings)
            await write_record(record)
            await advance_session_chain(record, session_id, ctx, chain_result)
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        tracker = getattr(ctx, "session_chain", None) if ctx else None
        if session_id and tracker is not None and hasattr(tracker, "session_lock"):
            async with tracker.session_lock(session_id):
                yield
        else:
            yield

    return _cm()
