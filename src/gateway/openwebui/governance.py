"""Governance pipeline for OpenWebUI plugin events.

Applies the same attestation → policy → session chain → WAL/Walacor write
steps that proxy requests get, but in audit-only mode (never blocking, since
the LLM response already happened).

Only **outlet** events run the full pipeline.  Inlet events get attestation +
policy evaluation and a lightweight audit record.
"""

from __future__ import annotations

import fnmatch
import logging
import uuid
from datetime import datetime, timezone

from gateway.adapters.base import ModelCall, ModelResponse
from gateway.cache.attestation_cache import CachedAttestation
from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context
from gateway.pipeline.hasher import build_execution_record
from gateway.pipeline.model_resolver import resolve_attestation
from gateway.pipeline.policy_evaluator import evaluate_pre_inference

logger = logging.getLogger(__name__)


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


def _build_model_response(event: dict) -> ModelResponse:
    """Construct a ``ModelResponse`` from an outlet event."""
    data = event.get("data") or {}
    governance = data.get("governance") or {}
    assistant_response = data.get("assistant_response") or ""
    response_length = data.get("response_length") or len(assistant_response)

    # Try to extract token counts from governance headers, else estimate
    usage = _extract_token_usage(governance, response_length)

    return ModelResponse(
        content=assistant_response,
        usage=usage,
        raw_body=b"{}",
        provider_request_id=governance.get("execution_id") or None,
    )


def _extract_token_usage(governance: dict, response_length: int) -> dict:
    """Extract or estimate token usage from governance headers."""
    # The proxy path doesn't expose token counts in headers directly,
    # so we estimate.  The estimate is consistent with the orchestrator's
    # budget check heuristic (len // 4).
    est_completion = max(response_length // 4, 1)
    return {
        "prompt_tokens": 0,
        "completion_tokens": est_completion,
        "total_tokens": est_completion,
        "token_source": "estimated",
    }


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

async def _apply_session_chain(record: dict, session_id: str | None, ctx, settings) -> bool:
    """Attach UUIDv7 ID-pointer session chain fields.

    Gateway no longer computes SHA3-512 record_hash — Walacor backend hashes on
    ingest. Chain integrity is maintained via `record_id` (UUIDv7) +
    `previous_record_id` pointers. Ed25519 signs the canonical ID string.
    """
    if not (session_id and ctx.session_chain and settings.session_chain_enabled):
        return False
    try:
        chain_vals = await ctx.session_chain.next_chain_values(session_id)
    except Exception:
        logger.error(
            "Plugin event session chain failed — skipping chain fields: session_id=%s",
            session_id, exc_info=True,
        )
        return False

    seq_num = chain_vals.sequence_number
    record["sequence_number"] = seq_num
    record["previous_record_id"] = chain_vals.previous_record_id

    # Ed25519 signing over canonical ID string (fail-open)
    try:
        from gateway.crypto.signing import sign_canonical

        signature = sign_canonical(
            record_id=record.get("record_id"),
            previous_record_id=record.get("previous_record_id"),
            sequence_number=seq_num,
            execution_id=record["execution_id"],
            timestamp=record["timestamp"],
        )
        if signature:
            record["record_signature"] = signature
    except Exception:
        pass

    return True

    return record_hash_val


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
            attestation, err = await resolve_attestation(
                ctx.attestation_cache, provider, model_id,
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
    if ctx.policy_cache:
        try:
            _, pv, pr, policy_err = evaluate_pre_inference(
                ctx.policy_cache, call, att_id, att_ctx,
            )
            policy_version = pv
            policy_result = pr
            if policy_err is not None:
                result["governance_status"] = "blocked_post_facto"
                result["policy_result"] = pr
                result["policy_version"] = pv
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
        return result

    # ── Steps 3-7: Outlet-only (full pipeline) ──────────────────────────

    # Build ModelResponse
    model_response = _build_model_response(event)

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

    result["execution_id"] = record["execution_id"]

    # Session chain
    try:
        await _apply_session_chain(record, session_id, ctx, settings)
        if "sequence_number" in record:
            result["sequence_number"] = record["sequence_number"]
    except Exception as exc:
        errors.append(f"session_chain: {exc}")
        logger.warning("Plugin event session chain failed: %s", exc, exc_info=True)

    # Dual-write (WAL + Walacor)
    try:
        if ctx.storage:
            write_result = await ctx.storage.write_execution(record)
            if not write_result.succeeded:
                errors.append(f"storage: write failed — {write_result.failed}")
    except Exception as exc:
        errors.append(f"storage: {exc}")
        logger.warning("Plugin event storage write failed: %s", exc, exc_info=True)

    # Final status
    if result.get("governance_status") == "skipped":
        result["governance_status"] = "pass" if policy_result == "pass" else "warn"

    if errors:
        result["errors"] = errors

    return result
