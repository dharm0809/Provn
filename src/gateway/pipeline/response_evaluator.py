"""Phase 10: Post-inference response policy evaluation with pluggable content analyzers."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from starlette.responses import JSONResponse

from gateway.adapters.base import ModelResponse
from gateway.cache.policy_cache import PolicyCache
from gateway.content.base import ContentAnalyzer, Decision, Verdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Content analysis cache — SHA256-keyed, bounded to prevent unbounded growth
# ---------------------------------------------------------------------------
_analysis_cache: dict[str, list] = {}
_CACHE_MAX = 1000


def clear_analysis_cache() -> None:
    """Clear the content analysis cache (e.g. after policy hot-reload)."""
    _analysis_cache.clear()


async def _run_analyzer(analyzer: ContentAnalyzer, text: str) -> Decision | None:
    """Run a single analyzer under its declared timeout. Returns fail-open Decision on timeout/error."""
    try:
        return await asyncio.wait_for(
            analyzer.analyze(text),
            timeout=analyzer.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Content analyzer %s timed out after %dms — returning fail-open PASS",
            analyzer.analyzer_id,
            analyzer.timeout_ms,
        )
        return Decision(
            analyzer_id=analyzer.analyzer_id,
            verdict=Verdict.PASS,
            confidence=0.0,
            category="timeout",
            reason=f"analyzer timed out after {analyzer.timeout_ms}ms",
        )
    except Exception as e:
        logger.warning("Content analyzer %s raised: %s — returning fail-open PASS", analyzer.analyzer_id, e)
        return Decision(
            analyzer_id=analyzer.analyzer_id,
            verdict=Verdict.PASS,
            confidence=0.0,
            category="error",
            reason=f"analyzer error: {e}",
        )


async def analyze_text(text: str, analyzers: list[ContentAnalyzer]) -> list[dict]:
    """Run all analyzers on arbitrary text (tool outputs, injected content, etc.).

    Results are cached by SHA256 hash of *text* so repeated identical content
    skips re-running analyzers.  Cache is bounded at ``_CACHE_MAX`` entries to
    prevent unbounded memory growth.

    Returns a list of decision dicts -- same shape as analyzer_decisions in
    evaluate_post_inference.  Never raises; timeouts and errors are skipped
    silently (same contract as _run_analyzer).
    """
    if not analyzers or not text:
        return []

    cache_key = hashlib.sha256(text.encode()).hexdigest()[:16]

    if cache_key in _analysis_cache:
        return _analysis_cache[cache_key]

    results = await asyncio.gather(*[_run_analyzer(a, text) for a in analyzers])
    decisions = [
        {
            "analyzer_id": d.analyzer_id,
            "verdict": d.verdict.value,
            "confidence": d.confidence,
            "category": d.category,
            "reason": d.reason,
        }
        for d in results if d is not None
    ]

    if len(_analysis_cache) < _CACHE_MAX:
        _analysis_cache[cache_key] = decisions

    return decisions


async def evaluate_post_inference(
    policy_cache: PolicyCache,
    model_response: ModelResponse,
    analyzers: list[ContentAnalyzer],
) -> tuple[bool, int, str, list[dict], JSONResponse | None]:
    """
    Run all content analyzers on model_response.content (or thinking_content as fallback).

    When thinking strip moves all model output to thinking_content (e.g. qwen3:4b),
    content may be empty. We analyse whatever text the model actually produced so that
    safety classifiers (Llama Guard, PII, toxicity) still fire.

    Returns:
        (blocked, response_policy_version, response_policy_result, analyzer_decisions, error_or_none)

    analyzer_decisions: list of {"analyzer_id", "verdict", "confidence", "category", "reason"}
        — labels only, no content.
    response_policy_result: "pass" | "blocked" | "flagged" | "skipped"
    """
    # Use visible content; fall back to thinking_content for thinking-enabled models
    # where strip_thinking_tokens moved everything to thinking_content.
    text_to_analyze = model_response.content or model_response.thinking_content
    if not analyzers or not text_to_analyze:
        return False, policy_cache.version, "skipped", [], None

    # Run all analyzers concurrently, each under its own timeout
    results: list[Decision | None] = await asyncio.gather(
        *[_run_analyzer(a, text_to_analyze) for a in analyzers]
    )

    decisions = [r for r in results if r is not None]
    analyzer_decisions = [
        {
            "analyzer_id": d.analyzer_id,
            "verdict": d.verdict.value,
            "confidence": d.confidence,
            "category": d.category,
            "reason": d.reason,
        }
        for d in decisions
    ]

    # Determine overall result
    blocks = [d for d in decisions if d.verdict == Verdict.BLOCK]
    warns = [d for d in decisions if d.verdict == Verdict.WARN]

    if blocks:
        # First blocking decision drives the error response
        top = blocks[0]
        logger.warning(
            "Response blocked by analyzer %s: category=%s reason=%s confidence=%.2f",
            top.analyzer_id, top.category, top.reason, top.confidence,
        )
        err = JSONResponse(
            {
                "error": "Response blocked by content policy",
                "category": top.category,
                "analyzer_id": top.analyzer_id,
            },
            status_code=403,
        )
        return True, policy_cache.version, "blocked", analyzer_decisions, err

    if warns:
        return False, policy_cache.version, "flagged", analyzer_decisions, None

    return False, policy_cache.version, "pass", analyzer_decisions, None
