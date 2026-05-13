"""Governance pipeline for OpenWebUI plugin events.

Applies the same attestation → policy → session chain → WAL/Walacor write
steps that proxy requests get, but in audit-only mode (never blocking, since
the LLM response already happened).

Only **outlet** events run the full pipeline.  Inlet events get attestation +
policy evaluation and a lightweight audit record.

Source-of-truth note: the chain section MUST stay in lock-step with the proxy
orchestrator. The bit-for-bit copy of ``_apply_session_chain`` that used to
live in this module has been deleted in favour of
``gateway.pipeline.chain_helpers``. Similarly, ``evaluate_pre_inference`` is
called via ``run_pre_inference`` so a future arity change breaks both call
sites at the same line (the pre-fix code unpacked 4 values from a 5-tuple,
raising ``ValueError`` whenever the policy cache was wired up — see C1).
"""

from __future__ import annotations

import fnmatch
import logging
from datetime import datetime, timezone

from gateway.adapters.base import ModelCall, ModelResponse
from gateway.cache.attestation_cache import CachedAttestation
from gateway.config import get_settings
from gateway.pipeline.chain_helpers import (
    advance_session_chain,
    apply_session_chain,
    run_pre_inference,
    session_chain_critical_section,
)
from gateway.pipeline.context import get_pipeline_context
from gateway.pipeline.hasher import build_execution_record
from gateway.pipeline.model_resolver import resolve_attestation
from gateway.util.request_context import new_request_id

logger = logging.getLogger(__name__)


# Map ``governance_status`` -> attempt-row ``disposition`` so the
# completeness invariant uses the same vocabulary as the proxy path
# (see _set_disposition in pipeline/orchestrator.py).  Plugin events
# never block; ``blocked_post_facto`` is the audit-only verdict for
# events that would have been blocked at the proxy.
_PLUGIN_STATUS_TO_DISPOSITION = {
    "pass": "allowed",
    "warn": "allowed",
    "skipped": "allowed",
    "blocked_post_facto": "blocked_post_facto",
}


async def _write_plugin_attempt(
    ctx,
    settings,
    *,
    event_type: str,
    provider: str,
    model_id: str,
    user_id: str | None,
    status: str,
    reason: str | None,
    execution_id: str | None,
) -> None:
    """Append one gateway_attempts row for a plugin event.

    Plugin events bypass ``completeness_middleware`` (``/v1/openwebui/*``
    is on the skip-list) because the request shape is governed via
    ``process_plugin_event`` rather than the standard chat/completions
    pipeline. The completeness invariant still applies — every governed
    request must have an attempt row — so we synthesize one here once
    the governance decision is finalized.

    Alternative considered (and rejected): remove ``/v1/openwebui`` from
    the middleware skip-list and let the standard finally-block run.
    Rejected because the middleware can't see the inlet/outlet split or
    the post-facto verdict; the disposition column would always be
    ``error_gateway`` for these requests and operators couldn't tell a
    plugin block from a real handler crash.
    """
    if not ctx.storage:
        return
    record = {
        "request_id": new_request_id(),
        "tenant_id": settings.gateway_tenant_id or "",
        "path": "/v1/openwebui/events",
        "disposition": _PLUGIN_STATUS_TO_DISPOSITION.get(status, "allowed"),
        "status_code": 200,  # plugin events always return 200 — never block
        "provider": provider or None,
        "model_id": model_id or None,
        "execution_id": execution_id or None,
        "user": user_id or None,
        "reason": reason or f"plugin_event:{event_type}",
    }
    try:
        await ctx.storage.write_attempt(record)
    except Exception:
        logger.warning("Plugin event write_attempt failed", exc_info=True)


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

def resolve_provider_for_model(model_id: str) -> str:
    """Resolve a model string to a provider name.

    Uses ``settings.model_routes`` with fnmatch matching (same logic as
    ``_resolve_adapter`` in the orchestrator), falling back to
    ``settings.gateway_provider``.
    """
    settings = get_settings()
    if model_id and settings.model_routes:
        for route in settings.model_routes:
            if fnmatch.fnmatch(model_id.lower(), route.get("pattern", "").lower()):
                return route.get("provider", settings.gateway_provider)
    return settings.gateway_provider


# ---------------------------------------------------------------------------
# ModelCall / ModelResponse builders
# ---------------------------------------------------------------------------

