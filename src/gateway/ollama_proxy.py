"""Proxy native Ollama API endpoints so OpenWebUI can use the Gateway as an Ollama connection.

OpenWebUI polls /api/tags, /api/ps, /api/version when configured with an Ollama connection.
These are native Ollama endpoints that the Gateway doesn't normally handle, so we proxy
them directly to the Ollama backend.
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)


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
    """GET /api/tags — list models (native Ollama format)."""
    return await _proxy_to_ollama(request, "/api/tags")


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
