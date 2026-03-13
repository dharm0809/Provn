"""Embedded control plane CRUD API route handlers."""

from __future__ import annotations

import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)

_start_time = time.time()


def _store_or_503():
    ctx = get_pipeline_context()
    if ctx.control_store is None:
        return None
    return ctx.control_store


def _tenant(request: Request) -> str:
    settings = get_settings()
    return request.query_params.get("tenant_id", settings.gateway_tenant_id or "")


# ── Cache refresh helpers ─────────────────────────────────────

def _refresh_attestation_cache() -> None:
    """Repopulate attestation cache from DB after mutation."""
    ctx = get_pipeline_context()
    settings = get_settings()
    store = ctx.control_store
    if store is None or ctx.attestation_cache is None:
        return
    tenant_id = settings.gateway_tenant_id
    ctx.attestation_cache.clear()
    proofs = store.get_attestation_proofs(tenant_id)
    for p in proofs:
        ctx.attestation_cache.set_from_proof(p.get("provider", "ollama"), p)
    logger.info("Attestation cache refreshed: %d entries", len(proofs))
    # Invalidate /v1/models cache so OpenWebUI picks up changes immediately
    try:
        from gateway.models_api import _invalidate_models_cache
        _invalidate_models_cache()
    except Exception:
        pass


def _refresh_policy_cache() -> None:
    """Repopulate policy cache from DB after mutation."""
    ctx = get_pipeline_context()
    settings = get_settings()
    store = ctx.control_store
    if store is None or ctx.policy_cache is None:
        return
    tenant_id = settings.gateway_tenant_id
    policies = store.get_active_policies(tenant_id)
    version = ctx.policy_cache.next_version()
    ctx.policy_cache.set_policies(version, policies)
    logger.info("Policy cache refreshed: %d active policies (version %d)", len(policies), version)


def _refresh_budget_tracker() -> None:
    """Sync budget tracker with DB after mutation."""
    ctx = get_pipeline_context()
    store = ctx.control_store
    if store is None or ctx.budget_tracker is None:
        return
    budgets = store.list_budgets()
    # Track which keys are in DB
    db_keys: set[tuple[str, str]] = set()
    for b in budgets:
        tid = b["tenant_id"]
        user = b.get("user", "")
        ctx.budget_tracker.configure(tid, user or None, b["period"], b["max_tokens"])
        db_keys.add((tid, user))
    # Remove budgets no longer in DB
    if hasattr(ctx.budget_tracker, "remove"):
        existing_keys = set(ctx.budget_tracker._states.keys()) if hasattr(ctx.budget_tracker, "_states") else set()
        for key in existing_keys - db_keys:
            ctx.budget_tracker.remove(key[0], key[1] or None)
    logger.info("Budget tracker refreshed: %d budgets", len(budgets))


# ── Attestation endpoints ─────────────────────────────────────

