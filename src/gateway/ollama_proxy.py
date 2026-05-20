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


def _attested_non_ollama_models(ctx) -> list[dict]:
    """Return active attestations whose provider is NOT ollama.

    OpenWebUI's *Ollama* connection only sees what /api/tags returns, so
    Anthropic and OpenAI attestations must be surfaced through this
    endpoint as if they were Ollama-resident models. Otherwise users who
    register the gateway as a single Ollama connection cannot select
    Claude or GPT models, and selecting one (via a hand-typed model name)
    POSTs /api/chat — historically unhandled → 404 → perpetual spinner
    in OWUI (see ollama_chat_bridge.py for the chat-route fix).
    """
    store = getattr(ctx, "control_store", None)
    if store is None:
        return []
    try:
        return [
            a for a in store.list_attestations()
            if a.get("status") == "active" and a.get("provider") != "ollama"
        ]
    except Exception:
        logger.warning("ollama_proxy: list_attestations failed", exc_info=True)
        return []


def _synth_tag_entry(att: dict) -> dict:
    """Manufacture an Ollama /api/tags entry for a non-ollama attestation.

    Ollama's tag schema requires ``name``, ``model``, ``modified_at``,
    ``size``, ``digest``, and ``details``. We fill plausible placeholder
    values — they're only ever rendered as text in OWUI's picker. The
    crucial field is ``name``/``model`` which OWUI submits back as the
    request's ``model`` field; the gateway's body-based router then
    dispatches to the correct provider adapter.
    """
    mid = att.get("model_id") or ""
    provider = att.get("provider") or "unknown"
    return {
        "name": mid,
        "model": mid,
        "modified_at": "2024-01-01T00:00:00Z",
        "size": 0,
        "digest": f"walacor-attested:{provider}",
        "details": {
            "parent_model": "",
            "format": "api",
            "family": provider,
            "families": [provider],
            "parameter_size": "n/a",
            "quantization_level": "n/a",
        },
    }


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
        # Ollama backend unreachable — but if we have attested
        # Anthropic/OpenAI models we can still serve them as synthetic
        # tags so OWUI's picker remains usable. Without this an Ollama
        # outage takes Claude/GPT down in the OWUI Ollama-connection mode.
        synthetic = [_synth_tag_entry(a) for a in _attested_non_ollama_models(ctx)]
        if synthetic:
            return JSONResponse({"models": synthetic})
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
    # Surface non-ollama attestations (Anthropic / OpenAI) as synthetic
    # tag entries so a single OWUI Ollama connection can drive any
    # attested model. See _synth_tag_entry for schema rationale.
    for att in _attested_non_ollama_models(ctx):
        filtered.append(_synth_tag_entry(att))
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
