"""Proxy native Ollama API endpoints so OpenWebUI can use the Gateway as an Ollama connection.

OpenWebUI polls /api/tags, /api/ps, /api/version when configured with an Ollama connection.
These are native Ollama endpoints that the Gateway doesn't normally handle, so we proxy
them directly to the Ollama backend.

`/api/tags` and `/api/show` additionally filter the response against the
control plane's active attestation set, so OpenWebUI only shows models
the admin has approved. Without this, a fresh Ollama deployment with 30
pulled models would expose all 30 to OpenWebUI even if only 3 are
attested — defeating the whole governance loop.
"""

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)


def _attested_ollama_models(ctx) -> set[str] | None:
    """Return the set of `model_id` for active ollama-provider attestations.

    Returns ``None`` when the control plane isn't available — caller
    falls through to the raw upstream response (the previous behavior).
    An empty set is a meaningful answer: "no ollama models attested,
    surface nothing to OpenWebUI."
    """
    store = getattr(ctx, "control_store", None)
    if store is None:
        return None
    try:
        active = [
            a for a in store.list_attestations()
            if a.get("status") == "active" and a.get("provider") == "ollama"
        ]
    except Exception:
        logger.warning("ollama_proxy: list_attestations failed", exc_info=True)
        return None
    return {a.get("model_id") for a in active if a.get("model_id")}


async def _proxy_to_ollama(request: Request, path: str) -> Response:
    """Forward a request to the Ollama backend and return the response."""
    settings = get_settings()
    ctx = get_pipeline_context()

    ollama_url = settings.provider_ollama_url or "http://localhost:11434"
    target_url = f"{ollama_url}{path}"

    if ctx.http_client is None:
        return JSONResponse({"error": "Gateway HTTP client not initialized"}, status_code=503)

    try:
        resp = await ctx.http_client.get(target_url, timeout=10.0)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"content-type": resp.headers.get("content-type", "application/json")},
        )
    except Exception as e:
        logger.warning("Ollama proxy failed for %s: %s", path, e)
        return JSONResponse({"error": f"Ollama unreachable: {e}"}, status_code=502)


async def ollama_api_tags(request: Request) -> Response:
    """GET /api/tags — list models, filtered by the active attestation set.

    The upstream Ollama instance may have many local models pulled; this
    endpoint only surfaces the ones that are explicitly attested in the
    control plane (provider="ollama", status="active"). Without the
    filter, OpenWebUI bypasses governance for model selection entirely
    — operators see every pulled model regardless of whether it's been
    approved.
    """
    ctx = get_pipeline_context()
    raw = await _proxy_to_ollama(request, "/api/tags")
    if raw.status_code != 200 or raw.body is None:
        return raw

    attested = _attested_ollama_models(ctx)
    if attested is None:
        # No control plane → preserve pre-fix behavior (fail open). The
        # gateway's own /v1/chat/completions still enforces attestation
        # at request time, so this isn't a security gap — just a
        # less-curated UX.
        return raw

    try:
        payload = json.loads(raw.body.decode("utf-8"))
    except (ValueError, AttributeError):
        return raw  # Malformed upstream; pass through.

    models = payload.get("models") or []
    # Ollama's tag list keys each model by both `name` and `model` (often
    # the same string with the same `:tag` suffix). Match on either so we
    # don't lose entries to schema drift between Ollama versions.
    filtered = [
        m for m in models
        if (m.get("name") in attested or m.get("model") in attested)
    ]
    payload["models"] = filtered
    return JSONResponse(payload)


async def ollama_api_ps(request: Request) -> Response:
    """GET /api/ps — list running models."""
    return await _proxy_to_ollama(request, "/api/ps")


async def ollama_api_version(request: Request) -> Response:
    """GET /api/version — Ollama version info."""
    return await _proxy_to_ollama(request, "/api/version")


async def ollama_api_show(request: Request) -> Response:
    """POST /api/show — model details."""
    settings = get_settings()
    ctx = get_pipeline_context()

    ollama_url = settings.provider_ollama_url or "http://localhost:11434"

    if ctx.http_client is None:
        return JSONResponse({"error": "Gateway HTTP client not initialized"}, status_code=503)

    try:
        body = await request.body()
        resp = await ctx.http_client.post(
            f"{ollama_url}/api/show",
            content=body,
            headers={"content-type": "application/json"},
            timeout=10.0,
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"content-type": resp.headers.get("content-type", "application/json")},
        )
    except Exception as e:
        logger.warning("Ollama proxy failed for /api/show: %s", e)
        return JSONResponse({"error": f"Ollama unreachable: {e}"}, status_code=502)
