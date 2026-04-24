"""GET /v1/models — OpenAI-compatible model listing.

Falls back to live provider discovery when:
  - No embedded control plane (skip_governance mode), OR
  - Control plane exists but has zero active attestations (fresh deployment)

Results are cached for 60 seconds so OpenWebUI's frequent polling (~1s) does
not hammer Ollama/OpenAI on every request.
"""
import time
import logging
from starlette.requests import Request
from starlette.responses import JSONResponse
from gateway.pipeline.context import get_pipeline_context
from gateway.config import get_settings
from gateway.control.discovery import discover_provider_models

logger = logging.getLogger(__name__)

# ── 60-second in-memory cache ─────────────────────────────────────────────────
_models_cache: list[dict] = []
_models_cache_at: float = 0.0
_MODELS_CACHE_TTL = 60.0


def _invalidate_models_cache() -> None:
    """Force next request to bypass cache. Called by tests and control-plane mutations."""
    global _models_cache, _models_cache_at
    _models_cache = []
    _models_cache_at = 0.0


async def _build_models_list(ctx) -> list[dict]:
    """Build the OpenAI-format model list from attestations or discovery."""
    now = int(time.time())

    settings = get_settings()

    # ── Path 1: attested models from embedded control plane ───────────────────
    if ctx.control_store:
        attestations = ctx.control_store.list_attestations()
        active = [a for a in attestations if a.get("status") == "active"]
        if active:
            return [
                {
                    "id": a["model_id"],
                    "object": "model",
                    "created": now,
                    "owned_by": a.get("provider", "walacor-gateway"),
                }
                for a in active
            ]
        # Strict mode: control store is the source of truth — never fall back to raw discovery.
        if getattr(settings, "strict_model_allowlist", False):
            logger.info(
                "/v1/models: strict allowlist active and no models attested — "
                "returning empty list (admin should attest via Control → Discover Models)"
            )
            return []
        # Fall through: control store exists but no attested models yet (fresh deployment)
        logger.info("/v1/models: control store has no active attestations — falling back to discovery")

    # ── Path 2: live discovery from configured providers ──────────────────────
    if not ctx.http_client:
        logger.debug("/v1/models: no http_client available — returning empty list")
        return []

    try:
        discovered = await discover_provider_models(settings, ctx.http_client)
        logger.info("/v1/models: discovered %d model(s) from providers", len(discovered))
        return [
            {
                "id": m["model_id"],
                "object": "model",
                "created": now,
                "owned_by": m.get("provider", "walacor-gateway"),
            }
            for m in discovered
        ]
    except Exception:
        logger.warning("/v1/models: discovery failed", exc_info=True)
        return []


async def list_models(request: Request) -> JSONResponse:
    global _models_cache, _models_cache_at

    # Serve from cache if fresh
    now = time.monotonic()
    if _models_cache and (now - _models_cache_at) < _MODELS_CACHE_TTL:
        return JSONResponse({"object": "list", "data": _models_cache})

    ctx = get_pipeline_context()
    models = await _build_models_list(ctx)

    _models_cache = models
    _models_cache_at = now

    return JSONResponse({"object": "list", "data": models})
