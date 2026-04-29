"""Phase 10: Post-inference response policy evaluation with pluggable content analyzers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from cachetools import TTLCache
from starlette.responses import JSONResponse

from gateway.adapters.base import ModelResponse
from gateway.cache.policy_cache import PolicyCache
from gateway.content.base import ContentAnalyzer, Decision, Verdict

logger = logging.getLogger(__name__)


@dataclass
class ContentBlockDetail:
    """Structured explanation for a content analysis block decision."""

    analyzer_id: str
    category: str
    confidence: float
    reason: str

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": "Blocked by content analysis",
            "reason": (
                f"Content analyzer '{self.analyzer_id}' flagged category "
                f"'{self.category}' (confidence: {self.confidence:.2f})"
            ),
            "governance_decision": {
                "analyzer_id": self.analyzer_id,
                "category": self.category,
                "confidence": self.confidence,
                "reason": self.reason,
            },
        }

# ---------------------------------------------------------------------------
# Content analysis cache — SHA256-keyed TTL to prevent unbounded growth
# and ensure stale verdicts are re-evaluated after policy hot-reloads.
# ---------------------------------------------------------------------------
_ANALYSIS_CACHE_TTL = 60  # seconds
_analysis_cache: TTLCache = TTLCache(maxsize=5000, ttl=_ANALYSIS_CACHE_TTL)


def clear_analysis_cache() -> None:
    """Clear the content analysis cache (e.g. after policy hot-reload)."""
    _analysis_cache.clear()


async def _run_analyzer(analyzer: ContentAnalyzer, text: str) -> Decision | None:
    """Run a single analyzer under its declared timeout. Returns fail-open Decision on timeout/error."""
    try:
        # Sync analyzers (PII, toxicity, DLP, SafetyClassifier) call into
        # blocking C extensions (ORT InferenceSession.run, regex engines).
        # Calling them inline would hold the event loop for the entirety
        # of the analyzer's runtime — including the per-analyzer ONNX
        # timeout budget. Offload to a worker thread so the loop stays
        # responsive and the existing `run_with_timeout` budget (sync,
        # thread-pool-backed) only blocks that worker thread.
        timeout_s = analyzer.timeout_ms / 1000.0
        result = await asyncio.wait_for(
            asyncio.to_thread(analyzer.analyze, text),
            timeout=timeout_s,
        )
        # The to_thread call already awaited a sync return. If the
        # analyzer happens to be coroutine-returning (e.g. LlamaGuard),
        # `asyncio.to_thread` would have returned the coroutine object
        # without awaiting it — handle that case.
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            return await asyncio.wait_for(result, timeout=timeout_s)
        return result
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


async def analyze_text(
    text: str,
    analyzers: list[ContentAnalyzer],
    tenant_id: str = "",
) -> list[dict]:
    """Run all analyzers on arbitrary text (tool outputs, injected content, etc.).

    Results are cached by (tenant_id, SHA256(text)) so repeated identical content
    within the same tenant skips re-running analyzers, while tenants remain
    isolated. Cache is bounded at 5000 entries with a 60s TTL to prevent
    unbounded memory growth and ensure stale verdicts expire.

    Returns a list of decision dicts -- same shape as analyzer_decisions in
    evaluate_post_inference.  Never raises; timeouts and errors are skipped
    silently (same contract as _run_analyzer).
    """
    if not analyzers or not text:
        return []

    text_digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    cache_key = f"{tenant_id}:{text_digest}"

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

    _analysis_cache[cache_key] = decisions

    return decisions


async def evaluate_post_inference(
    policy_cache: PolicyCache,
    model_response: ModelResponse,
    analyzers: list[ContentAnalyzer],
    tenant_id: str = "",
) -> tuple[bool, int, str, list[dict], JSONResponse | None]:
    """
    Run all content analyzers on model_response.content (or thinking_content as fallback).

    When thinking strip moves all model output to thinking_content (e.g. qwen3:1.7b),
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
        detail = ContentBlockDetail(
            analyzer_id=top.analyzer_id,
            category=top.category,
            confidence=top.confidence,
            reason=top.reason,
        )
        err = JSONResponse(detail.to_response_body(), status_code=403)
        return True, policy_cache.version, "blocked", analyzer_decisions, err

    if warns:
        return False, policy_cache.version, "flagged", analyzer_decisions, None

    return False, policy_cache.version, "pass", analyzer_decisions, None
