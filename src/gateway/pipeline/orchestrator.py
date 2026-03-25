"""Pipeline orchestrator: 8-step flow (G1→G3→budget→forward→G4→hash→G5→WAL).

Phase 14 additions (steps 2.7 and 3.5):
  2.7  Inject MCP tool definitions into local-model requests (active strategy).
  3.5  Tool Strategy Router:
         passive — extract tool interactions already reported in the provider response.
         active  — run the gateway-side tool-call loop via MCP.
       Both strategies produce a unified tool_interactions audit metadata entry.
"""

from __future__ import annotations

import asyncio
import dataclasses
import fnmatch
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, cast

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.background import BackgroundTask

from gateway.config import get_settings
import gateway.util.json_utils as json
from gateway.util.request_context import disposition_var, execution_id_var, provider_var, model_id_var, request_id_var
from gateway.adapters import OpenAIAdapter, OllamaAdapter
from gateway.adapters.anthropic import AnthropicAdapter
from gateway.adapters.generic import GenericAdapter
from gateway.adapters.huggingface import HuggingFaceAdapter
from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter, ToolInteraction
from gateway.pipeline.context import get_pipeline_context
from gateway.pipeline.forwarder import forward, stream_with_tee
from gateway.pipeline.model_resolver import resolve_attestation
from gateway.pipeline.policy_evaluator import evaluate_pre_inference
from gateway.pipeline.response_evaluator import analyze_text, evaluate_post_inference
from gateway.pipeline.hasher import build_execution_record
from gateway.pipeline.session_chain import compute_record_hash
from gateway.metrics.prometheus import (
    requests_total, pipeline_duration, response_policy_total,
    token_usage_total, budget_exceeded_total,
    tool_calls_total, tool_loop_iterations,
    rate_limit_hits_total, content_blocks_total,
    inflight_requests, response_status_total, forward_duration_by_model,
    cache_hits, cache_misses,
)
from gateway.metrics.anomaly import latency_detector
from cachetools import LRUCache

logger = logging.getLogger(__name__)

from gateway.routing.concurrency import ConcurrencyLimiter

_AUDIT_ONLY_ATTESTATION_ID = "audit_only_no_attestation"

# ── Adaptive Concurrency Limiters (Gradient2) ─────────────────────────────────
# Per-provider concurrency limiters, created on first use.  Thread-safe for
# asyncio (single writer, dict mutation is atomic in CPython).
_concurrency_limiters: LRUCache = LRUCache(maxsize=100)


def _get_or_create_limiter(provider: str) -> ConcurrencyLimiter:
    """Return the per-provider ConcurrencyLimiter, creating one if needed."""
    lim = _concurrency_limiters.get(provider)
    if lim is None:
        settings = get_settings()
        lim = ConcurrencyLimiter(
            min_limit=settings.adaptive_concurrency_min,
            max_limit=settings.adaptive_concurrency_max,
        )
        _concurrency_limiters[provider] = lim
        logger.info(
            "Created adaptive concurrency limiter for provider=%s min=%d max=%d",
            provider, settings.adaptive_concurrency_min, settings.adaptive_concurrency_max,
        )
    return lim

# ── Model Capability Registry ─────────────────────────────────────────────────
# Caches per-model capabilities discovered at runtime so the gateway never
# wastes a retry on a model that has already been probed.  Thread-safe for
# asyncio (single writer, dict mutation is atomic in CPython).

_model_capabilities: LRUCache = LRUCache(maxsize=500)
# e.g.  {"gemma3:1b": {"supports_tools": False}, "qwen3:1.7b": {"supports_tools": True}}

# PIISanitizer singleton — avoid recompiling regex patterns on every request.
_pii_sanitizer_instance = None
_pii_sanitizer_types: set[str] | None = None


def _get_pii_sanitizer(settings):
    """Return a cached PIISanitizer, recreating only if sanitize_types change."""
    global _pii_sanitizer_instance, _pii_sanitizer_types
    types = {t.strip() for t in settings.pii_sanitization_types.split(",") if t.strip()}
    if _pii_sanitizer_instance is None or _pii_sanitizer_types != types:
        from gateway.content.pii_sanitizer import PIISanitizer
        _pii_sanitizer_instance = PIISanitizer(sanitize_types=types)
        _pii_sanitizer_types = types
    return _pii_sanitizer_instance


def _model_supports_tools(model_id: str) -> bool | None:
    """Return True/False if known, None if not yet probed."""
    return _model_capabilities.get(model_id, {}).get("supports_tools")


def _record_model_capability(model_id: str, supports_tools: bool) -> None:
    """Cache a discovered capability for a model."""
    caps = _model_capabilities.setdefault(model_id, {})
    caps["supports_tools"] = supports_tools
    logger.info("Model capability cached: %s supports_tools=%s", model_id, supports_tools)


# ── A/B test cache (B.9) ─────────────────────────────────────────────────────
# Parsed once at first use from settings.ab_tests_json and reused for all
# subsequent requests.  Thread-safe for asyncio (single writer, atomic assign).
_AB_TESTS_CACHE: list | None = None


def _get_ab_tests() -> list:
    """Return parsed A/B test list, loading from config on first call."""
    global _AB_TESTS_CACHE
    if _AB_TESTS_CACHE is None:
        from gateway.routing.ab_test import load_ab_tests
        _AB_TESTS_CACHE = load_ab_tests(get_settings().ab_tests_json)
    return _AB_TESTS_CACHE


# ── Resilience layer (Phase 25) ──────────────────────────────────────────────

async def _forward_with_resilience(adapter, call, request):
    """Forward with retry, circuit breaker, and fallback.

    Returns (http_response, model_response, used_fallback: bool).
    Falls back to plain forward() when no load balancer is configured.
    """
    ctx = get_pipeline_context()
    cb_reg = ctx.circuit_breakers
    lb = ctx.load_balancer

    # No resilience layer configured — plain forward with timeout handling
    if not lb:
        try:
            resp, mr = await forward(adapter, call, request)
        except Exception as fwd_exc:
            # Catch httpx.ReadTimeout and other transport errors — return 504 not 500
            exc_name = type(fwd_exc).__name__
            if "Timeout" in exc_name or "ReadTimeout" in exc_name:
                logger.error("Provider timeout for %s: %s (increase WALACOR_PROVIDER_TIMEOUT)", call.model_id, fwd_exc)
                return Response(
                    content=json.dumps({"error": f"Provider timeout — model '{call.model_id}' did not respond in time. It may be loading into memory (retry in 30s)."}),
                    status_code=504,
                    headers={"Content-Type": "application/json", "Retry-After": "30"},
                ), ModelResponse(content="", usage=None, raw_body=b"", provider_request_id="", model_hash=""), False
            logger.error("Provider forward error for %s: %s", call.model_id, fwd_exc, exc_info=True)
            return Response(
                content=json.dumps({"error": "Provider unavailable"}),
                status_code=502,
                headers={"Content-Type": "application/json"},
            ), ModelResponse(content="", usage=None, raw_body=b"", provider_request_id="", model_hash=""), False
        if cb_reg and resp.status_code < 400:
            cb_reg.record_success(call.model_id)
        elif cb_reg and resp.status_code >= 500:
            cb_reg.record_failure(call.model_id)
        return resp, mr, False

    # Check circuit breaker
    if cb_reg and cb_reg.is_open(call.model_id):
        logger.warning("Circuit open for %s — trying fallback", call.model_id)
        from gateway.routing.fallback import select_fallback
        fb = select_fallback("server_error", call.model_id, lb)
        if fb is None:
            return JSONResponse(
                {"error": {"message": "Service unavailable (circuit open, no fallback)", "type": "server_error"}},
                status_code=503,
            ), ModelResponse(content="", usage=None, raw_body=b"", provider_request_id="", model_hash=""), True
        # TODO: route to fallback endpoint (requires adapter URL override)
        logger.info("Circuit open fallback available: %s", fb.url)

    # Try primary forward with retry
    from gateway.routing.retry import forward_with_retry, is_retryable

    try:
        async def _do_forward():
            resp, mr = await forward(adapter, call, request)
            if resp.status_code >= 500:
                raise _ProviderHTTPError(resp.status_code, bytes(resp.body).decode("utf-8", errors="replace"))
            return resp, mr

        result = await forward_with_retry(_do_forward, max_attempts=get_settings().retry_max_attempts)
        if cb_reg:
            cb_reg.record_success(call.model_id)
        return result[0], result[1], False
    except _ProviderHTTPError as e:
        if cb_reg:
            cb_reg.record_failure(call.model_id)
        # Try fallback
        from gateway.routing.fallback import classify_error, select_fallback
        error_class = classify_error(e.status_code, e.body)
        fb = select_fallback(error_class, call.model_id, lb)
        if fb is not None:
            logger.info("Falling back to %s after %s error", fb.url, error_class)
            # For now, retry with same adapter (future: route to fallback URL)
            try:
                resp, mr = await forward(adapter, call, request)
                return resp, mr, True
            except Exception:
                pass
        # Return error response
        return JSONResponse(
            {"error": {"message": f"Provider error: {e.body}", "type": "server_error"}},
            status_code=e.status_code,
        ), ModelResponse(content="", usage=None, raw_body=b"", provider_request_id="", model_hash=""), False
    except Exception:
        if cb_reg:
            cb_reg.record_failure(call.model_id)
        raise


class _ProviderHTTPError(Exception):
    """Raised when provider returns 5xx to trigger retry logic."""
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body}")


# ── Basic helpers ─────────────────────────────────────────────────────────────

def _set_disposition(request: Request, value: str) -> None:
    """Set disposition on both ContextVar and request.state (crosses BaseHTTPMiddleware boundary)."""
    disposition_var.set(value)
    request.state.walacor_disposition = value


def _inc_request(provider: str, model: str, outcome: str) -> None:
    try:
        requests_total.labels(provider=provider, model=model, outcome=outcome).inc()
    except Exception:
        logger.debug("Metric increment failed (requests_total)", exc_info=True)


def _add_governance_headers(
    response, execution_id=None, attestation_id=None, chain_seq=None,
    policy_result=None, content_analysis=None, budget_remaining=None,
    budget_percent=None, model_id=None,
):
    """Add X-Walacor-* governance metadata headers to response."""
    if execution_id:
        response.headers["x-walacor-execution-id"] = str(execution_id)
    if attestation_id:
        response.headers["x-walacor-attestation-id"] = str(attestation_id)
    if chain_seq is not None:
        response.headers["x-walacor-chain-seq"] = str(chain_seq)
    if policy_result:
        response.headers["x-walacor-policy-result"] = str(policy_result)
    if content_analysis:
        response.headers["x-walacor-content-analysis"] = str(content_analysis)
    if budget_remaining is not None:
        response.headers["x-walacor-budget-remaining"] = str(budget_remaining)
    if budget_percent is not None:
        response.headers["x-walacor-budget-percent"] = str(budget_percent)
    if model_id:
        response.headers["x-walacor-model-id"] = str(model_id)


def _summarize_content_analysis(decisions: list) -> str:
    """Summarize content analysis decisions into a single header value."""
    if not decisions:
        return "clean"
    for d in decisions:
        if d.get("action") == "block":
            return "blocked"
    verdicts = [d.get("verdict", "") for d in decisions]
    if any("pii" in v for v in verdicts):
        return "pii_warn"
    if any("toxic" in v or "warn" in v for v in verdicts):
        return "toxicity_warn"
    return "clean"


# ── Request Type Classification ───────────────────────────────────────────────
# Detects OpenWebUI background tasks (title generation, follow-up suggestions,
# autocomplete, tag generation) so the lineage dashboard can separate real user
# messages from system-generated noise.  Detection is prompt-content-based to
# work with unmodified OpenWebUI installs (no filter plugin required).