def _build_model_call(event: dict, provider: str) -> ModelCall:
    """Construct a ``ModelCall`` from a plugin event."""
    model_id = event.get("model") or ""
    user_info = event.get("user") or {}
    chat_id = event.get("chat_id") or ""
    data = event.get("data") or {}

    # Concatenate all messages into prompt text
    all_messages = data.get("all_messages") or []
    parts: list[str] = []
    for msg in all_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if content:
            parts.append(f"[{role}] {content}")
    prompt_text = "\n".join(parts)

    return ModelCall(
        provider=provider,
        model_id=model_id,
        prompt_text=prompt_text,
        raw_body=b"{}",
        is_streaming=False,
        metadata={
            "user": user_info.get("id", ""),
            "session_id": f"owui:{chat_id}",
            "event_source": "openwebui_plugin",
            "chat_id": chat_id,
            "message_id": event.get("message_id", ""),
        },
    )


def _build_model_response(event: dict) -> tuple[ModelResponse, bool]:
    """Construct a ``ModelResponse`` from an outlet event.

    Returns ``(response, tokens_were_estimated)``. The boolean is plumbed up
    to the execution record as the TOP-LEVEL ``tokens_estimated`` field (C8).
    The previous design only marked it inside ``metadata.token_source`` which
    Walacor's metadata-keep filter drops on long prompts — meaning auditors
    couldn't tell whether the token counts came from the provider or from a
    rough heuristic.
    """
    data = event.get("data") or {}
    governance = data.get("governance") or {}
    assistant_response = data.get("assistant_response") or ""
    response_length = data.get("response_length") or len(assistant_response)

    usage, estimated = _extract_token_usage(governance, response_length)

    return (
        ModelResponse(
            content=assistant_response,
            usage=usage,
            raw_body=b"{}",
            provider_request_id=governance.get("execution_id") or None,
        ),
        estimated,
    )


