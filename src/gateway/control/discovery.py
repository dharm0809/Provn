"""On-demand model discovery from configured providers."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = 5.0

# Models used internally (e.g. content safety analyzers) — excluded from discovery
_INTERNAL_MODEL_PREFIXES = ("llama-guard", "llama_guard")


async def discover_provider_models(
    settings: Any,
    http_client: Any,
) -> list[dict]:
    """Query configured providers for available models.

    Returns a list of ``{model_id, provider, source}`` dicts.
    Each provider query is wrapped in try/except — failures are logged
    and skipped (fail-open).
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

    return models


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
                result.append({"model_id": name, "provider": "ollama", "source": "ollama_tags"})
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
