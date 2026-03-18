"""Embedded control plane CRUD API route handlers."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context

_TEMPLATES_DIR = Path(__file__).parent / "templates"

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
    """Repopulate attestation cache from DB after mutation.

    Preserves auto-attested models that may only exist in cache (not DB)
    to prevent CRUD operations from accidentally de-attesting active models.
    """
    ctx = get_pipeline_context()
    settings = get_settings()
    store = ctx.control_store
    if store is None or ctx.attestation_cache is None:
        return
    tenant_id = settings.gateway_tenant_id

    # Snapshot auto-attested entries before clearing — these may not be in the DB
    # if they were attested at request time but the DB write didn't persist yet.
    preserved = {}
    for key, entry in list(ctx.attestation_cache._cache.items()):
        if getattr(entry, "verification_level", "") in ("self_attested", "auto_attested"):
            preserved[key] = entry

    ctx.attestation_cache.clear()
    proofs = store.get_attestation_proofs(tenant_id)
    for p in proofs:
        ctx.attestation_cache.set_from_proof(p.get("provider", "ollama"), p)

    # Restore auto-attested entries that weren't repopulated from DB
    restored = 0
    for key, entry in preserved.items():
        if ctx.attestation_cache._cache.get(key) is None:
            ctx.attestation_cache._cache[key] = entry
            restored += 1

    logger.info("Attestation cache refreshed: %d from DB, %d auto-attested preserved",
                len(proofs), restored)
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


# ── Model Pricing endpoints ───────────────────────────────────

async def control_list_pricing(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    try:
        rows = store.list_model_pricing()
        return JSONResponse({"pricing": rows, "count": len(rows)})
    except Exception as e:
        logger.error("control_list_pricing error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def control_upsert_pricing(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    try:
        body = await request.json()
        if not body.get("model_pattern"):
            return JSONResponse({"error": "model_pattern is required"}, status_code=400)
        result = store.upsert_model_pricing(body)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        logger.error("control_upsert_pricing error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)


async def control_delete_pricing(request: Request) -> JSONResponse:
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    pricing_id = request.path_params["id"]
    try:
        deleted = store.delete_model_pricing(pricing_id)
        return JSONResponse({"deleted": deleted})
    except Exception as e:
        logger.error("control_delete_pricing error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


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

        # Optional: verify model signatures (OpenSSF/Sigstore)
        if settings.model_signing_enabled:
            from gateway.control.signing import verify_model_signature

            for m in discovered:
                verified, details = await verify_model_signature(
                    m["model_id"], m["provider"], ctx.http_client
                )
                m["signature_verified"] = verified
                m["verification_details"] = details

        return JSONResponse({"models": discovered, "count": len(discovered)})
    except Exception as e:
        logger.error("control_discover_models error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Key-Policy Assignment endpoints ──────────────────────────

async def control_get_key_policies(request: Request) -> JSONResponse:
    """GET /v1/control/keys/{key_hash}/policies — list policies assigned to an API key."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    key_hash = request.path_params["key_hash"]
    try:
        policy_ids = store.get_key_policies(key_hash)
        return JSONResponse({"api_key_hash": key_hash, "policy_ids": policy_ids})
    except Exception as e:
        logger.error("control_get_key_policies error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def control_set_key_policies(request: Request) -> JSONResponse:
    """PUT /v1/control/keys/{key_hash}/policies — set (replace) all policy assignments for an API key."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    key_hash = request.path_params["key_hash"]
    try:
        body = await request.json()
        policy_ids = body.get("policy_ids", [])
        if not isinstance(policy_ids, list):
            return JSONResponse({"error": "policy_ids must be a list"}, status_code=400)
        store.set_key_policies(key_hash, policy_ids)
        return JSONResponse({"api_key_hash": key_hash, "policy_ids": policy_ids, "status": "updated"})
    except Exception as e:
        logger.error("control_set_key_policies error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)


async def control_remove_key_policy(request: Request) -> JSONResponse:
    """DELETE /v1/control/keys/{key_hash}/policies/{policy_id} — remove a single policy from an API key."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    key_hash = request.path_params["key_hash"]
    policy_id = request.path_params["policy_id"]
    try:
        removed = store.remove_key_policy(key_hash, policy_id)
        if not removed:
            return JSONResponse({"error": "Assignment not found"}, status_code=404)
        return JSONResponse({"api_key_hash": key_hash, "policy_id": policy_id, "status": "removed"})
    except Exception as e:
        logger.error("control_remove_key_policy error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def control_list_key_policy_assignments(request: Request) -> JSONResponse:
    """GET /v1/control/keys/assignments — list all key-policy assignments."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    try:
        assignments = store.list_key_policy_assignments()
        return JSONResponse({"assignments": assignments, "count": len(assignments)})
    except Exception as e:
        logger.error("control_list_key_policy_assignments error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Key-Tool Permission endpoints ─────────────────────────────

async def control_get_key_tools(request: Request) -> JSONResponse:
    """GET /v1/control/keys/{key_hash}/tools — list allowed tools for an API key."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    key_hash = request.path_params["key_hash"]
    try:
        allowed = store.get_allowed_tools(key_hash)
        return JSONResponse({
            "api_key_hash": key_hash,
            "allowed_tools": allowed,
            "unrestricted": allowed is None,
        })
    except Exception as e:
        logger.error("control_get_key_tools error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


async def control_set_key_tools(request: Request) -> JSONResponse:
    """PUT /v1/control/keys/{key_hash}/tools — set (replace) tool allow-list for an API key."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    key_hash = request.path_params["key_hash"]
    try:
        body = await request.json()
        tool_names = body.get("allowed_tools", [])
        if not isinstance(tool_names, list):
            return JSONResponse({"error": "allowed_tools must be a list"}, status_code=400)
        store.set_allowed_tools(key_hash, tool_names)
        return JSONResponse({"api_key_hash": key_hash, "allowed_tools": tool_names, "status": "updated"})
    except Exception as e:
        logger.error("control_set_key_tools error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)


async def control_remove_key_tool(request: Request) -> JSONResponse:
    """DELETE /v1/control/keys/{key_hash}/tools/{tool_name} — remove a tool from a key's allow-list."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)
    key_hash = request.path_params["key_hash"]
    tool_name = request.path_params["tool_name"]
    try:
        removed = store.remove_tool_permission(key_hash, tool_name)
        if not removed:
            return JSONResponse({"error": "Permission not found"}, status_code=404)
        return JSONResponse({"api_key_hash": key_hash, "tool_name": tool_name, "status": "removed"})
    except Exception as e:
        logger.error("control_remove_key_tool error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Policy Template endpoints ─────────────────────────────────

async def control_list_templates(request: Request) -> JSONResponse:
    """GET /v1/control/templates — list available policy templates."""
    templates = []
    for path in sorted(_TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            templates.append({
                "name": path.stem,
                "display_name": data.get("name", path.stem),
                "description": data.get("description", ""),
                "version": data.get("version", "1.0"),
                "policy_count": len(data.get("policies", [])),
            })
        except Exception as exc:
            logger.warning("Failed to read template %s: %s", path.name, exc)
    return JSONResponse({"templates": templates})


async def control_apply_template(request: Request) -> JSONResponse:
    """POST /v1/control/templates/{name}/apply — create all policies from a template."""
    store = _store_or_503()
    if store is None:
        return JSONResponse({"error": "Control plane not available"}, status_code=503)

    template_name = request.path_params["name"]
    path = _TEMPLATES_DIR / f"{template_name}.json"
    if not path.exists():
        return JSONResponse({"error": f"Template '{template_name}' not found"}, status_code=404)

    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        logger.error("control_apply_template: failed to parse %s: %s", path.name, exc)
        return JSONResponse({"error": f"Template file could not be parsed: {exc}"}, status_code=500)

    settings = get_settings()
    tenant_id = request.query_params.get("tenant_id", settings.gateway_tenant_id or "")

    policies = data.get("policies", [])
    created: list[str] = []
    errors: list[dict] = []

    for policy in policies:
        try:
            policy_data = dict(policy)
            if "tenant_id" not in policy_data:
                policy_data["tenant_id"] = tenant_id
            store.create_policy(policy_data)
            created.append(policy_data.get("policy_id") or policy_data.get("policy_name", "?"))
        except Exception as exc:
            errors.append({
                "policy": policy.get("policy_id", policy.get("policy_name", "?")),
                "error": str(exc),
            })

    if created:
        _refresh_policy_cache()

    return JSONResponse({
        "template": template_name,
        "created": len(created),
        "errors": errors,
        "policy_ids": created,
    }, status_code=200)
