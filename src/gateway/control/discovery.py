"""On-demand model discovery from configured providers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = 5.0
_PROBE_TIMEOUT = 8.0
_PROBE_MAX_CONCURRENCY = 6
_PROBE_BODY_MAX_CHARS = 200

# Models used internally (e.g. content safety analyzers) — excluded from discovery
_INTERNAL_MODEL_PREFIXES = ("llama-guard", "llama_guard")


async def discover_provider_models(
    settings: Any,
    http_client: Any,
    *,
    live_check: bool = False,
) -> list[dict]:
    """Query configured providers for available models.

    Returns a list of ``{model_id, provider, source}`` dicts.
    Each provider query is wrapped in try/except — failures are logged
    and skipped (fail-open).

    When ``live_check`` is True, each discovered model is probed with a
    1-token chat completion to verify the configured upstream key has
    access. Results are annotated as ``callable: bool`` and, on failure,
    ``unavailable_reason: str``. Probes run concurrently (bounded) so a
    slow provider can't stall the whole discovery.
    """
    models: list[dict] = []

    # Ollama: GET {url}/api/tags
    if settings.provider_ollama_url:
        models.extend(await _discover_ollama(settings.provider_ollama_url, http_client))

    # OpenAI: GET {url}/v1/models with Bearer key
    if settings.provider_openai_key:
        models.extend(
            await _discover_openai(
                settings.provider_openai_url,
                settings.provider_openai_key,
                http_client,
            )
        )

    # Anthropic: GET {url}/v1/models with x-api-key header
    if settings.provider_anthropic_key:
        models.extend(
            await _discover_anthropic(
                settings.provider_anthropic_url,
                settings.provider_anthropic_key,
                http_client,
            )
        )

    if live_check and models:
        await _probe_models(models, settings, http_client)

    return models


async def probe_model_callable(
    model_id: str,
    provider: str,
    settings: Any,
    http_client: Any,
) -> tuple[bool, str | None]:
    """Send a minimal 1-token chat request through the configured upstream.

    Returns ``(callable, unavailable_reason)``. ``unavailable_reason`` is None
    on success and a short ``HTTP {code}: {body_prefix}`` string on any
    non-2xx response or transport error.
    """
    try:
        if provider == "ollama":
            url = settings.provider_ollama_url.rstrip("/") + "/api/chat"
            headers = {"content-type": "application/json"}
            body = {
                "model": model_id,
                "messages": [{"role": "user", "content": "."}],
                "stream": False,
                "options": {"num_predict": 1},
            }
        elif provider == "openai":
            url = settings.provider_openai_url.rstrip("/") + "/v1/chat/completions"
            headers = {
                "content-type": "application/json",
                "authorization": f"Bearer {settings.provider_openai_key}",
            }
            body = {
                "model": model_id,
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
            }
        elif provider == "anthropic":
            url = settings.provider_anthropic_url.rstrip("/") + "/v1/messages"
            headers = {
                "content-type": "application/json",
                "x-api-key": settings.provider_anthropic_key,
                "anthropic-version": "2023-06-01",
            }
            body = {
                "model": model_id,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            }
        else:
            return False, f"unknown provider: {provider}"

        resp = await http_client.post(
            url, json=body, headers=headers, timeout=_PROBE_TIMEOUT
        )
        if 200 <= resp.status_code < 300:
            return True, None
        snippet = (resp.text or "")[:_PROBE_BODY_MAX_CHARS].replace("\n", " ").strip()
        return False, f"HTTP {resp.status_code}: {snippet}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _probe_models(
    models: list[dict],
    settings: Any,
    http_client: Any,
) -> None:
    """Probe every model in `models` and annotate each entry in place."""
    sem = asyncio.Semaphore(_PROBE_MAX_CONCURRENCY)

    async def _one(entry: dict) -> None:
        async with sem:
            callable_, reason = await probe_model_callable(
                entry["model_id"], entry["provider"], settings, http_client
            )
            entry["callable"] = callable_
            if reason:
                entry["unavailable_reason"] = reason

    await asyncio.gather(*(_one(m) for m in models), return_exceptions=False)
    callable_count = sum(1 for m in models if m.get("callable"))
    logger.info(
        "Model callability probe: %d/%d callable", callable_count, len(models)
    )


async def _discover_ollama(base_url: str, http_client: Any) -> list[dict]:
    try:
        url = base_url.rstrip("/") + "/api/tags"
        resp = await http_client.get(url, timeout=_DISCOVERY_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("Ollama discovery returned %d", resp.status_code)
            return []
        data = resp.json()
        result = []
        for m in data.get("models", []):
            name = m.get("name", "")
            if name and not name.lower().startswith(_INTERNAL_MODEL_PREFIXES):
                entry: dict = {"model_id": name, "provider": "ollama", "source": "ollama_tags"}
                digest = m.get("digest", "")
                if digest:
                    entry["digest"] = digest
                result.append(entry)
        logger.info("Ollama discovery: found %d models", len(result))
        return result
    except Exception as e:
        logger.warning("Ollama discovery failed: %s", e)
        return []


async def _discover_openai(base_url: str, api_key: str, http_client: Any) -> list[dict]:
    try:
        url = base_url.rstrip("/") + "/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = await http_client.get(url, headers=headers, timeout=_DISCOVERY_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("OpenAI discovery returned %d", resp.status_code)
            return []
        data = resp.json()
        result = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if mid:
                result.append({"model_id": mid, "provider": "openai", "source": "openai_models"})
        logger.info("OpenAI discovery: found %d models", len(result))
        return result
    except Exception as e:
        logger.warning("OpenAI discovery failed: %s", e)
        return []


async def _discover_anthropic(base_url: str, api_key: str, http_client: Any) -> list[dict]:
    try:
        url = base_url.rstrip("/") + "/v1/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = await http_client.get(url, headers=headers, timeout=_DISCOVERY_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("Anthropic discovery returned %d", resp.status_code)
            return []
        data = resp.json()
        result = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if mid:
                result.append({"model_id": mid, "provider": "anthropic", "source": "anthropic_models"})
        logger.info("Anthropic discovery: found %d models", len(result))
        return result
    except Exception as e:
        logger.warning("Anthropic discovery failed: %s", e)
        return []
