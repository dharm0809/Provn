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

# OpenAI returns 130+ models from /v1/models — most aren't chat-completions
# models and probing them with a chat request just produces "not a chat
# model" 404s that waste 1-2 s of probe time each. Prefix/substring match
# against the model_id to skip these before the probe runs.
#
# (We still LIST them in /v1/control/discover output so an admin can see
# what's available; we just don't probe their chat-callability — the
# `callable` field reads `False` with `non_chat_model_skipped` as the
# reason.)
_NON_CHAT_MODEL_TOKENS = (
    "whisper",           # speech-to-text
    "transcribe",        # gpt-4o-transcribe family
    "tts",               # text-to-speech
    "audio",             # gpt-4o-audio-preview etc.
    "embedding",         # text-embedding-3-* / ada-002
    "dall-e",            # image gen
    "sora",              # video gen
    "moderation",        # omni-moderation-*
    "image",             # gpt-image-*
    "realtime",          # gpt-realtime-* (WebRTC, not chat)
    "search-api",        # gpt-5-search-api (search tool, not chat)
    "deep-research",     # o4-mini-deep-research (research API)
    "codex",             # gpt-5-codex (code completions, not chat-shape)
)
_NON_CHAT_EXACT = {
    "davinci-002", "babbage-002",
    "gpt-3.5-turbo-instruct", "gpt-3.5-turbo-instruct-0914",
}


def _is_non_chat_model(model_id: str) -> bool:
    """Return True when this model ID won't accept a chat-completions probe.

    Cheap substring + exact-match check. The list grows by provider release;
    test with a fixture corpus when extending.
    """
    if not isinstance(model_id, str):
        return False
    if model_id in _NON_CHAT_EXACT:
        return True
    lower = model_id.lower()
    return any(tok in lower for tok in _NON_CHAT_MODEL_TOKENS)


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
    """Probe every chat-compatible model and annotate each entry in place.

    Non-chat models (embeddings, whisper, tts, dall-e, …) are short-circuited
    to ``callable=False`` with ``non_chat_model_skipped`` as the reason —
    sending them a chat-completions probe just produces noise + costs ~1-2s
    of round-trip per model. With 130+ OpenAI models listed, this was the
    primary cause of the providers tab's 11.5s page-load on prod.
    """
    sem = asyncio.Semaphore(_PROBE_MAX_CONCURRENCY)

    async def _one(entry: dict) -> None:
        if _is_non_chat_model(entry.get("model_id", "")):
            entry["callable"] = False
            entry["unavailable_reason"] = "non_chat_model_skipped"
            return
        async with sem:
            callable_, reason = await probe_model_callable(
                entry["model_id"], entry["provider"], settings, http_client
            )
            entry["callable"] = callable_
            if reason:
                entry["unavailable_reason"] = reason

    await asyncio.gather(*(_one(m) for m in models), return_exceptions=False)
    probed = sum(1 for m in models if m.get("unavailable_reason") != "non_chat_model_skipped")
    callable_count = sum(1 for m in models if m.get("callable"))
    skipped = len(models) - probed
    logger.info(
        "Model callability probe: %d/%d callable (probed=%d, skipped_non_chat=%d)",
        callable_count, len(models), probed, skipped,
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