async def control_list_attestations(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    tenant_id = _tenant(request)
    try:
        rows = store.list_attestations(tenant_id)
        return JSONResponse({"attestations": rows, "count": len(rows)})
    except Exception as e:
        logger.error("control_list_attestations error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def control_upsert_attestation(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    try:
        body = await request.json()
        settings = get_settings()
        if "tenant_id" not in body:
            body["tenant_id"] = settings.gateway_tenant_id or ""
        result = store.upsert_attestation(body)
        _refresh_attestation_cache()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        logger.error("control_upsert_attestation error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)


async def control_delete_attestation(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    attestation_id = request.path_params["id"]
    try:
        deleted = store.delete_attestation(attestation_id)
        if deleted:
            _refresh_attestation_cache()
        return JSONResponse({"deleted": deleted})
    except Exception as e:
        logger.error("control_delete_attestation error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Policy endpoints ──────────────────────────────────────────

async def control_list_policies(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    tenant_id = _tenant(request)
    try:
        rows = store.list_policies(tenant_id)
        return JSONResponse({"policies": rows, "count": len(rows)})
    except Exception as e:
        logger.error("control_list_policies error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def control_create_policy(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    try:
        body = await request.json()
        settings = get_settings()
        if "tenant_id" not in body:
            body["tenant_id"] = settings.gateway_tenant_id or ""
        result = store.create_policy(body)
        _refresh_policy_cache()
        return JSONResponse(result, status_code=201)
    except Exception as e:
        logger.error("control_create_policy error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)


async def control_update_policy(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    policy_id = request.path_params["id"]
    try:
        body = await request.json()
        updated = store.update_policy(policy_id, body)
        if updated:
            _refresh_policy_cache()
        return JSONResponse({"updated": updated})
    except Exception as e:
        logger.error("control_update_policy error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)


async def control_delete_policy(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    policy_id = request.path_params["id"]
    try:
        deleted = store.delete_policy(policy_id)
        if deleted:
            _refresh_policy_cache()
        return JSONResponse({"deleted": deleted})
    except Exception as e:
        logger.error("control_delete_policy error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Budget endpoints ──────────────────────────────────────────

async def control_list_budgets(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    tenant_id = _tenant(request)
    try:
        rows = store.list_budgets(tenant_id)
        return JSONResponse({"budgets": rows, "count": len(rows)})
    except Exception as e:
        logger.error("control_list_budgets error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def control_upsert_budget(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    try:
        body = await request.json()
        settings = get_settings()
        if "tenant_id" not in body:
            body["tenant_id"] = settings.gateway_tenant_id or ""
        result = store.upsert_budget(body)
        _refresh_budget_tracker()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        logger.error("control_upsert_budget error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)


async def control_delete_budget(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    budget_id = request.path_params["id"]
    try:
        deleted = store.delete_budget(budget_id)
        if deleted:
            _refresh_budget_tracker()
        return JSONResponse({"deleted": deleted})
    except Exception as e:
        logger.error("control_delete_budget error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Content Policies ──────────────────────────────────────────────────────────

def _refresh_content_policies() -> None:
    """Reload content policies from store into running analyzers."""
    from gateway.pipeline.response_evaluator import clear_analysis_cache

    ctx = get_pipeline_context()
    store = ctx.control_store
    if not store:
        return
    policies = store.list_content_policies()
    for analyzer in ctx.content_analyzers:
        aid = getattr(analyzer, "analyzer_id", None)
        if aid and hasattr(analyzer, "configure"):
            relevant = [p for p in policies if p["analyzer_id"] == aid]
            analyzer.configure(relevant)
    clear_analysis_cache()
    logger.info("Content policies refreshed: %d rules across %d analyzers",
                len(policies), len(ctx.content_analyzers))


async def control_list_content_policies(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    analyzer_id = request.query_params.get("analyzer_id")
    policies = store.list_content_policies(analyzer_id=analyzer_id)
    return JSONResponse({"policies": policies, "count": len(policies)})


async def control_upsert_content_policy(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    tenant = body.get("tenant_id", _tenant(request))
    analyzer_id = body.get("analyzer_id")
    category = body.get("category")
    action = body.get("action", "warn")
    if not analyzer_id or not category:
        return JSONResponse({"error": "analyzer_id and category required"}, status_code=400)
    if action not in ("block", "warn", "pass"):
        return JSONResponse({"error": "action must be block, warn, or pass"}, status_code=400)
    threshold = float(body.get("threshold", 0.5))
    policy = store.upsert_content_policy(tenant, analyzer_id, category, action, threshold)
    _refresh_content_policies()
    return JSONResponse(policy, status_code=201)


async def control_delete_content_policy(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    policy_id = request.path_params["policy_id"]
    deleted = store.delete_content_policy(policy_id)
    if not deleted:
        return JSONResponse({"error": "Not found"}, status_code=404)
    _refresh_content_policies()
    return JSONResponse({"deleted": True})


# ── Status endpoint ───────────────────────────────────────────

async def control_status(request: Request) -> JSONResponse:
    """Comprehensive gateway status for the control dashboard."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    settings = get_settings()
    ctx = get_pipeline_context()

    status: dict = {
        "gateway_id": settings.gateway_id,
        "tenant_id": settings.gateway_tenant_id,
        "enforcement_mode": settings.enforcement_mode,
        "skip_governance": settings.skip_governance,
        "uptime_seconds": int(time.time() - _start_time),
        "control_plane_enabled": True,
    }

    if ctx.attestation_cache:
        status["attestation_cache"] = {
            "entries": ctx.attestation_cache.entry_count,
        }
    if ctx.policy_cache:
        status["policy_cache"] = {
            "version": ctx.policy_cache.version,
            "stale": ctx.policy_cache.is_stale,
            "last_sync": ctx.policy_cache.last_sync.isoformat() if ctx.policy_cache.last_sync else None,
        }
    if ctx.wal_writer:
        status["wal"] = {
            "pending_records": ctx.wal_writer.pending_count(),
            "disk_usage_bytes": ctx.wal_writer.disk_usage_bytes(),
        }
    if ctx.sync_client:
        status["sync_mode"] = "remote"
        status["control_plane_url"] = settings.control_plane_url
    else:
        status["sync_mode"] = "local"

    # Auth & security
    status["auth_mode"] = settings.auth_mode
    status["jwt_configured"] = bool(settings.jwt_secret or settings.jwt_jwks_url)

    # Content analyzers
    if ctx.content_analyzers:
        status["content_analyzers"] = {
            "count": len(ctx.content_analyzers),
            "types": [type(a).__name__ for a in ctx.content_analyzers],
        }

    # Configured providers
    providers = []
    if settings.provider_ollama_url:
        providers.append({"name": "ollama", "url": settings.provider_ollama_url})
    if settings.provider_openai_key:
        providers.append({"name": "openai", "url": settings.provider_openai_url})
    if settings.provider_anthropic_key:
        providers.append({"name": "anthropic", "url": settings.provider_anthropic_url})
    if settings.provider_huggingface_key:
        providers.append({"name": "huggingface", "url": settings.provider_huggingface_url})
    status["providers"] = providers

    # Model routing
    if settings.model_routing_json:
        status["model_routes_count"] = len(settings.model_routes)

    # Session chain
    if ctx.session_chain:
        try:
            count = ctx.session_chain.active_session_count()
            status["session_chain"] = {"active_sessions": count}
        except Exception:
            logger.debug("control_status: session_chain unavailable", exc_info=True)

    # Token budget
    if ctx.budget_tracker and settings.token_budget_enabled:
        try:
            snapshot = await ctx.budget_tracker.get_snapshot(settings.gateway_tenant_id)
            if snapshot:
                status["token_budget"] = snapshot
        except Exception:
            logger.debug("control_status: token_budget unavailable", exc_info=True)

    # Lineage
    status["lineage_enabled"] = settings.lineage_enabled

    # Model capabilities
    try:
        from gateway.pipeline.orchestrator import _model_capabilities
        if _model_capabilities:
            status["model_capabilities"] = dict(_model_capabilities)
    except Exception:
        logger.debug("control_status: model_capabilities unavailable", exc_info=True)

    return JSONResponse(status)


# ── Discovery endpoint ────────────────────────────────────────

async def control_discover_models(request: Request) -> JSONResponse:
    """GET /v1/control/discover — scan providers for available models."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    ctx = get_pipeline_context()
    settings = get_settings()
    if not ctx.http_client:
        return JSONResponse({"error": "HTTP client not initialized"}, status_code=503)
    try:
        from gateway.control.discovery import discover_provider_models

        discovered = await discover_provider_models(settings, ctx.http_client)

        # Mark which models are already registered
        tenant_id = _tenant(request)
        attestations = store.list_attestations(tenant_id)
        registered_ids = {a["model_id"] for a in attestations}

        for m in discovered:
            m["registered"] = m["model_id"] in registered_ids

        return JSONResponse({"models": discovered, "count": len(discovered)})
    except Exception as e:
        logger.error("control_discover_models error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