_SYSTEM_TASK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("title_generation", re.compile(
        r"generate a (?:concise|brief|short).*?title", re.IGNORECASE)),
    ("autocomplete", re.compile(
        r"### Task:.*?autocompletion system", re.IGNORECASE | re.DOTALL)),
    ("follow_up", re.compile(
        r"generate (?:\d+ )?(?:follow[- ]?up|suggested|relevant).*?question", re.IGNORECASE)),
    ("tag_generation", re.compile(
        r"generate (?:\d+ )?(?:concise )?tags?\b", re.IGNORECASE)),
    ("emoji_generation", re.compile(
        r"generate (?:a single |an? )?emoji", re.IGNORECASE)),
    ("search_query", re.compile(
        r"generate (?:a )?search query", re.IGNORECASE)),
]


def _classify_request_type(prompt: str) -> str:
    """Classify a request as user_message or a specific system_task subtype."""
    text = prompt[:1000]  # only inspect first 1000 chars for efficiency
    for task_type, pattern in _SYSTEM_TASK_PATTERNS:
        if pattern.search(text):
            return f"system_task:{task_type}"
    if text.lstrip().startswith("### Task:"):
        return "system_task"
    return "user_message"


def _compute_budget_percent(budget_remaining, settings) -> int | None:
    """Compute budget usage percent. Returns None if budget not configured."""
    if budget_remaining is None:
        return None
    if budget_remaining < 0:  # unlimited sentinel
        return None
    max_tokens = settings.token_budget_max_tokens
    if max_tokens <= 0:
        return None
    used = max_tokens - budget_remaining
    return min(100, max(0, round(used / max_tokens * 100)))


def _inject_caller_role(att_ctx: dict, request) -> None:
    """Inject caller_role into attestation context for policy evaluation."""
    caller_identity = getattr(request.state, "caller_identity", None)
    if caller_identity is not None and caller_identity.roles:
        # Only use verified identity (JWT) for policy decisions — unverified headers are audit-only
        if caller_identity.source == "jwt":
            att_ctx["caller_role"] = caller_identity.roles[0]


async def _peek_model_id(request: Request) -> str:
    """Extract model field from request body without consuming it.

    Caches the parsed dict on request.state._parsed_body so downstream code
    (e.g. request classifier) can reuse it without re-parsing.
    """
    try:
        body = await request.body()
        parsed = json.loads(body)
        request.state._parsed_body = parsed
        return str(parsed.get("model") or "")
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return ""
    except Exception:
        logger.warning("_peek_model_id unexpected error — model routing may use path fallback", exc_info=True)
        return ""


def _make_adapter_for_route(route: dict) -> ProviderAdapter | None:
    """Build a provider adapter from a model routing table entry."""
    settings = get_settings()
    provider = route.get("provider", "")
    url = route.get("url", "")
    key = route.get("key", "")
    if provider == "openai":
        return OpenAIAdapter(
            base_url=url or settings.provider_openai_url,
            api_key=key or settings.provider_openai_key,
        )
    if provider == "ollama":
        return OllamaAdapter(
            base_url=url or settings.provider_ollama_url,
            api_key=key or settings.provider_ollama_key,
            digest_cache_ttl=settings.ollama_digest_cache_ttl,
            thinking_strip_enabled=settings.thinking_strip_enabled,
        )
    if provider == "anthropic":
        return AnthropicAdapter(
            base_url=url or settings.provider_anthropic_url,
            api_key=key or settings.provider_anthropic_key,
            prompt_caching=settings.prompt_caching_enabled,
        )
    if provider == "huggingface":
        return HuggingFaceAdapter(
            base_url=url or settings.provider_huggingface_url,
            api_key=key or settings.provider_huggingface_key,
        )
    return None


def _resolve_adapter(path: str, model_id: str = "") -> ProviderAdapter | None:
    settings = get_settings()
    # Model routing table takes priority over path-based routing
    if model_id and settings.model_routes:
        for route in settings.model_routes:
            if fnmatch.fnmatch(model_id.lower(), route.get("pattern", "").lower()):
                adapter = _make_adapter_for_route(route)
                if adapter:
                    return adapter
    # Path-based fallback (existing logic unchanged)
    if path.startswith("/v1/chat/completions") or path.startswith("/v1/completions"):
        if settings.provider_ollama_url and settings.gateway_provider == "ollama":
            return OllamaAdapter(
                base_url=settings.provider_ollama_url,
                api_key=settings.provider_ollama_key,
                digest_cache_ttl=settings.ollama_digest_cache_ttl,
                thinking_strip_enabled=settings.thinking_strip_enabled,
            )
        return OpenAIAdapter(base_url=settings.provider_openai_url, api_key=settings.provider_openai_key)
    if path.startswith("/v1/messages"):
        return AnthropicAdapter(base_url=settings.provider_anthropic_url, api_key=settings.provider_anthropic_key, prompt_caching=settings.prompt_caching_enabled)
    if settings.provider_huggingface_url and (path.startswith("/generate") or path.startswith("/v1/models")):
        return HuggingFaceAdapter(base_url=settings.provider_huggingface_url, api_key=settings.provider_huggingface_key)
    if settings.generic_upstream_url and path.startswith("/v1/custom"):
        return GenericAdapter(
            base_url=settings.generic_upstream_url, api_key="",
            model_path=settings.generic_model_path,
            prompt_path=settings.generic_prompt_path,
            response_path=settings.generic_response_path,
            auto_detect=settings.generic_auto_detect,
        )
    return None


async def _record_token_usage(
    model_response: ModelResponse,
    tenant_id: str,
    provider: str,
    user: str | None,
    estimated: int = 0,
) -> int:
    """Push token counts to metrics and budget tracker. Returns total tokens.

    `estimated` is the number of tokens pre-reserved in check_and_reserve so
    the budget tracker can apply only the delta (Finding 4).
    """
    ctx = get_pipeline_context()
    usage = model_response.usage or {}
    total = usage.get("total_tokens", 0) or 0
    prompt_t = usage.get("prompt_tokens", 0) or 0
    completion_t = usage.get("completion_tokens", 0) or 0
    if total > 0:
        try:
            token_usage_total.labels(tenant_id=tenant_id, provider=provider, token_type="prompt").inc(prompt_t)
            token_usage_total.labels(tenant_id=tenant_id, provider=provider, token_type="completion").inc(completion_t)
            token_usage_total.labels(tenant_id=tenant_id, provider=provider, token_type="total").inc(total)
        except Exception:
            logger.debug("Metric increment failed (token_usage_total)", exc_info=True)
        if ctx.budget_tracker:
            await ctx.budget_tracker.record_usage(tenant_id, user, total, estimated)
    elif estimated > 0 and ctx.budget_tracker:
        # No usage data returned (e.g. streaming with no usage field) but tokens were
        # pre-reserved in check_and_reserve — refund the full reservation.
        await ctx.budget_tracker.record_usage(tenant_id, user, 0, estimated)
    return total


# ── Private dataclasses ───────────────────────────────────────────────────────

@dataclasses.dataclass
class _AuditParams:
    """Groups audit fields to keep _build_and_write_record within parameter limits."""

    attestation_id: str
    policy_version: int
    policy_result: str
    budget_remaining: int | None
    audit_metadata: dict
    tool_interactions: list[ToolInteraction]
    tool_strategy: str
    tool_iterations: int
    rp_version: int
    rp_result: str
    rp_decisions: list
    provider: str = ""
    latency_ms: float | None = None
    timings: dict | None = None
    variant_id: str | None = None


@dataclasses.dataclass
class _PreCheckResult:
    """Outcome of the governance pre-checks (Steps 1–2.7)."""

    error: Response | None = None
    att_id: str = ""
    pv: int = 0
    pr: str = ""
    budget_remaining: int | None = None
    budget_estimated: int = 0  # tokens pre-reserved; threaded to record_usage for delta fix
    call: ModelCall | None = None  # always set when error is None
    audit_metadata: dict = dataclasses.field(default_factory=dict)
    tool_strategy: str = "disabled"
    whb: bool = False
    reason: str | None = None
    timings: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _ToolStrategyResult:
    """Outcome of the step-3.5 tool strategy router."""

    call: ModelCall
    model_response: ModelResponse
    interactions: list[ToolInteraction]
    iterations: int
    error: Response | None
    http_response: Response | None = None  # final HTTP response from tool loop (replaces original)


# ── Phase 14: tool strategy helpers ──────────────────────────────────────────

def _select_tool_strategy(adapter: ProviderAdapter, settings) -> str:
    """Return 'passive', 'active', or 'disabled' based on config and provider."""
    if not settings.tool_aware_enabled:
        return "disabled"
    if settings.tool_strategy != "auto":
        return settings.tool_strategy
    return "passive" if adapter.get_provider_name() in ("openai", "anthropic") else "active"


def _strip_tools_from_call(call: ModelCall) -> ModelCall:
    """Remove tool definitions from request body — used when model rejects tools."""
    try:
        body = json.loads(call.raw_body)
        body.pop("tools", None)
        body.pop("tool_choice", None)
        new_body = json.dumps_bytes(body)
        return dataclasses.replace(call, raw_body=new_body)
    except Exception:
        logger.warning("_strip_tools_from_call: failed to strip tools from model=%s — sending original body", call.model_id, exc_info=True)
        return call


_TOOL_UNSUPPORTED_PHRASES = (
    "does not support tools",
    "tool use is not supported",
    "tools are not supported",
    "tool_use is not supported",
    "does not support function",
    "function calling is not supported",
    "does not support tool_use",
)


def _is_tool_unsupported_error(status_code: int, body: bytes | memoryview | None) -> bool:
    """Check if a provider error indicates the model doesn't support tools."""
    if status_code not in (400, 422) or body is None:
        return False
    try:
        text = bytes(body).decode("utf-8", errors="replace").lower()
        return any(phrase in text for phrase in _TOOL_UNSUPPORTED_PHRASES)
    except Exception:
        return False


def _filter_tools_for_key(
    tool_definitions: list[dict],
    api_key: str | None,
    ctx,
) -> list[dict]:
    """Filter tool definitions based on per-key allow-list stored in the control plane.

    Returns:
      - All tools unchanged if api_key is None, control_store is None, or key has no
        restrictions (get_allowed_tools returns None → unrestricted).
      - Empty list if the key has an explicit empty allow-list (all tools blocked).
      - Filtered list containing only the tools whose name appears in the allow-list.
    """
    if not api_key or ctx.control_store is None:
        return tool_definitions
    import hashlib
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    allowed = ctx.control_store.get_allowed_tools(key_hash)
    if allowed is None:
        return tool_definitions  # no restrictions for this key
    if not allowed:
        return []  # explicitly blocked all tools
    return [
        t for t in tool_definitions
        if (t.get("function", {}).get("name") in allowed or t.get("name") in allowed)
    ]


def _inject_tools_into_call(call: ModelCall, tool_definitions: list[dict]) -> ModelCall:
    """Transparently add MCP tool definitions to request body (active strategy)."""
    if not tool_definitions:
        return call
    try:
        body = json.loads(call.raw_body)
        if not body.get("tools"):
            body["tools"] = tool_definitions
            new_body = json.dumps_bytes(body)
            return ModelCall(
                provider=call.provider, model_id=call.model_id,
                prompt_text=call.prompt_text, raw_body=new_body,
                is_streaming=call.is_streaming, metadata=call.metadata,
            )
    except Exception:
        logger.warning(
            "Failed to inject tool definitions into call: model=%s", call.model_id,
            exc_info=True,
        )
    return call


