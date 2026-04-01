"""Unit tests for the post-parse ModelResponse normalizer."""

from __future__ import annotations

import pytest
from gateway.adapters.base import ModelResponse
from gateway.pipeline.normalizer import normalize_model_response


def _make_response(**kwargs) -> ModelResponse:
    defaults = {
        "content": "hello",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "raw_body": b"{}",
    }
    defaults.update(kwargs)
    return ModelResponse(**defaults)


# ---------------------------------------------------------------------------
# Rule 1: Usage field normalization (Anthropic input_tokens → prompt_tokens)
# ---------------------------------------------------------------------------

class TestUsageNormalization:
    def test_anthropic_usage_mapped(self):
        """Anthropic's input_tokens/output_tokens get mapped to prompt_tokens/completion_tokens."""
        resp = _make_response(usage={
            "input_tokens": 100,
            "output_tokens": 50,
        })
        result = normalize_model_response(resp, "anthropic")
        assert result.usage["prompt_tokens"] == 100
        assert result.usage["completion_tokens"] == 50
        assert result.usage["total_tokens"] == 150

    def test_anthropic_preserves_original_keys(self):
        """Original input_tokens/output_tokens are kept alongside canonical names."""
        resp = _make_response(usage={"input_tokens": 100, "output_tokens": 50})
        result = normalize_model_response(resp, "anthropic")
        assert result.usage["input_tokens"] == 100
        assert result.usage["output_tokens"] == 50

    def test_openai_usage_unchanged(self):
        """OpenAI already has prompt_tokens — normalization is a no-op for field names."""
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        resp = _make_response(usage=usage)
        result = normalize_model_response(resp, "openai")
        assert result.usage["prompt_tokens"] == 10
        assert result.usage["completion_tokens"] == 5
        assert result.usage["total_tokens"] == 15

    def test_total_tokens_computed_when_missing(self):
        """total_tokens is computed from prompt + completion when absent."""
        resp = _make_response(usage={"prompt_tokens": 20, "completion_tokens": 10})
        result = normalize_model_response(resp, "ollama")
        assert result.usage["total_tokens"] == 30

    def test_total_tokens_computed_when_zero(self):
        """total_tokens=0 gets recomputed if prompt/completion are present."""
        resp = _make_response(usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 0})
        result = normalize_model_response(resp, "ollama")
        assert result.usage["total_tokens"] == 30

    def test_null_usage_passthrough(self):
        """usage=None passes through unchanged."""
        resp = _make_response(usage=None)
        result = normalize_model_response(resp, "ollama")
        assert result.usage is None


# ---------------------------------------------------------------------------
# Rule 2: Cache enrichment
# ---------------------------------------------------------------------------