def _extract_token_usage(governance: dict, response_length: int) -> tuple[dict, bool]:
    """Extract or estimate token usage from governance headers.

    Returns ``(usage_dict, estimated)`` so callers can mark the record's
    top-level ``tokens_estimated`` field. The proxy path doesn't expose token
    counts in headers directly, so the heuristic is the same len//4 that the
    budget check uses — but the audit record needs to make that distinction
    visible.
    """
    est_completion = max(response_length // 4, 1)
    return (
        {
            "prompt_tokens": 0,
            "completion_tokens": est_completion,
            "total_tokens": est_completion,
            "token_source": "estimated",
        },
        True,  # always estimated for plugin events until governance headers carry real counts
    )


# ---------------------------------------------------------------------------
# Auto-attestation (lightweight, no adapter dependency)
# ---------------------------------------------------------------------------

async def _auto_attest(ctx, settings, provider: str, model_id: str) -> tuple[str, dict]:
    """Auto-attest a model for plugin governance.

    Mirrors the orchestrator's auto-attestation logic but without requiring a
    ProviderAdapter.  Returns ``(attestation_id, att_ctx)``.
    """
    att_id = f"self-attested:{model_id}"
    att_ctx = {
        "model_id": model_id,
        "provider": provider,
        "status": "active",
        "verification_level": "self_attested",
        "tenant_id": settings.gateway_tenant_id,
    }

    # Only auto-attest if the model wasn't explicitly revoked in the control store
    if ctx.control_store is not None:
        existing = [
            a for a in ctx.control_store.list_attestations(settings.gateway_tenant_id)
            if a.get("model_id") == model_id and a.get("provider") == provider
        ]
        if existing and existing[0].get("status") == "revoked":
            att_ctx["status"] = "revoked"
            return att_id, att_ctx

    auto_att = CachedAttestation(
        attestation_id=att_id,
        model_id=model_id,
        provider=provider,
        status="active",
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=settings.attestation_cache_ttl,
        tenant_id=settings.gateway_tenant_id,
        verification_level="self_attested",
    )
    ctx.attestation_cache.set(auto_att)

    if ctx.control_store is not None:
        ctx.control_store.upsert_attestation({
            "attestation_id": att_id,
            "model_id": model_id,
            "provider": provider,
            "status": "active",
            "verification_level": "auto_attested",
            "tenant_id": settings.gateway_tenant_id,
            "notes": "Auto-attested via OpenWebUI plugin event",
        })

    return att_id, att_ctx


# ---------------------------------------------------------------------------
# Session chain helper
# ---------------------------------------------------------------------------
#
# The plugin governance path used to ship its own copy of `_apply_session_chain`
# — the same code lived in `pipeline/orchestrator.py`. Two consequences:
#   1. Concurrent OWUI outlet events for the same chat_id had no per-session
#      lock here (the orchestrator's `_session_chain_lock` was never imported),
#      so two events could read the same `last_record_id` and emit records
#      with duplicate `previous_record_id`. (C4)
#   2. A signature change to `next_chain_values` would have drifted between
#      the two copies. (C5)
#
# Both issues are fixed by sharing the helper in `gateway.pipeline.chain_helpers`.
# `session_chain_critical_section` here wraps the same per-session lock the
# orchestrator uses; `apply_session_chain` + `advance_session_chain` reserve
# and commit chain state with identical semantics to the proxy path.


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def process_plugin_event(event: dict) -> dict:
    """Run the governance pipeline on a plugin event.

    Returns a result dict with governance outcomes.  Never raises — all
    errors are caught and reported in the result.
    """
    ctx = get_pipeline_context()
    settings = get_settings()
    event_type = event.get("event_type", "")
    model_id = event.get("model") or ""
    chat_id = event.get("chat_id") or ""
    errors: list[str] = []

    result: dict = {
        "governance_status": "skipped",
        "event_type": event_type,
    }

    # Skip if no storage backend available (nothing to write to)
    if not ctx.storage:
        return result

    provider = resolve_provider_for_model(model_id)

    # Build ModelCall from event data
    call = _build_model_call(event, provider)

    # ── Step 1: Attestation ──────────────────────────────────────────────
    att_id = f"plugin:{model_id}"
    att_ctx: dict = {"model_id": model_id, "provider": provider,
                     "status": "active", "tenant_id": settings.gateway_tenant_id}
    policy_version = 0
    policy_result = "skipped"

    if ctx.attestation_cache:
        try:
            # Plugin events have no Request — fall back to the gateway-level
            # tenant. This matches the behaviour before per-caller tenant
            # plumbing was introduced (plugin events reach this path only
            # via `_apply_governance` after the proxy already ran).
            attestation, err = await resolve_attestation(
                ctx.attestation_cache, provider, model_id,
                tenant_id=settings.gateway_tenant_id or "",
            )
            if err is not None:
                # Try auto-attestation (no sync client for plugin events)
                if ctx.sync_client is None:
                    att_id, att_ctx = await _auto_attest(ctx, settings, provider, model_id)
                    if att_ctx.get("status") == "revoked":
                        result["governance_status"] = "blocked_post_facto"
                        result["attestation_id"] = att_id
                        result["reason"] = "model_revoked"
                        # Don't return — still write the record
                else:
                    result["governance_status"] = "blocked_post_facto"
                    result["reason"] = "attestation_not_found"
            else:
                att_id = attestation.attestation_id
                att_ctx = {
                    "model_id": model_id,
                    "provider": getattr(attestation, "provider", provider),
                    "status": getattr(attestation, "status", "active"),
                    "verification_level": getattr(attestation, "verification_level", "self_reported"),
                    "tenant_id": attestation.tenant_id or settings.gateway_tenant_id,
                }
        except Exception as exc:
            errors.append(f"attestation: {exc}")
            logger.warning("Plugin event attestation check failed: %s", exc, exc_info=True)

    result["attestation_id"] = att_id

    # ── Step 2: Pre-policy ───────────────────────────────────────────────
    # C1: ``evaluate_pre_inference`` returns 5 values, not 4. The pre-fix code
    # unpacked 4 and silently raised ``ValueError`` whenever ``policy_cache``
    # was configured — caught only by the broad ``except Exception`` below,
    # so the symptom looked like "policy unavailable" rather than a hard bug.
    # ``run_pre_inference`` returns a typed ``PreInferenceResult`` so a future
    # arity change surfaces as ``AttributeError`` at the use site instead.
    if ctx.policy_cache:
        try:
            pre = run_pre_inference(ctx.policy_cache, call, att_id, att_ctx)
            policy_version = pre.policy_version
            policy_result = pre.policy_result
            if pre.error_response is not None:
                result["governance_status"] = "blocked_post_facto"
                result["policy_result"] = pre.policy_result
                result["policy_version"] = pre.policy_version
                if pre.failure_reason:
                    result["policy_failure_reason"] = pre.failure_reason
                # Don't return — still write the audit record for outlet events
        except Exception as exc:
            errors.append(f"policy: {exc}")
            logger.warning("Plugin event policy check failed: %s", exc, exc_info=True)

    result["policy_version"] = policy_version
    result["policy_result"] = policy_result

    # ── Inlet events: lightweight record only ────────────────────────────
    if event_type != "outlet":
        if result["governance_status"] == "skipped":
            result["governance_status"] = "pass" if policy_result == "pass" else "warn"
        if errors:
            result["errors"] = errors
        # Completeness invariant: even though inlet events skip the full
        # pipeline, they are still governed requests and must produce
        # exactly one gateway_attempts row. Disposition reflects the
        # pre-inference verdict — proxy-side blocks would be
        # blocked_post_facto here too.
        await _write_plugin_attempt(
            ctx,
            settings,
            event_type=event_type,
            provider=provider,
            model_id=model_id,
            user_id=(event.get("user") or {}).get("id"),
            status=result.get("governance_status", "skipped"),
            reason=result.get("reason"),
            execution_id=None,  # inlet events don't write execution records
        )
        return result

    # ── Steps 3-7: Outlet-only (full pipeline) ──────────────────────────

    # Build ModelResponse — `tokens_estimated` is plumbed up to the top-level
    # execution record field so it survives Walacor metadata truncation (C8).
    model_response, tokens_estimated = _build_model_response(event)

    # Content analysis (post-inference)
    rp_version = 0
    rp_result = "skipped"
    rp_decisions: list[dict] = []
    if ctx.content_analyzers and ctx.policy_cache and settings.response_policy_enabled:
        try:
            from gateway.pipeline.response_evaluator import evaluate_post_inference

            text_to_analyze = model_response.content or ""
            if text_to_analyze:
                _, rp_version, rp_result, rp_decisions, _ = await evaluate_post_inference(
                    ctx.policy_cache, model_response, ctx.content_analyzers,
                )
        except Exception as exc:
            errors.append(f"content_analysis: {exc}")
            logger.warning("Plugin event content analysis failed: %s", exc, exc_info=True)

    # Build metadata
    user_info = event.get("user") or {}
    governance_headers = (event.get("data") or {}).get("governance") or {}
    metadata: dict = {
        "event_source": "openwebui_plugin",
        "original_execution_id": governance_headers.get("execution_id", ""),
        "openwebui_user_name": user_info.get("name", ""),
        "openwebui_user_email": user_info.get("email", ""),
    }
    if rp_decisions:
        metadata["analyzer_decisions"] = rp_decisions
    if rp_result != "skipped":
        metadata["response_policy_version"] = rp_version
        metadata["response_policy_result"] = rp_result

    # Build execution record
    session_id = f"owui:{chat_id}"
    record = build_execution_record(
        call=call,
        model_response=model_response,
        attestation_id=att_id,
        policy_version=policy_version,
        policy_result=policy_result,
        tenant_id=settings.gateway_tenant_id,
        gateway_id=settings.gateway_id,
        user=user_info.get("id"),
        session_id=session_id,
        metadata=metadata,
        model_id=model_id,
        provider=provider,
    )
    # C8: surface "we estimated these counts" at the top level so it survives
    # Walacor's metadata-keep filter. Dashboard renders an "estimated tokens"
    # badge on records where this is True. Stays False on records where the
    # provider reported real counts (proxy path).
    if tokens_estimated:
        record["tokens_estimated"] = True

    result["execution_id"] = record["execution_id"]

    # ── Session chain + write (C4 + C5 + C7) ────────────────────────────
    # The plugin governance path used to call its own copy of
    # `_apply_session_chain` WITHOUT acquiring `_session_chain_lock` — meaning
    # two concurrent OWUI outlet events for the same chat_id could read the
    # same `last_record_id` and emit records with duplicate
    # `previous_record_id`. The shared helper now owns the lock so neither
    # call site can forget it.
    #
    # Also: tracker advance happens only on successful WRITE, not on
    # successful sign (C7).
    async with session_chain_critical_section(ctx, session_id):
        try:
            chain_result = await apply_session_chain(record, session_id, ctx, settings)
            if "sequence_number" in record:
                result["sequence_number"] = record["sequence_number"]
        except Exception as exc:
            errors.append(f"session_chain: {exc}")
            logger.warning("Plugin event session chain failed: %s", exc, exc_info=True)
            chain_result = None

        # Dual-write (WAL + Walacor)
        wrote_ok = False
        try:
            if ctx.storage:
                write_result = await ctx.storage.write_execution(record)
                if write_result.succeeded:
                    wrote_ok = True
                else:
                    errors.append(f"storage: write failed — {write_result.failed}")
        except Exception as exc:
            errors.append(f"storage: {exc}")
            logger.warning("Plugin event storage write failed: %s", exc, exc_info=True)

        # Advance tracker only on successful write — not gated on signing
        # success. A signing failure leaves `record_signature` null but the
        # ID-pointer chain still advances cleanly.
        if wrote_ok and chain_result is not None:
            await advance_session_chain(record, session_id, ctx, chain_result)

    # Final status
    if result.get("governance_status") == "skipped":
        result["governance_status"] = "pass" if policy_result == "pass" else "warn"

    if errors:
        result["errors"] = errors

    # Completeness invariant for outlet events. process_plugin_event is
    # the only governance path for /v1/openwebui/events, which is on the
    # completeness_middleware skip-list, so we write the attempt row
    # here once the final verdict is known.
    await _write_plugin_attempt(
        ctx,
        settings,
        event_type=event_type,
        provider=provider,
        model_id=model_id,
        user_id=(event.get("user") or {}).get("id"),
        status=result.get("governance_status", "pass"),
        reason=result.get("reason"),
        execution_id=result.get("execution_id"),
    )

    return result