def _serialize_tool_interaction(t: ToolInteraction, source: str) -> dict[str, Any]:
    """Serialize one ToolInteraction to audit metadata dict (hashes input/output)."""
    from gateway.core import compute_sha3_512_string
    d: dict[str, Any] = {"tool_id": t.tool_id, "tool_type": t.tool_type, "tool_name": t.tool_name, "source": source}
    if t.input_data is not None:
        d["input_hash"] = compute_sha3_512_string(json.dumps(t.input_data, default=str, sort_keys=True))
    if t.output_data is not None:
        d["output_hash"] = compute_sha3_512_string(json.dumps(t.output_data, default=str, sort_keys=True))
    if t.sources:
        d["sources"] = t.sources
    if t.metadata:
        d.update(t.metadata)
    return d


def _build_tool_audit_metadata(
    interactions: list[ToolInteraction], strategy: str, iterations: int
) -> dict[str, Any]:
    """Build tool_* keys to merge into audit_metadata."""
    if not interactions:
        return {}
    source = "provider" if strategy == "passive" else "gateway"
    result: dict[str, Any] = {
        "tool_strategy": strategy,
        "tool_interaction_count": len(interactions),
        "tool_interactions": [_serialize_tool_interaction(t, source) for t in interactions],
    }
    if iterations > 0:
        result["tool_loop_iterations"] = iterations
    return result


def _build_tool_event_record(
    t: ToolInteraction,
    execution_id: str,
    session_id: str | None,
    prompt_id: str,
    source: str,
    tenant_id: str,
    gateway_id: str,
) -> dict[str, Any]:
    """Build a first-class tool event record for Walacor/WAL (ETId 9000003)."""
    from gateway.core import compute_sha3_512_string
    record: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "execution_id": execution_id,
        "session_id": session_id,
        "prompt_id": prompt_id,
        "tenant_id": tenant_id,
        "gateway_id": gateway_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "tool_call",
        "tool_id": t.tool_id,
        "tool_type": t.tool_type,
        "tool_name": t.tool_name,
        "source": source,
    }
    if t.input_data is not None:
        record["input_data"] = t.input_data
        record["input_hash"] = compute_sha3_512_string(json.dumps(t.input_data, default=str, sort_keys=True))
    if t.output_data is not None:
        record["output_hash"] = compute_sha3_512_string(json.dumps(t.output_data, default=str, sort_keys=True))
    if t.sources:
        record["sources"] = t.sources
    if t.metadata:
        record["iteration"] = t.metadata.get("iteration")
        record["duration_ms"] = t.metadata.get("duration_ms")
        record["is_error"] = t.metadata.get("is_error")
    return record


async def _write_tool_events(
    interactions: list[ToolInteraction],
    execution_id: str,
    call: ModelCall,
    strategy: str,
    ctx: Any,
    settings: Any,
) -> None:
    """Write each tool interaction as a first-class audit event record (dual-write: Walacor + WAL)."""
    if not interactions:
        return
    session_id = call.metadata.get("session_id")
    prompt_id = call.metadata.get("prompt_id", "")
    source = "provider" if strategy == "passive" else "gateway"
    for t in interactions:
        record = _build_tool_event_record(
            t, execution_id, session_id, prompt_id, source,
            settings.gateway_tenant_id, settings.gateway_id,
        )
        # Analyze tool output for indirect prompt injection
        if ctx.content_analyzers and t.output_data is not None:
            output_text = (t.output_data if isinstance(t.output_data, str)
                           else json.dumps(t.output_data, default=str))
            analysis = await analyze_text(output_text, ctx.content_analyzers)
            if analysis:
                record["content_analysis"] = analysis
        if ctx.storage:
            await ctx.storage.write_tool_event(record)


def _emit_tool_metrics(interactions: list[ToolInteraction], provider: str, source: str) -> None:
    for t in interactions:
        try:
            tool_calls_total.labels(provider=provider, tool_type=t.tool_type, source=source).inc()
        except Exception:
            logger.debug("Metric increment failed (tool_calls_total)", exc_info=True)


async def _execute_one_tool(
    tc: ToolInteraction, ctx, settings, provider: str, iteration: int
) -> tuple[ToolInteraction, dict]:
    """Execute one MCP tool call. Returns (enriched_interaction, result_dict)."""
    # Validate arguments against tool schema before calling MCP
    if tc.tool_name and ctx.tool_registry:
        schema = ctx.tool_registry.get_tool_schema(tc.tool_name)
        if schema:
            required = schema.get("required", [])
            args = tc.input_data if isinstance(tc.input_data, dict) else {}
            missing = [f for f in required if f not in args]
            if missing:
                logger.warning("Tool arg validation failed: tool=%s missing=%s", tc.tool_name, missing)
                enriched = ToolInteraction(
                    tool_id=tc.tool_id, tool_type=tc.tool_type, tool_name=tc.tool_name,
                    input_data=tc.input_data, output_data=None, sources=None,
                    metadata={"iteration": iteration, "duration_ms": 0.0, "is_error": True,
                              "validation_error": f"missing required args: {missing}"},
                )
                return enriched, {"tool_call_id": tc.tool_id,
                                  "content": f"Tool call rejected: missing required arguments {missing}"}

    t_start = time.perf_counter()
    result = await ctx.tool_registry.execute_tool(
        tc.tool_name or "",
        tc.input_data if isinstance(tc.input_data, dict) else {},
        timeout_ms=settings.tool_execution_timeout_ms,
    )
    duration_ms = round((time.perf_counter() - t_start) * 1000.0, 2)

    # Truncate oversized tool output to prevent memory/token exhaustion
    if result.content and len(result.content) > settings.tool_max_output_bytes:
        logger.warning(
            "Tool %s output truncated: %d > %d bytes",
            tc.tool_name, len(result.content), settings.tool_max_output_bytes,
        )
        from gateway.mcp.client import ToolResult as _ToolResult
        result = _ToolResult(
            content=result.content[:settings.tool_max_output_bytes] + "\n[TRUNCATED]",
            is_error=result.is_error,
            duration_ms=getattr(result, "duration_ms", None),
            sources=getattr(result, "sources", None),
        )

    # Heuristic check for indirect prompt injection patterns in tool output
    _injection_detected = False
    if not result.is_error and result.content:
        _injection_patterns = [
            "ignore previous instructions",
            "ignore all previous",
            "disregard your instructions",
            "you are now",
            "new instructions:",
            "system prompt:",
            "override:",
            "<system>",
        ]
        content_lower = result.content if isinstance(result.content, str) else str(result.content)
        content_lower = content_lower.lower()
        for pattern in _injection_patterns:
            if pattern in content_lower:
                logger.warning(
                    "Potential indirect prompt injection in tool output: tool=%s pattern='%s'",
                    tc.tool_name, pattern,
                )
                _injection_detected = True
                break

    try:
        tool_calls_total.labels(provider=provider, tool_type=tc.tool_type, source="gateway").inc()
    except Exception:
        logger.debug("Metric increment failed (tool_calls_total gateway)", exc_info=True)

    # Analyse tool output BEFORE feeding back to the LLM (blocks indirect prompt injection)
    output_content = result.content
    is_error = result.is_error
    if (settings.tool_content_analysis_enabled
            and ctx.content_analyzers
            and output_content
            and not is_error):
        analysis = await analyze_text(output_content, ctx.content_analyzers)
        blocking = [d for d in analysis if d.get("verdict") == "block"]
        if blocking:
            top = blocking[0]
            logger.warning(
                "Tool output blocked before LLM injection: tool=%s category=%s analyzer=%s",
                tc.tool_name, top["category"], top["analyzer_id"],
            )
            output_content = f"[Tool output blocked by content policy: {top['category']}]"
            is_error = True

    _meta: dict = {"iteration": iteration, "duration_ms": duration_ms, "is_error": is_error}
    if _injection_detected:
        _meta["injection_warning"] = True
    enriched = ToolInteraction(
        tool_id=tc.tool_id, tool_type=tc.tool_type, tool_name=tc.tool_name,
        input_data=tc.input_data, output_data=output_content, sources=result.sources,
        metadata=_meta,
    )
    return enriched, {"tool_call_id": tc.tool_id, "content": output_content}


async def _run_active_tool_loop(
    adapter: ProviderAdapter,
    call: ModelCall,
    request: Request,
    model_response: ModelResponse,
    ctx,
    settings,
    provider: str,
) -> tuple[ModelCall, ModelResponse, Response | None, list[ToolInteraction], int, Response | None]:
    """Gateway-side tool-call loop for local/private models (active strategy).

    Returns (final_call, final_model_response, error_response_or_None, all_interactions, iterations, final_http_response).
    """
    all_interactions: list[ToolInteraction] = []
    iterations = 0
    current_call = call
    current_model = model_response
    final_http_resp: Response | None = None
    loop_deadline = time.perf_counter() + (settings.tool_loop_total_timeout_ms / 1000.0)

    while (
        current_model.has_pending_tool_calls
        and current_model.tool_interactions
        and iterations < settings.tool_max_iterations
        and time.perf_counter() < loop_deadline
    ):
        iterations += 1
        pending = current_model.tool_interactions
        tool_results: list[dict] = []

        for tc in pending:
            enriched, result_dict = await _execute_one_tool(tc, ctx, settings, provider, iterations)
            all_interactions.append(enriched)
            tool_results.append(result_dict)

        try:
            current_call = adapter.build_tool_result_call(current_call, pending, tool_results)
        except NotImplementedError:
            logger.warning("Adapter %s does not support build_tool_result_call — stopping tool loop", adapter.get_provider_name())
            break

        http_resp, current_model = await forward(adapter, current_call, request)
        final_http_resp = http_resp
        if http_resp.status_code >= 500:
            return current_call, current_model, http_resp, all_interactions, iterations, None

    if time.perf_counter() >= loop_deadline:
        logger.warning("Tool loop wall-clock timeout reached (%.0fms)", settings.tool_loop_total_timeout_ms)

    if iterations > 0:
        try:
            tool_loop_iterations.labels(provider=provider).observe(iterations)
        except Exception:
            logger.debug("Metric increment failed (tool_loop_iterations)", exc_info=True)

    return current_call, current_model, None, all_interactions, iterations, final_http_resp


# ── Session chain + record write helpers ─────────────────────────────────────

async def _apply_session_chain(record, session_id: str | None, ctx, settings) -> str | None:
    """Compute and attach session chain fields to record. Returns record_hash or None.

    On Redis error, skips chain fields and returns None so the execution record
    is still written without chain data — preferable to forging (0, GENESIS_HASH)
    for an established session, which would silently corrupt the Merkle chain.
    """
    if not (session_id and ctx.session_chain and settings.session_chain_enabled):
        return None
    try:
        seq_num, prev_hash = await ctx.session_chain.next_chain_values(session_id)
    except Exception:
        logger.error(
            "Session chain next_chain_values failed — skipping chain fields: session_id=%s",
            session_id, exc_info=True,
        )
        return None
    record_hash_val = compute_record_hash(
        execution_id=record["execution_id"],
        policy_version=record["policy_version"],
        policy_result=record["policy_result"],
        previous_record_hash=prev_hash,
        sequence_number=seq_num,
        timestamp=record["timestamp"],
    )
    record["sequence_number"] = seq_num
    record["previous_record_hash"] = prev_hash
    record["record_hash"] = record_hash_val

    # Phase 26: Ed25519 record signing (fail-open)
    if record_hash_val:
        try:
            from gateway.crypto.signing import sign_hash

            signature = sign_hash(record_hash_val)
            if signature:
                record["record_signature"] = signature
        except Exception:
            pass  # fail-open: never block record writing

    return record_hash_val


