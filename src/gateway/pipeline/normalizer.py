"""Post-parse normalization layer for ModelResponse.

Ensures every ModelResponse has a consistent field contract before the
orchestrator, hasher, or content analysis touches it — regardless of which
adapter (Ollama, OpenAI, Anthropic, HuggingFace, Generic) produced it.

Called in two places:
  1. Non-streaming: after forward() returns model_response
  2. Streaming: after adapter.parse_streamed_response() in _after_stream_record
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from gateway.adapters.base import ModelResponse
from gateway.adapters.caching import detect_cache_hit

logger = logging.getLogger(__name__)

# Sentinel returned by OpenAI adapter when reasoning summary is unavailable
_RETRY_SENTINEL = "__RETRY_WITHOUT_SUMMARY__"


def normalize_model_response(response: ModelResponse, provider: str) -> ModelResponse:
    """Normalize a ModelResponse to the canonical contract.

    Rules applied in order:
      1. Usage field normalization (Anthropic input_tokens → prompt_tokens)
      2. Cache enrichment (detect_cache_hit if cache keys absent)
      3. Content sentinel check (__RETRY_WITHOUT_SUMMARY__)
      4. Content type enforcement (None → "")
      5. Thinking fallback (empty content + populated thinking → copy as response)

    Returns a new ModelResponse if any field changed, otherwise the original.
    """
    usage = response.usage
    content = response.content
    thinking = response.thinking_content
    changed = False

    # ── 1. Usage field normalization ──────────────────────────────────────
    if usage is not None:
        new_usage = dict(usage)
        modified = False

        # Map Anthropic/Responses API field names to canonical names
        if "input_tokens" in new_usage and "prompt_tokens" not in new_usage:
            new_usage["prompt_tokens"] = new_usage["input_tokens"]
            modified = True
        if "output_tokens" in new_usage and "completion_tokens" not in new_usage:
            new_usage["completion_tokens"] = new_usage["output_tokens"]
            modified = True

        # Compute total_tokens when missing
        if "total_tokens" not in new_usage or not new_usage["total_tokens"]:
            pt = new_usage.get("prompt_tokens", 0) or 0
            ct = new_usage.get("completion_tokens", 0) or 0
            if pt > 0 or ct > 0:
                new_usage["total_tokens"] = pt + ct
                modified = True

        # ── 2. Cache enrichment ───────────────────────────────────────────
        if "cache_hit" not in new_usage:
            cache_info = detect_cache_hit(new_usage)
            new_usage.update(cache_info)
            modified = True

        if modified:
            usage = new_usage
            changed = True

    # ── 3. Content sentinel check ─────────────────────────────────────────
    if content == _RETRY_SENTINEL:
        content = ""
        changed = True
        logger.warning("Cleared __RETRY_WITHOUT_SUMMARY__ sentinel from response content")

    # ── 4. Content type enforcement ───────────────────────────────────────
    if content is None:
        content = ""
        changed = True

    # ── 5. Thinking fallback ──────────────────────────────────────────────
    # When the model wraps its entire output in <think> tags (qwen3) or the
    # adapter produces empty content but has thinking_content, use thinking
    # as the response so the user/dashboard sees actual content.
    if not content.strip() and thinking:
        content = thinking
        changed = True

    if not changed:
        return response

    return dataclasses.replace(response, content=content, usage=usage, thinking_content=thinking)