class TestCacheEnrichment:
    def test_cache_fields_added_when_missing(self):
        """detect_cache_hit adds cache_hit/cached_tokens/cache_creation_tokens."""
        resp = _make_response(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        result = normalize_model_response(resp, "huggingface")
        assert result.usage["cache_hit"] is False
        assert result.usage["cached_tokens"] == 0
        assert result.usage["cache_creation_tokens"] == 0

    def test_cache_fields_not_overwritten(self):
        """If cache_hit already present, don't re-run detect_cache_hit."""
        resp = _make_response(usage={
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            "cache_hit": True, "cached_tokens": 100, "cache_creation_tokens": 50,
        })
        result = normalize_model_response(resp, "openai")
        assert result.usage["cache_hit"] is True
        assert result.usage["cached_tokens"] == 100

    def test_anthropic_cache_detection(self):
        """Anthropic's cache_read_input_tokens triggers cache_hit=True."""
        resp = _make_response(usage={
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 80, "cache_creation_input_tokens": 20,
        })
        result = normalize_model_response(resp, "anthropic")
        assert result.usage["cache_hit"] is True
        assert result.usage["cached_tokens"] == 80
        assert result.usage["cache_creation_tokens"] == 20


# ---------------------------------------------------------------------------
# Rule 3: Content sentinel check
# ---------------------------------------------------------------------------

class TestSentinelCheck:
    def test_retry_sentinel_cleared(self):
        """__RETRY_WITHOUT_SUMMARY__ sentinel is replaced with empty string."""
        resp = _make_response(content="__RETRY_WITHOUT_SUMMARY__")
        result = normalize_model_response(resp, "openai")
        assert result.content == ""

    def test_normal_content_untouched(self):
        """Regular content passes through."""
        resp = _make_response(content="Hello world")
        result = normalize_model_response(resp, "openai")
        assert result.content == "Hello world"


# ---------------------------------------------------------------------------
# Rule 4: Content type enforcement
# ---------------------------------------------------------------------------

class TestContentEnforcement:
    def test_none_content_becomes_empty_string(self):
        """content=None becomes ""."""
        resp = _make_response(content=None)
        result = normalize_model_response(resp, "ollama")
        assert result.content == ""

    def test_empty_string_stays_empty(self):
        """content="" stays "" (no thinking to fallback to)."""
        resp = _make_response(content="")
        result = normalize_model_response(resp, "ollama")
        assert result.content == ""


# ---------------------------------------------------------------------------
# Rule 5: Thinking fallback
# ---------------------------------------------------------------------------

class TestThinkingFallback:
    def test_empty_content_uses_thinking(self):
        """When content is empty but thinking_content has text, use it as response."""
        resp = _make_response(content="", thinking_content="The user asked about X...")
        result = normalize_model_response(resp, "ollama")
        assert result.content == "The user asked about X..."

    def test_whitespace_content_uses_thinking(self):
        """Whitespace-only content triggers thinking fallback."""
        resp = _make_response(content="   \n  ", thinking_content="Reasoning here")
        result = normalize_model_response(resp, "ollama")
        assert result.content == "Reasoning here"

    def test_content_present_ignores_thinking(self):
        """When content has real text, thinking_content is NOT copied."""
        resp = _make_response(content="Real answer", thinking_content="Internal reasoning")
        result = normalize_model_response(resp, "ollama")
        assert result.content == "Real answer"

    def test_no_thinking_no_fallback(self):
        """When both content and thinking are empty, content stays empty."""
        resp = _make_response(content="", thinking_content=None)
        result = normalize_model_response(resp, "ollama")
        assert result.content == ""


# ---------------------------------------------------------------------------
# Combined / integration
# ---------------------------------------------------------------------------

class TestCombinedNormalization:
    def test_anthropic_full_normalization(self):
        """Anthropic response gets usage mapping + cache enrichment + content enforcement."""
        resp = _make_response(
            content="Analysis complete",
            usage={"input_tokens": 200, "output_tokens": 100, "cache_read_input_tokens": 50},
        )
        result = normalize_model_response(resp, "anthropic")
        assert result.content == "Analysis complete"
        assert result.usage["prompt_tokens"] == 200
        assert result.usage["completion_tokens"] == 100
        assert result.usage["total_tokens"] == 300
        assert result.usage["cache_hit"] is True
        assert result.usage["cached_tokens"] == 50

    def test_no_change_returns_same_object(self):
        """When nothing needs normalizing, returns the same object (not a copy)."""
        resp = _make_response(
            content="hello",
            usage={
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                "cache_hit": False, "cached_tokens": 0, "cache_creation_tokens": 0,
            },
        )
        result = normalize_model_response(resp, "openai")
        assert result is resp

    def test_qwen3_full_think_wrap(self):
        """qwen3 puts entire output in <think> tags — normalizer recovers it."""
        resp = _make_response(
            content="",
            thinking_content="Okay, the user is asking about REST APIs...",
            usage={"prompt_tokens": 16, "completion_tokens": 4, "total_tokens": 20},
        )
        result = normalize_model_response(resp, "ollama")
        assert result.content == "Okay, the user is asking about REST APIs..."