async def _store_execution(record, request: Request, ctx) -> None:
    """Write execution record via storage router, then tag request state."""
    eid = record["execution_id"]
    if ctx.storage:
        result = await ctx.storage.write_execution(record)
        if result.succeeded:
            execution_id_var.set(eid)
            request.state.walacor_execution_id = eid


# ── Post-stream background tasks ─────────────────────────────────────────────

async def _eval_post_stream_policy(ctx, settings, model_response) -> tuple[int, str, list]:
    """Run response policy after stream. Returns (version, result, decisions)."""
    if not (ctx.content_analyzers and ctx.policy_cache and settings.response_policy_enabled):
        return 0, "skipped", []
    _, version, result, decisions, _ = await evaluate_post_inference(
        ctx.policy_cache, model_response, ctx.content_analyzers
    )
    if result == "blocked":
        result = "flagged_post_stream"
    try:
        response_policy_total.labels(result=result).inc()
    except Exception:
        logger.debug("Metric increment failed (response_policy_total post-stream)", exc_info=True)
    return version, result, decisions


async def _after_stream_record(
    buffer: list[bytes],
    call: ModelCall,
    adapter: ProviderAdapter,
    attestation_id: str,
    policy_version: int,
    policy_result: str,
    audit_metadata: dict,
    budget_estimated: int = 0,
    pipeline_start: float | None = None,
    governance_meta: dict | None = None,
    request: Request | None = None,
) -> None:
    """Background task: after stream ends, evaluate response, build chain, write record (no hashing — Walcor hashes).

    budget_estimated: tokens reserved in check_and_reserve; passed to record_usage so the
    budget tracker can apply only the actual-vs-estimated delta (Finding 4).
    """
    ctx = get_pipeline_context()
    settings = get_settings()
    if not ctx.wal_writer and not ctx.walacor_client:
        return
    _exec_id = "unknown"
    try:
        model_response = adapter.parse_streamed_response(buffer)

        if isinstance(adapter, OllamaAdapter) and call.model_id:
            model_hash = await adapter.fetch_model_hash(call.model_id, ctx.http_client)
            if model_hash:
                model_response = dataclasses.replace(model_response, model_hash=model_hash)

        await _record_token_usage(
            model_response, settings.gateway_tenant_id,
            adapter.get_provider_name(), call.metadata.get("user"),
            estimated=budget_estimated,
        )

        rp_version, rp_result, rp_decisions = await _eval_post_stream_policy(ctx, settings, model_response)

        # Phase 14: capture passive tool interactions from streamed response
        stream_tool_meta: dict[str, Any] = {}
        if model_response.tool_interactions:
            stream_tool_meta = _build_tool_audit_metadata(model_response.tool_interactions, "passive", 0)
            _emit_tool_metrics(model_response.tool_interactions, adapter.get_provider_name(), "provider")

        session_id = call.metadata.get("session_id")
        record = build_execution_record(
            call=call, model_response=model_response, attestation_id=attestation_id,
            policy_version=policy_version, policy_result=policy_result,
            tenant_id=settings.gateway_tenant_id, gateway_id=settings.gateway_id,
            user=call.metadata.get("user"), session_id=session_id,
            metadata={
                **call.metadata, **audit_metadata, **stream_tool_meta,
                "response_policy_version": rp_version,
                "response_policy_result": rp_result,
                "analyzer_decisions": rp_decisions,
                "enforcement_mode": settings.enforcement_mode,
            },
            model_id=call.model_id, provider=adapter.get_provider_name(),
            latency_ms=round((time.perf_counter() - pipeline_start) * 1000, 1) if pipeline_start else None,
            file_metadata=getattr(request.state, "file_metadata", None) if request else None,
        )
        _exec_id = record.get("execution_id", "unknown")

        record_hash_val = await _apply_session_chain(record, session_id, ctx, settings)
        if ctx.storage:
            result = await ctx.storage.write_execution(record)
            if result.succeeded:
                execution_id_var.set(record["execution_id"])
        await _write_tool_events(model_response.tool_interactions or [], record["execution_id"], call, "passive", ctx, settings)
        if session_id and ctx.session_chain and record_hash_val is not None:
            await ctx.session_chain.update(session_id, record["sequence_number"], record_hash_val)
        # Phase 23: populate governance_meta for SSE event injection
        if governance_meta is not None:
            governance_meta["execution_id"] = record.get("execution_id")
            governance_meta["chain_seq"] = record.get("sequence_number")
            governance_meta["content_analysis"] = _summarize_content_analysis(rp_decisions)
    except Exception as e:
        logger.error(
            "After-stream execution record write failed: execution_id=%s prompt_id=%s session_id=%s error=%s",
            _exec_id, call.metadata.get("prompt_id"), call.metadata.get("session_id"), e,
            exc_info=True,
        )


async def _skip_governance_after_stream(
    buffer: list[bytes],
    call: ModelCall,
    adapter: ProviderAdapter,
    pipeline_start: float | None = None,
    request: Request | None = None,
) -> None:
    """In skip_governance mode: build execution record from stream buffer and write to Walacor/WAL."""
    ctx = get_pipeline_context()
    settings = get_settings()
    if not ctx.storage:
        return
    if request and getattr(request.state, "skip_audit", None) is True:
        return
    try:
        model_response = adapter.parse_streamed_response(buffer)
        if isinstance(adapter, OllamaAdapter) and call.model_id and ctx.http_client:
            model_hash = await adapter.fetch_model_hash(call.model_id, ctx.http_client)
            if model_hash:
                model_response = dataclasses.replace(model_response, model_hash=model_hash)
        record = build_execution_record(
            call=call, model_response=model_response, attestation_id="skip_governance",
            policy_version=0, policy_result="pass",
            tenant_id=settings.gateway_tenant_id, gateway_id=settings.gateway_id,
            user=call.metadata.get("user"), session_id=call.metadata.get("session_id"),
            metadata={"enforcement_mode": "skip_governance"},
            model_id=call.model_id, provider=adapter.get_provider_name(),
            latency_ms=round((time.perf_counter() - pipeline_start) * 1000, 1) if pipeline_start else None,
            file_metadata=getattr(request.state, "file_metadata", None) if request else None,
        )
        if ctx.storage:
            await ctx.storage.write_execution(record)
        execution_id_var.set(record["execution_id"])
    except Exception as e:
        logger.error("Skip-governance after-stream write failed: %s", e, exc_info=True)


# ── Governance sub-step helpers ───────────────────────────────────────────────

async def _attestation_check(
    request: Request, adapter: ProviderAdapter, call: ModelCall,
    ctx, settings, is_audit_only: bool, provider: str, model: str,
) -> tuple[str, dict, bool, str | None, Response | None]:
    """Step 1. Returns (attestation_id, context, would_block, reason, error_resp)."""
    att_id = _AUDIT_ONLY_ATTESTATION_ID
    att_ctx: dict = {"model_id": call.model_id, "provider": adapter.get_provider_name(), "status": "active", "verification_level": "audit_only", "tenant_id": settings.gateway_tenant_id}

    async def try_refresh() -> bool:
        return await ctx.sync_client.sync_attestations(provider=adapter.get_provider_name()) if ctx.sync_client else False

    attestation, err = await resolve_attestation(ctx.attestation_cache, adapter.get_provider_name(), call.model_id, try_refresh=try_refresh)
    if err is not None:
        # Auto-attest when no remote sync client is configured.
        # With embedded control plane: auto-attest only if the model was NEVER explicitly revoked.
        # Without control plane: always auto-attest (standalone governance mode).
        _can_auto_attest = False
        if ctx.sync_client is None:
            if ctx.control_store is None:
                _can_auto_attest = True
            else:
                # Check if model was explicitly revoked in the control store.
                # If it was never registered or is active, allow auto-attestation.
                existing = [
                    a for a in ctx.control_store.list_attestations(settings.gateway_tenant_id)
                    if a.get("model_id") == call.model_id and a.get("provider") == adapter.get_provider_name()
                ]
                if not existing or existing[0].get("status") != "revoked":
                    _can_auto_attest = True

        if _can_auto_attest:
            from datetime import datetime, timezone
            from gateway.cache.attestation_cache import CachedAttestation

            auto_att = CachedAttestation(
                attestation_id=f"self-attested:{call.model_id}",
                model_id=call.model_id,
                provider=adapter.get_provider_name(),
                status="active",
                fetched_at=datetime.now(timezone.utc),
                ttl_seconds=settings.attestation_cache_ttl,
                tenant_id=settings.gateway_tenant_id,
                verification_level="self_attested",
            )
            ctx.attestation_cache.set(auto_att)
            # Also persist to control store so it shows up in the dashboard
            if ctx.control_store is not None:
                ctx.control_store.upsert_attestation({
                    "attestation_id": auto_att.attestation_id,
                    "model_id": call.model_id,
                    "provider": adapter.get_provider_name(),
                    "status": "active",
                    "verification_level": "auto_attested",
                    "tenant_id": settings.gateway_tenant_id,
                    "notes": "Auto-attested on first use",
                })
                logger.info("Auto-attested model: provider=%s model=%s (registered in control plane)", adapter.get_provider_name(), call.model_id)
            else:
                logger.info("Auto-attested model: provider=%s model=%s (no control plane)", adapter.get_provider_name(), call.model_id)
            att_id = auto_att.attestation_id
            att_ctx = {"model_id": call.model_id, "provider": adapter.get_provider_name(), "status": "active", "verification_level": "self_attested", "tenant_id": settings.gateway_tenant_id}
            _inject_caller_role(att_ctx, request)
            return att_id, att_ctx, False, None, None
        if is_audit_only:
            logger.warning("AUDIT_ONLY: Would have blocked (attestation) provider=%s model=%s", provider, model)
            _inject_caller_role(att_ctx, request)
            return att_id, att_ctx, True, "attestation", None
        # Model was explicitly revoked — block with clear message
        _set_disposition(request, "denied_attestation")
        _inc_request(provider, model, "blocked_stale" if err.status_code == 503 else "blocked_attestation")
        _inject_caller_role(att_ctx, request)
        return att_id, att_ctx, False, None, err

    att_id = attestation.attestation_id
    att_ctx = {
        "model_id": call.model_id,
        "provider": getattr(attestation, "provider", adapter.get_provider_name()),
        "status": getattr(attestation, "status", "active"),
        "verification_level": getattr(attestation, "verification_level", "self_reported"),
        "tenant_id": attestation.tenant_id or settings.gateway_tenant_id,
    }
    _inject_caller_role(att_ctx, request)
    return att_id, att_ctx, False, None, None


def _get_policies_for_key(api_key: str | None, ctx) -> list | None:
    """Return per-key policies if assigned, otherwise None (use global policies).

    Per-key policies are fetched from the embedded control store using a SHA-256
    hash of the raw API key (never stored in plaintext).  If no assignments exist
    for this key, None is returned and the caller falls back to the global policy
    cache.
    """
    if not api_key or ctx.control_store is None:
        return None
    import hashlib
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    policy_ids = ctx.control_store.get_key_policies(key_hash)
    if not policy_ids:
        return None
    policies = []
    for pid in policy_ids:
        policy = ctx.control_store.get_policy(pid)
        if policy:
            policies.append(policy)
    return policies if policies else None


async def _pre_policy_check(
    request: Request, call: ModelCall, ctx, is_audit_only: bool,
    att_id: str, att_ctx: dict, whb: bool, reason: str | None, provider: str, model: str,
) -> tuple[int, str, bool, str | None, Response | None]:
    """Step 2. Returns (policy_version, policy_result, would_block, reason, error_resp)."""
    settings = get_settings()

    # OPA policy engine path
    if settings.policy_engine == "opa":
        from gateway.pipeline.opa_evaluator import query_opa
        allowed, opa_reason = await query_opa(
            settings.opa_url, settings.opa_policy_path, att_ctx, ctx.http_client
        )
        if not allowed:
            if is_audit_only:
                logger.warning("AUDIT_ONLY: Would have blocked (OPA) provider=%s model=%s reason=%s", provider, model, opa_reason)
                return 0, opa_reason, True, reason or "opa", None
            _set_disposition(request, "denied_by_opa")
            _inc_request(provider, model, "blocked_policy")
            return 0, opa_reason, whb, reason, JSONResponse(
                {"error": "Blocked by OPA policy", "reason": opa_reason},
                status_code=403,
            )
        return 0, "opa_allow", whb, reason, None

    # Check for per-key policy override — use only the assigned policies for this key
    # if any are configured; otherwise fall through to the global policy cache.
    from gateway.auth.api_key import get_api_key_from_request
    raw_api_key = get_api_key_from_request(request)
    per_key_policies = _get_policies_for_key(raw_api_key, ctx)
    if per_key_policies is not None:
        logger.debug(
            "Per-key policy override: key_hash=%.8s... policies=%d",
            __import__("hashlib").sha256((raw_api_key or "").encode()).hexdigest(),
            len(per_key_policies),
        )
        # Evaluate using only the per-key policy set via a temporary PolicyCache
        from gateway.cache.policy_cache import PolicyCache
        per_key_cache = PolicyCache(staleness_threshold_seconds=86400)
        per_key_cache.set_policies(ctx.policy_cache.version if ctx.policy_cache else 0, per_key_policies)
        _, pv, pr, err = evaluate_pre_inference(per_key_cache, call, att_id, att_ctx)
        if err is not None:
            if is_audit_only:
                logger.warning("AUDIT_ONLY: Would have blocked (per-key policy) provider=%s model=%s", provider, model)
                return pv, pr, True, reason or "policy", None
            _set_disposition(request, "denied_policy")
            _inc_request(provider, model, "blocked_stale" if err.status_code == 503 else "blocked_policy")
            return pv, pr, whb, reason, err
        return pv, pr, whb, reason, None

    # Builtin policy engine path (global policies)
    _, pv, pr, err = evaluate_pre_inference(ctx.policy_cache, call, att_id, att_ctx)
    if err is not None:
        if is_audit_only:
            logger.warning("AUDIT_ONLY: Would have blocked (pre-policy) provider=%s model=%s", provider, model)
            return pv, pr, True, reason or "policy", None
        _set_disposition(request, "denied_policy")
        _inc_request(provider, model, "blocked_stale" if err.status_code == 503 else "blocked_policy")
        return pv, pr, whb, reason, err
    return pv, pr, whb, reason, None


def _wal_backpressure_check(request: Request, ctx, settings, provider: str, model: str) -> Response | None:
    """Step 2.5. Returns 503 if WAL is at capacity, None if OK."""
    if not (ctx.wal_writer and not ctx.walacor_client):
        return None
    pending = ctx.wal_writer.pending_count()
    disk_bytes = ctx.wal_writer.disk_usage_bytes()
    max_bytes = int(settings.wal_max_size_gb * (1024 ** 3))
    if pending >= settings.wal_high_water_mark or (max_bytes > 0 and disk_bytes >= max_bytes):
        _set_disposition(request, "denied_wal_full")
        _inc_request(provider, model, "error")
        return JSONResponse({"error": "WAL retention exhausted; control plane unreachable or backlog too large"}, status_code=503)
    return None


async def _budget_check(
    request: Request, ctx, settings, is_audit_only: bool,
    call: ModelCall, whb: bool, reason: str | None, provider: str, model: str,
) -> tuple[int | None, int, bool, str | None, Response | None]:
    """Step 2.6. Returns (budget_remaining, budget_estimated, would_block, reason, error_resp).

    budget_estimated is the number of tokens reserved now; it must be threaded to
    _record_token_usage so record_usage can apply only the actual-vs-estimated delta.
    """
    if not (ctx.budget_tracker and settings.token_budget_enabled):
        return None, 0, whb, reason, None
    estimated = max(len(call.prompt_text) // 4, 1)
    try:
        allowed, remaining = await ctx.budget_tracker.check_and_reserve(
            settings.gateway_tenant_id, call.metadata.get("user"), estimated
        )
    except Exception:
        logger.warning(
            "Budget check_and_reserve failed: tenant_id=%s estimated=%d — FAILING OPEN (request allowed without budget check)",
            settings.gateway_tenant_id, estimated, exc_info=True,
        )
        try:
            from gateway.metrics.prometheus import budget_failopen_total
            budget_failopen_total.inc()
        except Exception:
            pass
        return None, 0, whb, reason, None  # fail-open: allow request
    budget_rem: int | None = remaining if remaining >= 0 else None
    if not allowed:
        if is_audit_only:
            logger.warning("AUDIT_ONLY: Would have blocked (budget) provider=%s model=%s", provider, model)
            return budget_rem, 0, True, reason or "budget", None
        _set_disposition(request, "denied_budget")
        _inc_request(provider, model, "error")
        try:
            budget_exceeded_total.labels(tenant_id=settings.gateway_tenant_id).inc()
        except Exception:
            logger.debug("Metric increment failed (budget_exceeded_total)", exc_info=True)
        return budget_rem, 0, whb, reason, JSONResponse({"error": "Token budget exhausted"}, status_code=429)
    return budget_rem, estimated, whb, reason, None


async def _rate_limit_check(request, ctx, settings, call, provider, model) -> Response | None:
    """Step 2.8: Rate limit check. Returns 429 response or None."""
    user = call.metadata.get("user", "anonymous")
    key = f"{user}:{call.model_id}" if settings.rate_limit_per_model else user
    allowed, remaining = await ctx.rate_limiter.check(key, settings.rate_limit_rpm, window_seconds=60)
    # Store rate limit info for response headers
    request.state.walacor_ratelimit_limit = settings.rate_limit_rpm
    request.state.walacor_ratelimit_remaining = remaining
    request.state.walacor_ratelimit_reset = int(ctx.rate_limiter.reset_time(key, 60))
    if not allowed:
        _set_disposition(request, "denied_rate_limit")
        _inc_request(provider, model, "error")
        try:
            rate_limit_hits_total.labels(model=call.model_id).inc()
        except Exception:
            logger.debug("Metric increment failed (rate_limit_hits_total)", exc_info=True)
        retry_after = max(1, request.state.walacor_ratelimit_reset - int(time.time()))
        resp = JSONResponse(
            {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
            status_code=429,
        )
        resp.headers["Retry-After"] = str(retry_after)
        resp.headers["X-RateLimit-Limit"] = str(settings.rate_limit_rpm)
        resp.headers["X-RateLimit-Remaining"] = "0"
        resp.headers["X-RateLimit-Reset"] = str(request.state.walacor_ratelimit_reset)
        return resp
    return None


def _add_rate_limit_headers(response, request):
    """Add X-RateLimit-* headers to response if rate limit data is available."""
    limit = getattr(request.state, "walacor_ratelimit_limit", None)
    if limit is not None:
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(getattr(request.state, "walacor_ratelimit_remaining", 0))
        response.headers["X-RateLimit-Reset"] = str(getattr(request.state, "walacor_ratelimit_reset", 0))


# ── Pre-check orchestration (Steps 1–2.7) ────────────────────────────────────

async def _run_pre_checks(
    request: Request, adapter: ProviderAdapter, call: ModelCall,
    ctx, settings, is_audit_only: bool, provider: str, model: str,
) -> _PreCheckResult:
    """Steps 1–2.7: attestation, policy, WAL backpressure, budget, tool inject."""
    step_timings: dict[str, float] = {}

    if not ctx.attestation_cache:
        _set_disposition(request, "error_config")
        _inc_request(provider, model, "error")
        return _PreCheckResult(error=JSONResponse({"error": "Attestation cache not configured"}, status_code=503))

    t_step = time.perf_counter()
    att_id, att_ctx, whb, reason, err = await _attestation_check(
        request, adapter, call, ctx, settings, is_audit_only, provider, model
    )
    step_timings["attestation_ms"] = round((time.perf_counter() - t_step) * 1000, 1)
    if err is not None:
        return _PreCheckResult(error=err)

    if not ctx.policy_cache and settings.policy_engine != "opa":
        _set_disposition(request, "error_config")
        _inc_request(provider, model, "error")
        return _PreCheckResult(error=JSONResponse({"error": "Policy cache not configured"}, status_code=503))

    # WAL backpressure is sync and fast — check before launching concurrent tasks.
    if not is_audit_only:
        wal_err = _wal_backpressure_check(request, ctx, settings, provider, model)
        if wal_err is not None:
            return _PreCheckResult(error=wal_err)

    # Run policy, budget, and rate-limit checks concurrently.
    # Policy depends on att_ctx (from attestation above) but is independent of budget/rate-limit.
    t_parallel = time.perf_counter()

    async def _policy_task():
        t = time.perf_counter()
        result = await _pre_policy_check(
            request, call, ctx, is_audit_only, att_id, att_ctx, whb, reason, provider, model
        )
        return result, round((time.perf_counter() - t) * 1000, 1)

    async def _budget_task():
        t = time.perf_counter()
        result = await _budget_check(
            request, ctx, settings, is_audit_only, call, whb, reason, provider, model
        )
        return result, round((time.perf_counter() - t) * 1000, 1)

    async def _rate_limit_task():
        if settings.rate_limit_enabled and ctx.rate_limiter:
            return await _rate_limit_check(request, ctx, settings, call, provider, model)
        return None

    policy_res, budget_res, rl_err = await asyncio.gather(
        _policy_task(), _budget_task(), _rate_limit_task()
    )

    (pv, pr, whb, reason, pol_err), pol_ms = policy_res
    step_timings["policy_ms"] = pol_ms
    if pol_err is not None:
        return _PreCheckResult(error=pol_err)

    (budget_rem, budget_est, whb, reason, bud_err), bud_ms = budget_res
    step_timings["budget_ms"] = bud_ms
    if bud_err is not None:
        return _PreCheckResult(error=bud_err)

    if rl_err is not None:
        return _PreCheckResult(error=rl_err)

    step_timings["parallel_checks_ms"] = round((time.perf_counter() - t_parallel) * 1000, 1)

    # Shadow policy evaluation (observe-only, non-blocking)
    if settings.shadow_policy_enabled and ctx.control_store is not None:
        try:
            shadow_policies = ctx.control_store.list_shadow_policies(settings.gateway_tenant_id)
            if shadow_policies:
                from gateway.pipeline.shadow_policy import run_shadow_policies
                shadow_results = await run_shadow_policies(shadow_policies, att_ctx)
                call.metadata["shadow_policy_results"] = shadow_results
        except Exception as e:
            logger.warning("Shadow policy evaluation failed: %s", e)

    tool_strategy = _select_tool_strategy(adapter, settings)
    if tool_strategy == "active" and ctx.tool_registry and ctx.tool_registry.get_tool_count() > 0:
        # Skip tool injection if we already know this model doesn't support tools
        _sup = None
        if ctx.capability_registry:
            _sup = ctx.capability_registry.supports_tools(call.model_id)
        if _sup is None:
            _sup = _model_supports_tools(call.model_id)
        if _sup is False:
            logger.debug("Skipping tool injection for %s — known to not support tools", call.model_id)
            tool_strategy = "none"
        else:
            from gateway.auth.api_key import get_api_key_from_request as _get_key
            _api_key = _get_key(request)
            _tool_defs = _filter_tools_for_key(
                ctx.tool_registry.get_tool_definitions(), _api_key, ctx
            )
            call = _inject_tools_into_call(call, _tool_defs)

    audit_metadata: dict = {}
    if is_audit_only:
        audit_metadata = {
            "enforcement_mode": "audit_only",
            "would_have_blocked": whb,
            "would_have_blocked_reason": reason,
        }

    return _PreCheckResult(
        att_id=att_id, pv=pv, pr=pr,
        budget_remaining=budget_rem, budget_estimated=budget_est,
        call=call, audit_metadata=audit_metadata,
        tool_strategy=tool_strategy, whb=whb, reason=reason,
        timings=step_timings,
    )


# ── Skip-governance path ──────────────────────────────────────────────────────

async def _handle_skip_governance_non_streaming(
    request: Request, adapter: ProviderAdapter, call: ModelCall,
    ctx, settings, t0: float, provider: str, model: str,
) -> Response:
    response, model_response = await forward(adapter, call, request)
    if ctx.storage and getattr(request.state, "skip_audit", None) is not True:
        if isinstance(adapter, OllamaAdapter) and call.model_id and ctx.http_client:
            mh = await adapter.fetch_model_hash(call.model_id, ctx.http_client)
            if mh:
                model_response = dataclasses.replace(model_response, model_hash=mh)
        record = build_execution_record(
            call=call, model_response=model_response, attestation_id="skip_governance",
            policy_version=0, policy_result="pass",
            tenant_id=settings.gateway_tenant_id, gateway_id=settings.gateway_id,
            user=call.metadata.get("user"), session_id=call.metadata.get("session_id"),
            metadata={"enforcement_mode": "skip_governance"},
            model_id=call.model_id, provider=adapter.get_provider_name(),
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            file_metadata=getattr(request.state, "file_metadata", None),
        )
        if ctx.storage:
            result = await ctx.storage.write_execution(record)
            if result.succeeded:
                execution_id_var.set(record["execution_id"])
                request.state.walacor_execution_id = record["execution_id"]
    pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
    _inc_request(provider, model, "allowed")
    return response


async def _handle_skip_governance(
    request: Request, adapter: ProviderAdapter, call: ModelCall,
    ctx, settings, t0: float, provider: str, model: str,
) -> Response:
    """Complete skip-governance path — streaming and non-streaming."""
    _set_disposition(request, "allowed")
    if call.is_streaming:
        buf: list[bytes] = []
        task = BackgroundTask(_skip_governance_after_stream, buf, call, adapter, t0, request)
        resp, _ = await stream_with_tee(adapter, call, request, buffer=buf, background_task=task)
        pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
        _inc_request(provider, model, "allowed")
        return resp
    return await _handle_skip_governance_non_streaming(request, adapter, call, ctx, settings, t0, provider, model)


# ── Non-streaming path helpers ────────────────────────────────────────────────

async def _maybe_fetch_ollama_hash(
    adapter: ProviderAdapter, call: ModelCall, model_response: ModelResponse, ctx,
) -> ModelResponse:
    """Fetch and attach model hash for Ollama non-streaming responses."""
    if isinstance(adapter, OllamaAdapter) and call.model_id:
        mh = await adapter.fetch_model_hash(call.model_id, ctx.http_client)
        if mh:
            return cast(ModelResponse, dataclasses.replace(model_response, model_hash=mh))
    return model_response


async def _route_tool_strategy(
    tool_strategy: str,
    model_response: ModelResponse,
    call: ModelCall,
    adapter: ProviderAdapter,
    ctx,
    settings,
    request: Request,
    provider: str,
) -> _ToolStrategyResult:
    """Step 3.5: passive — collect tool interactions; active — run tool loop."""
    if tool_strategy == "passive" and model_response.tool_interactions:
        interactions = list(model_response.tool_interactions)
        _emit_tool_metrics(interactions, provider, "provider")
        return _ToolStrategyResult(
            call=call, model_response=model_response,
            interactions=interactions, iterations=0, error=None,
        )
    if tool_strategy == "active" and ctx.tool_registry and model_response.has_pending_tool_calls:
        try:
            call, model_response, loop_err, interactions, iters, final_http = await _run_active_tool_loop(
                adapter, call, request, model_response, ctx, settings, provider
            )
        except Exception:
            logger.error("Active tool loop failed — falling back to original response", exc_info=True)
            return _ToolStrategyResult(
                call=call, model_response=model_response,
                interactions=[], iterations=0, error=None,
            )
        return _ToolStrategyResult(
            call=call, model_response=model_response,
            interactions=interactions, iterations=iters, error=loop_err,
            http_response=final_http,
        )
    return _ToolStrategyResult(call=call, model_response=model_response, interactions=[], iterations=0, error=None)


# ── Input content analysis (B.7: parallel mode) ───────────────────────────────

async def _run_input_analysis_async(call: ModelCall, ctx) -> list[dict]:
    """Run content analyzers on the prompt text (input side).

    Used in parallel-analysis mode (B.7): this coroutine is gathered alongside
    the LLM forward call so that input PII/toxicity analysis does not add to the
    end-to-end latency when the analyzer is slower than the LLM call.

    Always fail-open — errors are logged and an empty list is returned so the
    main pipeline is never blocked by analyzer unavailability.

    Returns a list of decision dicts (same shape as evaluate_post_inference
    analyzer_decisions) or [] when analyzers are disabled / no prompt text.
    """
    if not ctx.content_analyzers or not call.prompt_text:
        return []
    try:
        from gateway.pipeline.response_evaluator import analyze_text
        return await analyze_text(call.prompt_text, ctx.content_analyzers)
    except Exception:
        logger.warning("Input content analysis failed (fail-open)", exc_info=True)
        return []


# ── Response policy ───────────────────────────────────────────────────────────

async def _run_response_policy(
    request: Request, ctx, settings, is_audit_only: bool,
    model_response: ModelResponse, audit_metadata: dict,
    provider: str, model: str, t0: float,
) -> tuple[int, str, list, bool, str | None, Response | None]:
    """Step 4. Returns (rp_version, rp_result, decisions, would_block, reason, error_resp)."""
    rp_version, rp_result, decisions = 0, "skipped", []
    whb = audit_metadata.get("would_have_blocked", False)
    reason: str | None = audit_metadata.get("would_have_blocked_reason")

    if not (ctx.content_analyzers and ctx.policy_cache and settings.response_policy_enabled):
        return rp_version, rp_result, decisions, whb, reason, None

    _, rp_version, rp_result, decisions, resp_err = await evaluate_post_inference(
        ctx.policy_cache, model_response, ctx.content_analyzers
    )
    try:
        response_policy_total.labels(result=rp_result).inc()
    except Exception:
        logger.debug("Metric increment failed (response_policy_total)", exc_info=True)

    if resp_err is None:
        return rp_version, rp_result, decisions, whb, reason, None

    if is_audit_only:
        whb, reason = True, reason or "response_policy"
        audit_metadata.update({"would_have_blocked": True, "would_have_blocked_reason": reason})
        logger.warning("AUDIT_ONLY: Would have blocked (response_policy) provider=%s model=%s", provider, model)
        return rp_version, rp_result, decisions, whb, reason, None

    _set_disposition(request, "denied_response_policy")
    _inc_request(provider, model, "blocked_response_policy")
    # Increment per-analyzer content block counters
    for d in (decisions or []):
        if d.get("action") == "block":
            try:
                content_blocks_total.labels(analyzer=d.get("analyzer", "unknown")).inc()
            except Exception:
                logger.debug("Metric increment failed (content_blocks_total)", exc_info=True)
    pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
    return rp_version, rp_result, decisions, whb, reason, resp_err


# ── Record build + write (Steps 6–8) ─────────────────────────────────────────

async def _build_and_write_record(
    request: Request,
    call: ModelCall,
    model_response: ModelResponse,
    params: _AuditParams,
    ctx,
    settings,
) -> None:
    """Steps 6-8: build record, apply session chain, write to storage."""
    # Skip audit for system-generated requests (OpenWebUI tags/suggestions/titles)
    if getattr(request.state, "skip_audit", None) is True:
        logger.debug("Skipping audit record for system task: %s", call.metadata.get("request_type"))
        return
    session_id = call.metadata.get("session_id")
    tool_meta = _build_tool_audit_metadata(params.tool_interactions, params.tool_strategy, params.tool_iterations)

    record = build_execution_record(
        call=call, model_response=model_response, attestation_id=params.attestation_id,
        policy_version=params.policy_version,
        policy_result=params.policy_result,
        tenant_id=settings.gateway_tenant_id, gateway_id=settings.gateway_id,
        user=call.metadata.get("user"), session_id=session_id,
        metadata={
            **call.metadata, **params.audit_metadata, **tool_meta,
            "response_policy_version": params.rp_version,
            "response_policy_result": params.rp_result,
            "analyzer_decisions": params.rp_decisions,
            "token_usage": model_response.usage,
            "budget_remaining": params.budget_remaining,
            "enforcement_mode": settings.enforcement_mode,
        },
        model_id=call.model_id, provider=params.provider,
        latency_ms=params.latency_ms,
        timings=params.timings,
        variant_id=params.variant_id,
        file_metadata=getattr(request.state, "file_metadata", None),
    )
    # Cost attribution: compute estimated cost from pricing table
    if ctx.control_store:
        try:
            pricing = ctx.control_store.get_model_pricing(call.model_id or "")
            if pricing:
                prompt_t = record.get("prompt_tokens", 0) or 0
                completion_t = record.get("completion_tokens", 0) or 0
                cost = (
                    prompt_t * pricing["input_cost_per_1k"] / 1000
                    + completion_t * pricing["output_cost_per_1k"] / 1000
                )
                record["estimated_cost_usd"] = round(cost, 6)
        except Exception:
            logger.debug("Cost computation failed (non-fatal)", exc_info=True)

    t_chain = time.perf_counter()
    record_hash_val = await _apply_session_chain(record, session_id, ctx, settings)
    if params.timings is not None:
        params.timings["chain_ms"] = round((time.perf_counter() - t_chain) * 1000, 1)
    await _store_execution(record, request, ctx)
    # Expose governance metadata for response headers (Phase 23)
    request.state.walacor_chain_seq = record.get("sequence_number")
    await _write_tool_events(params.tool_interactions, record["execution_id"], call, params.tool_strategy, ctx, settings)
    if session_id and ctx.session_chain and record_hash_val is not None:
        try:
            await ctx.session_chain.update(session_id, record["sequence_number"], record_hash_val)
        except Exception:
            logger.error(
                "Session chain update failed — chain state may be stale: session_id=%s seq_num=%d",
                session_id, record["sequence_number"], exc_info=True,
            )

    # Phase 17: OTel GenAI span (fail-open; emitted after write so execution_id is set)
    if ctx.tracer is not None:
        try:
            from gateway.telemetry.otel import emit_inference_span
            usage = model_response.usage or {}
            emit_inference_span(
                tracer=ctx.tracer,
                provider=call.provider,
                model_id=call.model_id,
                prompt_tokens=usage.get("prompt_tokens", 0) or 0,
                completion_tokens=usage.get("completion_tokens", 0) or 0,
                execution_id=record["execution_id"],
                policy_result=params.policy_result,
                tenant_id=settings.gateway_tenant_id,
                session_id=session_id,
                tool_count=len(params.tool_interactions),
                has_thinking=model_response.thinking_content is not None,
                provider_request_id=model_response.provider_request_id,
            )
        except Exception:
            logger.debug("OTel emit_inference_span failed (fail-open)", exc_info=True)

    # B.2: Export to SIEM/S3/file if configured (fail-open, non-critical)
    if ctx.audit_exporter is not None:
        try:
            await ctx.audit_exporter.export(record)
        except Exception:
            logger.debug("Audit exporter failed (non-critical)", exc_info=True)


# ── Main entry point ──────────────────────────────────────────────────────────

async def handle_request(request: Request) -> Response:
    """Run the full 8-step pipeline (+ Phase 14 tool strategy at step 3.5)."""
    t0 = time.perf_counter()
    inflight_requests.inc()
    try:
        return await _handle_request_inner(request, t0)
    finally:
        inflight_requests.dec()


def _record_status(status_code: int, source: str = "gateway") -> None:
    """Record HTTP response status code metric."""
    try:
        response_status_total.labels(status_code=str(status_code), source=source).inc()
    except Exception:
        logger.debug("Metric increment failed (response_status_total)", exc_info=True)


async def _handle_request_inner(request: Request, t0: float) -> Response:
    """Inner handler: full pipeline logic, called with inflight tracking around it."""
    _set_disposition(request, "error_gateway")
    settings = get_settings()
    is_audit_only = settings.enforcement_mode == "audit_only"

    if request.method != "POST":
        _set_disposition(request, "error_method_not_allowed")
        _inc_request("unknown", "unknown", "error")
        resp = JSONResponse({"error": "Method not allowed"}, status_code=405)
        _record_status(405)
        return resp

    model_hint = await _peek_model_id(request) if get_settings().model_routes else ""
    adapter = _resolve_adapter(request.url.path, model_hint)
    if not adapter:
        _set_disposition(request, "error_no_adapter")
        _inc_request("unknown", "unknown", "error")
        resp = JSONResponse({"error": "No adapter for this path"}, status_code=404)
        _record_status(404)
        return resp

    try:
        call = await adapter.parse_request(request)
    except Exception as e:
        logger.warning("parse_request failed: %s", e)
        _set_disposition(request, "error_parse")
        _inc_request("unknown", "unknown", "error")
        resp = JSONResponse({"error": "Invalid request body"}, status_code=400)
        _record_status(400)
        return resp

    # Inject prompt_id + client context for end-to-end audit correlation
    prompt_id = request_id_var.get()
    client_context: dict[str, Any] = {}
    if request.client:
        client_context["ip"] = request.client.host
    ua = request.headers.get("user-agent")
    if ua:
        client_context["user_agent"] = ua
    xff = request.headers.get("x-forwarded-for")
    if xff:
        client_context["x_forwarded_for"] = xff
    app_ver = request.headers.get("x-app-version")
    if app_ver:
        client_context["app_version"] = app_ver
    extra: dict[str, Any] = {"prompt_id": prompt_id}
    if client_context:
        extra["client_context"] = client_context
    # Classify request type: prefer metadata from OpenWebUI filter plugin,
    # fall back to multi-source adaptive classifier.
    _meta_rt = call.metadata.get("request_type")
    _rc = get_pipeline_context().request_classifier
    if _meta_rt:
        extra["request_type"] = _meta_rt
    elif _rc:
        # Use cached parsed body from request.state if available (set by _peek_model_id),
        # otherwise parse once and cache for future use.
        body_dict = getattr(request.state, "_parsed_body", None)
        if body_dict is None:
            try:
                body_dict = json.loads(call.raw_body) if call.raw_body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                body_dict = {}
        extra["request_type"] = _rc.classify(
            call.prompt_text or "", dict(request.headers), body_dict)
    else:
        extra["request_type"] = _classify_request_type(call.prompt_text or "")
    # Propagate OpenWebUI message ID for per-message audit correlation
    msg_id = request.headers.get("x-openwebui-message-id")
    if msg_id:
        extra["message_id"] = msg_id
    # Merge caller identity from middleware (JWT or header-based).
    # Second-pass: if middleware didn't resolve identity (no headers),
    # try body metadata (OpenWebUI plugin injects user info there).
    caller_identity = getattr(request.state, "caller_identity", None)
    if caller_identity is None or caller_identity.source == "anonymous":
        try:
            from gateway.auth.identity import resolve_identity_from_headers
            _body_meta = (body_dict if isinstance(body_dict, dict) else {}).get("metadata")
            if not isinstance(_body_meta, dict):
                _body_meta = None
            resolved = resolve_identity_from_headers(request, body_metadata=_body_meta)
            if resolved and (caller_identity is None or resolved.source != "anonymous"):
                caller_identity = resolved
                request.state.caller_identity = resolved
        except Exception:
            pass
    if caller_identity is not None:
        if not extra.get("user"):
            extra["user"] = caller_identity.user_id
        if caller_identity.email:
            extra["caller_email"] = caller_identity.email
        if caller_identity.roles:
            extra["caller_roles"] = caller_identity.roles
        if caller_identity.team:
            extra["team"] = caller_identity.team
        extra["identity_source"] = caller_identity.source
        # Expose user_id for completeness middleware
        request.state.walacor_user_id = caller_identity.user_id

    # ── Audit content classification ─────────────────────────────────────────
    # Extract structured question/context from the messages array.
    # OpenWebUI plugin classifies at source (preferred); gateway fallback for others.
    try:
        from gateway.middleware.audit_classifier import classify_request
        _body = body_dict if isinstance(body_dict, dict) else {}
        audit_class = classify_request(_body)
        extra["walacor_audit"] = audit_class
    except Exception:
        logger.debug("Audit classifier skipped", exc_info=True)

    call = dataclasses.replace(call, metadata={**call.metadata, **extra})

    # ── B.9: A/B model testing — rewrite model before adapter resolution ──────
    if settings.ab_tests_json:
        _ab_tests = _get_ab_tests()
        if _ab_tests and call.model_id:
            from gateway.routing.ab_test import resolve_ab_model
            _original_model = call.model_id
            _resolved_model, _test_name = resolve_ab_model(call.model_id, _ab_tests)
            if _test_name is not None:
                _ab_meta = {
                    "ab_variant": _test_name,
                    "ab_original_model": _original_model,
                    "ab_selected_model": _resolved_model,
                }
                call = dataclasses.replace(
                    call,
                    model_id=_resolved_model,
                    metadata={**call.metadata, **_ab_meta},
                )
                logger.info(
                    "A/B test '%s': model rewritten %s → %s",
                    _test_name, _original_model, _resolved_model,
                )

    # ── System task detection: skip audit for auto-generated requests ────────
    _req_type = extra.get("request_type", "user_message")
    if settings.skip_system_task_audit and isinstance(_req_type, str) and _req_type.startswith("system_task:"):
        request.state.skip_audit = True
        logger.debug("System task detected (%s) — will skip audit record", _req_type)

    ctx = get_pipeline_context()
    provider = adapter.get_provider_name()
    model = call.model_id or "unknown"
    provider_var.set(provider)
    model_id_var.set(call.model_id)
    request.state.walacor_provider = provider
    request.state.walacor_model_id = call.model_id

    # ── Skip-governance (transparent proxy) ──────────────────────────────────
    if ctx.skip_governance:
        return await _handle_skip_governance(request, adapter, call, ctx, settings, t0, provider, model)

    # ── Steps 1–2.7: governance pre-checks ───────────────────────────────────
    t_pre = time.perf_counter()
    pre = await _run_pre_checks(request, adapter, call, ctx, settings, is_audit_only, provider, model)
    timings: dict[str, float] = {**pre.timings, "pre_checks_ms": round((time.perf_counter() - t_pre) * 1000, 1)}
    if pre.error is not None:
        _record_status(pre.error.status_code)
        return pre.error

    assert pre.call is not None  # always set when error is None
    call = pre.call

    # ── B.4: Semantic cache check (non-streaming only) ───────────────────────
    # Cache hit returns the stored response immediately — no LLM call, no audit
    # record (correct: there was no actual inference, so nothing to audit).
    if ctx.semantic_cache is not None and not call.is_streaming and call.prompt_text:
        _cached = ctx.semantic_cache.get(call.model_id, call.prompt_text)
        if _cached is not None:
            try:
                cache_hits.labels(model=call.model_id or "unknown").inc()
            except Exception:
                logger.debug("Metric increment failed (cache_hits)", exc_info=True)
            logger.debug("Semantic cache HIT: model=%s", call.model_id)
            _set_disposition(request, "allowed")
            _inc_request(provider, model, "allowed")
            pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
            return Response(
                content=_cached.response_body,
                status_code=_cached.status_code,
                headers={"Content-Type": _cached.content_type, "X-Cache": "HIT"},
            )
        try:
            cache_misses.labels(model=call.model_id or "unknown").inc()
        except Exception:
            logger.debug("Metric increment failed (cache_misses)", exc_info=True)

    # Active tool strategy requires non-streaming: we need to intercept the
    # response, execute tools, and loop before returning anything to the client.
    if pre.tool_strategy == "active" and call.is_streaming:
        try:
            body = json.loads(call.raw_body)
            body["stream"] = False
            call = dataclasses.replace(
                call, is_streaming=False, raw_body=json.dumps_bytes(body)
            )
            logger.debug("Active tool strategy: overriding stream=False for %s/%s", provider, model)
        except Exception:
            logger.warning(
                "Active tool strategy: failed to override stream=False for %s/%s — "
                "tool loop will be skipped; proceeding on streaming path",
                provider, model, exc_info=True,
            )

    # ── Step 2.9: PII Sanitization (pre-forward, non-streaming only) ─────────
    # Strip high-risk PII from the prompt before it reaches the LLM.
    # The mapping is stored in call.metadata["_pii_mapping"] and used post-
    # response to restore original values (so the user sees their data back).
    # Streaming restoration is deferred (TODO: streaming restore support).
    if settings.pii_sanitization_enabled and call.prompt_text and not call.is_streaming:
        _sanitizer = _get_pii_sanitizer(settings)
        _san_result = _sanitizer.sanitize(call.prompt_text)
        if _san_result.pii_count > 0:
            call = dataclasses.replace(
                call,
                prompt_text=_san_result.sanitized_text,
                metadata={**call.metadata, "_pii_mapping": _san_result.mapping},
            )
            logger.info(
                "PII sanitized %d token(s) pre-forward provider=%s model=%s",
                _san_result.pii_count, provider, model,
            )

    # ── Adaptive concurrency gate ────────────────────────────────────────────
    _concurrency_acquired = False
    _concurrency_limiter: ConcurrencyLimiter | None = None
    if settings.adaptive_concurrency_enabled:
        limiter = _get_or_create_limiter(provider)
        _concurrency_limiter = limiter
        if not limiter.try_acquire():
            logger.warning(
                "Adaptive concurrency limit reached for provider=%s limit=%d inflight=%d",
                provider, limiter.limit, limiter.inflight,
            )
            _set_disposition(request, "error_overloaded")
            _inc_request(provider, model, "overloaded")
            _record_status(503)
            return JSONResponse(
                {"error": "Service overloaded", "retry_after": 1},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        _concurrency_acquired = True

    # ── Step 2.95: Responses API reasoning models require non-streaming ─────
    # OpenAI Responses API does not support SSE streaming. Force non-streaming
    # for reasoning models so the gateway can normalize the response format.
    if call.is_streaming and call.metadata.get("_responses_api"):
        try:
            body = json.loads(call.raw_body)
            body["stream"] = False
            call = dataclasses.replace(
                call, is_streaming=False, raw_body=json.dumps_bytes(body)
            )
            logger.debug("Responses API: overriding stream=False for %s", call.model_id)
        except Exception:
            pass

    # ── Step 3: Forward ───────────────────────────────────────────────────────
    if call.is_streaming:
        # For streaming, we do a quick non-streaming probe only if tools were
        # injected and the model might not support them.  Actually, the easier
        # path: strip tools from streaming requests and let the stream proceed;
        # tool-loop already forces non-streaming for active strategy.  If we
        # reach here with is_streaming=True and tools injected, it means the
        # active strategy override failed — just strip tools to be safe.
        if pre.tool_strategy == "active":
            call = _strip_tools_from_call(call)
        _set_disposition(request, "allowed")
        buf: list[bytes] = []
        governance_meta: dict = {
            "attestation_id": pre.att_id, "policy_result": pre.pr,
            "model_id": call.model_id,
            "budget_remaining": pre.budget_remaining,
            "budget_percent": _compute_budget_percent(pre.budget_remaining, settings),
        }
        task = BackgroundTask(
            _after_stream_record, buf, call, adapter,
            pre.att_id, pre.pv, pre.pr, pre.audit_metadata,
            pre.budget_estimated, t0, governance_meta, request,
        )
        resp, _ = await stream_with_tee(
            adapter, call, request, buffer=buf, background_task=task,
            governance_meta=governance_meta,
        )
        # Release concurrency slot using time-to-first-byte as RTT signal
        if _concurrency_acquired and _concurrency_limiter is not None:
            _concurrency_limiter.release(time.perf_counter() - t0)
        pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
        outcome = "audit_only_allowed" if (is_audit_only and pre.whb) else "allowed"
        _inc_request(provider, model, outcome)
        _record_status(200, source="provider")
        return resp

    t_fwd = time.perf_counter()
    # ── B.7: Parallel input analysis ─────────────────────────────────────────
    # When content_analysis_parallel=True, run content analyzers on the *input*
    # concurrently with the LLM call.  This reduces total latency when the
    # analyzer (PII, toxicity) takes non-trivial time, because both proceed in
    # parallel rather than sequentially.
    #
    # IMPORTANT: input blocking is not enforced in parallel mode — the request
    # has already been forwarded.  Input analysis results are stored in the
    # audit record as metadata only (informational, not enforcement).
    # Output analysis (evaluate_post_inference) still enforces BLOCK decisions.
    _input_analysis: list[dict] = []
    if settings.content_analysis_parallel and ctx.content_analyzers and call.prompt_text:
        logger.debug(
            "B.7 parallel input analysis: starting alongside forward provider=%s model=%s",
            provider, model,
        )
        (http_response, model_response, _used_fallback), _input_analysis = await asyncio.gather(
            _forward_with_resilience(adapter, call, request),
            _run_input_analysis_async(call, ctx),
        )
        if _input_analysis:
            logger.debug(
                "B.7 parallel input analysis: %d decision(s) for provider=%s model=%s",
                len(_input_analysis), provider, model,
            )
    else:
        http_response, model_response, _used_fallback = await _forward_with_resilience(adapter, call, request)

    # ── Tool-unsupported retry: if the model rejects tools, strip and retry ──
    if _is_tool_unsupported_error(http_response.status_code, bytes(http_response.body)):
        if ctx.capability_registry:
            ctx.capability_registry.record(call.model_id, supports_tools=False, provider=provider)
        else:
            _record_model_capability(call.model_id, supports_tools=False)
        logger.info(
            "Model %s does not support tools — retrying without tool definitions",
            call.model_id,
        )
        call = _strip_tools_from_call(call)
        pre = dataclasses.replace(pre, tool_strategy="none")
        http_response, model_response, _used_fallback = await _forward_with_resilience(adapter, call, request)
    elif pre.tool_strategy == "active" and http_response.status_code < 400:
        # Model accepted tools successfully — cache this
        if ctx.capability_registry:
            ctx.capability_registry.record(call.model_id, supports_tools=True, provider=provider)
        else:
            _record_model_capability(call.model_id, supports_tools=True)

    model_response = await _maybe_fetch_ollama_hash(adapter, call, model_response, ctx)
    fwd_rtt = time.perf_counter() - t_fwd
    timings["forward_ms"] = round(fwd_rtt * 1000, 1)
    forward_duration_by_model.labels(model=call.model_id or "unknown").observe(fwd_rtt)
    latency_detector.record(provider, fwd_rtt)
    # Adaptive timeout: record observed latency for this model
    if ctx.capability_registry and call.model_id and http_response.status_code < 500:
        ctx.capability_registry.record_latency(call.model_id, fwd_rtt)

    # B.7: Merge input analysis results into audit metadata (informational; not enforcement)
    if _input_analysis:
        pre = dataclasses.replace(
            pre,
            audit_metadata={**pre.audit_metadata, "input_analysis": _input_analysis},
        )

    # Release adaptive concurrency slot with observed forward RTT
    if _concurrency_acquired and _concurrency_limiter is not None:
        _concurrency_limiter.release(fwd_rtt)

    if http_response.status_code >= 500:
        _set_disposition(request, "error_provider")
        _inc_request(provider, model, "error")
        _record_status(http_response.status_code, source="provider")
        await _record_token_usage(
            model_response, settings.gateway_tenant_id, provider,
            call.metadata.get("user"), estimated=pre.budget_estimated,
        )
        await _build_and_write_record(request, call, model_response, _AuditParams(
            attestation_id=pre.att_id,
            policy_version=pre.pv, policy_result=pre.pr,
            budget_remaining=pre.budget_remaining, audit_metadata=pre.audit_metadata,
            tool_interactions=[], tool_strategy=pre.tool_strategy, tool_iterations=0,
            rp_version=0, rp_result="skipped", rp_decisions=[], provider=provider,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        ), ctx, settings)
        pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
        return http_response

    # ── Step 3.5: Tool strategy router ───────────────────────────────────────
    tool_result = await _route_tool_strategy(
        pre.tool_strategy, model_response, call, adapter, ctx, settings, request, provider
    )
    if tool_result.error is not None:
        _set_disposition(request, "error_provider")
        _inc_request(provider, model, "error")
        _record_status(500)
        await _record_token_usage(
            tool_result.model_response, settings.gateway_tenant_id, provider,
            call.metadata.get("user"), estimated=pre.budget_estimated,
        )
        await _build_and_write_record(request, tool_result.call, tool_result.model_response, _AuditParams(
            attestation_id=pre.att_id,
            policy_version=pre.pv, policy_result=pre.pr,
            budget_remaining=pre.budget_remaining, audit_metadata=pre.audit_metadata,
            tool_interactions=tool_result.interactions, tool_strategy=pre.tool_strategy,
            tool_iterations=tool_result.iterations,
            rp_version=0, rp_result="skipped", rp_decisions=[], provider=provider,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        ), ctx, settings)
        pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
        return tool_result.error

    call, model_response = tool_result.call, tool_result.model_response
    # If the active tool loop ran and produced a final HTTP response, use it instead of the
    # original http_response (which still has finish_reason=tool_calls from the first forward).
    if tool_result.http_response is not None:
        http_response = tool_result.http_response

    # ── Step 3.9: PII Restoration (post-response, replace=mode only) ─────────
    # Restore original PII values in the model response so the user receives
    # their data back.  Only runs when a mapping was stored pre-forward and
    # mode is "replace" (redact mode intentionally omits restoration).
    _pii_mapping = call.metadata.get("_pii_mapping")
    if (
        _pii_mapping
        and settings.pii_sanitization_enabled
        and settings.pii_sanitization_mode == "replace"
        and model_response.content
    ):
        from gateway.content.pii_sanitizer import get_default_sanitizer
        _restored_content = get_default_sanitizer().restore(model_response.content, _pii_mapping)
        if _restored_content != model_response.content:
            model_response = dataclasses.replace(model_response, content=_restored_content)
            logger.debug("PII restored %d placeholder(s) in model response", len(_pii_mapping))

    # ── Step 4: Post-inference policy (G4) ───────────────────────────────────
    t_rp = time.perf_counter()
    rp_version, rp_result, rp_decisions, whb, _, resp_err = await _run_response_policy(
        request, ctx, settings, is_audit_only, model_response, pre.audit_metadata, provider, model, t0
    )
    timings["content_analysis_ms"] = round((time.perf_counter() - t_rp) * 1000, 1)
    if resp_err is not None:
        _record_status(403)
        await _record_token_usage(
            model_response, settings.gateway_tenant_id, provider,
            call.metadata.get("user"), estimated=pre.budget_estimated,
        )
        await _build_and_write_record(request, call, model_response, _AuditParams(
            attestation_id=pre.att_id,
            policy_version=pre.pv, policy_result=pre.pr,
            budget_remaining=pre.budget_remaining, audit_metadata=pre.audit_metadata,
            tool_interactions=tool_result.interactions, tool_strategy=pre.tool_strategy,
            tool_iterations=tool_result.iterations,
            rp_version=rp_version, rp_result=rp_result, rp_decisions=rp_decisions, provider=provider,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        ), ctx, settings)
        return resp_err

    # ── Step 5: Token usage ───────────────────────────────────────────────────
    await _record_token_usage(
        model_response, settings.gateway_tenant_id, provider,
        call.metadata.get("user"), estimated=pre.budget_estimated,
    )

    # ── B.4: Semantic cache store (non-streaming, status=200 only) ───────────
    # Cache successful responses so identical future prompts get instant replies.
    if (
        ctx.semantic_cache is not None
        and not call.is_streaming
        and http_response.status_code == 200
        and call.prompt_text
    ):
        try:
            ctx.semantic_cache.put(
                call.model_id,
                call.prompt_text,
                bytes(http_response.body),
                status_code=http_response.status_code,
                content_type=http_response.headers.get("content-type", "application/json"),
            )
            logger.debug("Semantic cache STORE: model=%s size=%d", call.model_id, ctx.semantic_cache.size)
        except Exception:
            logger.debug("Semantic cache put failed (non-fatal)", exc_info=True)

    # ── Steps 6-8: Hash, session chain, write ────────────────────────────────
    t_write = time.perf_counter()
    timings["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    # Compute variant_id for A/B tracking when model groups have >1 endpoint
    _variant_id = None
    if ctx.load_balancer and hasattr(adapter, '_base_url'):
        for _mg in ctx.load_balancer._groups:
            from fnmatch import fnmatch as _fn
            if _fn(call.model_id.lower(), _mg.pattern.lower()) and len(_mg.endpoints) > 1:
                _variant_id = f"{call.model_id}@{adapter._base_url}"
                break

    audit_params = _AuditParams(
        attestation_id=pre.att_id,
        policy_version=pre.pv, policy_result=pre.pr,
        budget_remaining=pre.budget_remaining, audit_metadata=pre.audit_metadata,
        tool_interactions=tool_result.interactions, tool_strategy=pre.tool_strategy,
        tool_iterations=tool_result.iterations,
        rp_version=rp_version, rp_result=rp_result, rp_decisions=rp_decisions, provider=provider,
        latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        timings=timings,
        variant_id=_variant_id,
    )
    await _build_and_write_record(request, call, model_response, audit_params, ctx, settings)
    timings["write_ms"] = round((time.perf_counter() - t_write) * 1000, 1)

    _set_disposition(request, "allowed")
    pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
    outcome = "audit_only_allowed" if (is_audit_only and whb) else "allowed"
    _inc_request(provider, model, outcome)

    # Phase 23: governance response headers (non-streaming)
    _content_verdict = _summarize_content_analysis(rp_decisions)
    _add_governance_headers(
        http_response,
        execution_id=getattr(request.state, "walacor_execution_id", None),
        attestation_id=pre.att_id,
        chain_seq=getattr(request.state, "walacor_chain_seq", None),
        policy_result=pre.pr,
        content_analysis=_content_verdict,
        budget_remaining=pre.budget_remaining,
        budget_percent=_compute_budget_percent(pre.budget_remaining, settings),
        model_id=call.model_id,
    )
    _add_rate_limit_headers(http_response, request)
    _record_status(http_response.status_code, source="provider")
    return http_response
