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
from pathlib import Path
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
                logger.warning(
                    "Fallback retry failed for model=%s after %s error",
                    call.model_id, error_class, exc_info=True,
                )
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

def _set_disposition(request: Request, value: str, reason: str | None = None) -> None:
    """Set disposition on both ContextVar and request.state (crosses BaseHTTPMiddleware boundary).

    When provided, `reason` is a short human-readable explanation of why this disposition
    was set (e.g. the failing policy rule, the provider error message, the parse error).
    It is clamped to 500 chars and surfaced in the lineage dashboard Attempts popover.
    """
    disposition_var.set(value)
    request.state.walacor_disposition = value
    if reason:
        request.state.walacor_reason = str(reason)[:500]


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
            beta_headers=settings.provider_anthropic_beta_headers,
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
        return AnthropicAdapter(
            base_url=settings.provider_anthropic_url,
            api_key=settings.provider_anthropic_key,
            prompt_caching=settings.prompt_caching_enabled,
            beta_headers=settings.provider_anthropic_beta_headers,
        )
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


# ── Tool executor (extracted to gateway.pipeline.tool_executor) ──────────────
from gateway.pipeline.tool_executor import (
    prepare_tools, execute_tools, ToolPrepResult, ToolExecResult,
    build_tool_audit_metadata, write_tool_events, emit_tool_metrics,
    strip_tools_from_call, filter_tools_for_key, is_tool_unsupported_error,
)
_build_tool_audit_metadata = build_tool_audit_metadata
_write_tool_events = write_tool_events
_emit_tool_metrics = emit_tool_metrics
_filter_tools_for_key = filter_tools_for_key
_is_tool_unsupported_error = is_tool_unsupported_error


# ── Session chain + record write helpers ─────────────────────────────────────

from contextlib import asynccontextmanager


@asynccontextmanager
async def _session_chain_lock(ctx, session_id: str | None):
    """Serialize the (reserve-seq → write-record → update-tracker) span
    per session so concurrent same-session requests can never read a
    stale `last_record_id` and produce a broken ID-pointer chain.

    No-ops when session tracking is disabled or session_id is missing.
    Works for both in-memory and Redis trackers — each exposes
    `session_lock(session_id)` returning an `asyncio.Lock`. In the
    Redis case the lock only serializes within THIS worker; multi-
    replica deployments still need sticky-session LB affinity.
    """
    tracker = getattr(ctx, "session_chain", None) if ctx else None
    if session_id and tracker is not None and hasattr(tracker, "session_lock"):
        async with tracker.session_lock(session_id):
            yield
    else:
        yield


async def _apply_session_chain(record, session_id: str | None, ctx, settings) -> bool:
    """Attach session chain fields to record. Returns True on success, False if skipped.

    Gateway no longer computes SHA3-512 record_hash; Walacor backend hashes on
    ingest and returns DH as the tamper-evident checkpoint. Chain integrity is
    now maintained via UUIDv7 record_id / previous_record_id pointers.

    On failure, skips chain fields so the execution record is still written.
    """
    if not (session_id and ctx.session_chain and settings.session_chain_enabled):
        return False
    try:
        chain_vals = await ctx.session_chain.next_chain_values(session_id)
    except Exception:
        logger.error(
            "Session chain next_chain_values failed — skipping chain fields: session_id=%s",
            session_id, exc_info=True,
        )
        return False
    seq_num = chain_vals.sequence_number
    record["sequence_number"] = seq_num
    record["previous_record_id"] = chain_vals.previous_record_id

    # Ed25519 signing over canonical ID string (fail-open)
    try:
        from gateway.crypto.signing import sign_canonical

        signature = sign_canonical(
            record_id=record.get("record_id"),
            previous_record_id=record.get("previous_record_id"),
            sequence_number=seq_num,
            execution_id=record["execution_id"],
            timestamp=record["timestamp"],
        )
        if signature:
            record["record_signature"] = signature
    except Exception:
        pass  # fail-open: never block record writing

    return True


async def _store_execution(record, request: Request, ctx) -> None:
    """Validate schema then write execution record via storage router."""
    # Schema validation ALWAYS runs — this is the last gate before permanent storage.
    # If SchemaIntelligence is unavailable, use standalone validation.
    try:
        _si = getattr(ctx, "schema_intelligence", None)
        if _si:
            record, _val_report = _si.validate_execution(record)
        else:
            from gateway.classifier.unified import validate_execution as _standalone_validate
            record, _val_report = _standalone_validate(record)
        if _val_report.issues:
            logger.info("Execution schema: %d issues fixed before write", len(_val_report.issues))
    except Exception as _val_err:
        logger.error("Schema validation failed (writing anyway): %s", _val_err)
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
    prebuilt_model_response: ModelResponse | None = None,
) -> None:
    """Background task: after stream ends, evaluate response, build chain, write record (no hashing — Walcor hashes).

    budget_estimated: tokens reserved in check_and_reserve; passed to record_usage so the
    budget tracker can apply only the actual-vs-estimated delta (Finding 4).

    prebuilt_model_response: Phase 24.4 — if set, the streaming response was
    synthesized from an already-parsed non-streaming forward. Use it directly
    instead of re-parsing the buffer (which may be empty in that case).
    """
    ctx = get_pipeline_context()
    settings = get_settings()
    if not ctx.wal_writer and not ctx.walacor_client:
        return
    _exec_id = "unknown"
    try:
        if prebuilt_model_response is not None:
            model_response = prebuilt_model_response
        else:
            model_response = adapter.parse_streamed_response(buffer)

        if isinstance(adapter, OllamaAdapter) and call.model_id:
            model_hash = await adapter.fetch_model_hash(call.model_id, ctx.http_client)
            if model_hash:
                model_response = dataclasses.replace(model_response, model_hash=model_hash)

        # Use unified SchemaIntelligence for streaming response normalization
        try:
            _si = getattr(ctx, "schema_intelligence", None)
            if _si:
                model_response, _norm_report = _si.process_response(model_response, adapter.get_provider_name())
                if _norm_report.changes:
                    logger.debug("Stream normalization: %s", "; ".join(_norm_report.changes))
        except Exception as _norm_err:
            logger.error("Stream normalization failed (non-fatal): %s", _norm_err)
        else:
            from gateway.pipeline.normalizer import normalize_model_response
            model_response = normalize_model_response(model_response, adapter.get_provider_name())

        await _record_token_usage(
            model_response, settings.gateway_tenant_id,
            adapter.get_provider_name(), call.metadata.get("user"),
            estimated=budget_estimated,
        )

        rp_version, rp_result, rp_decisions = await _eval_post_stream_policy(ctx, settings, model_response)

        # Phase 14: capture passive tool interactions from streamed response.
        # IMPORTANT: capture BEFORE normalization (below) which may replace model_response
        # and clear tool_interactions.
        stream_tool_meta: dict[str, Any] = {}
        _raw_tool_interactions = model_response.tool_interactions
        _raw_thinking = model_response.thinking_content
        _raw_provider_id = model_response.provider_request_id
        _raw_usage = model_response.usage
        if _raw_tool_interactions:
            stream_tool_meta = _build_tool_audit_metadata(_raw_tool_interactions, "passive", 0)
            _emit_tool_metrics(_raw_tool_interactions, adapter.get_provider_name(), "provider")

        session_id = call.metadata.get("session_id")

        # Phase 24.5: full-fidelity metadata for streaming path.
        # Uses _raw_* values captured BEFORE normalization (which may replace model_response).
        _stream_meta: dict = {
            **call.metadata, **audit_metadata, **stream_tool_meta,
            "response_policy_version": rp_version,
            "response_policy_result": rp_result,
            "analyzer_decisions": rp_decisions,
            "enforcement_mode": settings.enforcement_mode,
            "thinking_content": _raw_thinking,
            "provider_response_id": _raw_provider_id,
        }
        if _raw_tool_interactions:
            _stream_meta["tool_events_detail"] = [
                {
                    "tool_id": t.tool_id,
                    "tool_type": t.tool_type,
                    "tool_name": t.tool_name,
                    "input_data": t.input_data,
                    "output_data": t.output_data,
                    "sources": t.sources,
                    "metadata": t.metadata,
                }
                for t in _raw_tool_interactions
            ]
        if _raw_usage:
            _stream_meta["token_usage"] = _raw_usage

        record = build_execution_record(
            call=call, model_response=model_response, attestation_id=attestation_id,
            policy_version=policy_version, policy_result=policy_result,
            tenant_id=settings.gateway_tenant_id, gateway_id=settings.gateway_id,
            user=call.metadata.get("user"), session_id=session_id,
            metadata=_stream_meta,
            model_id=call.model_id, provider=adapter.get_provider_name(),
            latency_ms=round((time.perf_counter() - pipeline_start) * 1000, 1) if pipeline_start else None,
            file_metadata=getattr(request.state, "file_metadata", None) if request else None,
        )
        _exec_id = record.get("execution_id", "unknown")

        # Serialize the full chain critical section per session so
        # concurrent same-session requests can't race and break the
        # ID-pointer chain linkage. The `_session_chain_lock` no-ops when
        # session tracking is off.
        async with _session_chain_lock(ctx, session_id):
            record_hash_val = await _apply_session_chain(record, session_id, ctx, settings)
            if ctx.storage:
                result = await ctx.storage.write_execution(record)
                if result.succeeded:
                    execution_id_var.set(record["execution_id"])
            await _write_tool_events(model_response.tool_interactions or [], record["execution_id"], call, "passive", ctx, settings)
            if session_id and ctx.session_chain and record_hash_val:
                await ctx.session_chain.update(session_id, record["sequence_number"], record_id=record.get("record_id"))
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
        if ctx.sync_client is None and not getattr(settings, "strict_model_allowlist", False):
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
        _set_disposition(
            request,
            "denied_attestation",
            reason=f"model {model!r} provider={provider} not attested or revoked in control plane",
        )
        _inc_request(provider, model, "blocked_stale" if err.status_code == 503 else "blocked_attestation")
        _inject_caller_role(att_ctx, request)
        # When the control plane is present (strict or permissive-but-revoked),
        # the stale-cache error text is misleading. Replace with a clear denial.
        if ctx.control_store is not None:
            from starlette.responses import JSONResponse as _JSON
            err = _JSON(
                {"error": {
                    "message": f"model {model!r} is not in the gateway allowlist (provider={provider}). "
                               "An admin must attest it via Control → Discover Models.",
                    "type": "model_not_attested",
                    "code": "model_not_attested",
                }},
                status_code=403,
            )
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
            _set_disposition(request, "denied_by_opa", reason=f"OPA: {opa_reason}")
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
        _, pv, pr, err, fail_reason = evaluate_pre_inference(per_key_cache, call, att_id, att_ctx)
        if err is not None:
            if is_audit_only:
                logger.warning("AUDIT_ONLY: Would have blocked (per-key policy) provider=%s model=%s", provider, model)
                return pv, pr, True, reason or "policy", None
            _set_disposition(request, "denied_policy", reason=fail_reason or "per-key policy blocked")
            _inc_request(provider, model, "blocked_stale" if err.status_code == 503 else "blocked_policy")
            return pv, pr, whb, reason, err
        return pv, pr, whb, reason, None

    # Builtin policy engine path (global policies)
    _, pv, pr, err, fail_reason = evaluate_pre_inference(ctx.policy_cache, call, att_id, att_ctx)
    if err is not None:
        if is_audit_only:
            logger.warning("AUDIT_ONLY: Would have blocked (pre-policy) provider=%s model=%s", provider, model)
            return pv, pr, True, reason or "policy", None
        _set_disposition(request, "denied_policy", reason=fail_reason or "policy blocked")
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
        _set_disposition(
            request,
            "denied_wal_full",
            reason=f"WAL back-pressure: pending={pending} disk_bytes={disk_bytes} (high_water={settings.wal_high_water_mark})",
        )
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
            logger.debug("budget_failopen_total metric increment failed", exc_info=True)
        return None, 0, whb, reason, None  # fail-open: allow request
    budget_rem: int | None = remaining if remaining >= 0 else None
    if not allowed:
        if is_audit_only:
            logger.warning("AUDIT_ONLY: Would have blocked (budget) provider=%s model=%s", provider, model)
            return budget_rem, 0, True, reason or "budget", None
        rem_str = str(budget_rem) if budget_rem is not None else "unknown"
        _set_disposition(
            request,
            "denied_budget",
            reason=f"tenant={settings.gateway_tenant_id} reserve={estimated} tokens failed; remaining={rem_str}",
        )
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
        _set_disposition(
            request,
            "denied_rate_limit",
            reason=f"rate_limit hit: user={user} model={call.model_id} limit={settings.rate_limit_rpm}/min",
        )
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
        _set_disposition(request, "error_config", reason="attestation cache not configured")
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
        _set_disposition(request, "error_config", reason="policy cache not configured")
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

    tool_prep = await prepare_tools(call, request, ctx, settings)
    call = tool_prep.call
    tool_strategy = tool_prep.strategy

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


# _route_tool_strategy removed — replaced by tool_executor.execute_tools()


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

    blocking_analyzers = ",".join(sorted({d.get("analyzer", "unknown") for d in (decisions or []) if d.get("action") == "block"})) or "unknown"
    _set_disposition(
        request,
        "denied_response_policy",
        reason=f"response policy blocked by analyzers: {blocking_analyzers} (result={rp_result})",
    )
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

def _emit_harvester_signals(record: dict, session_id: str | None) -> None:
    """Fan out HarvesterSignals for each ONNX model that ran in this request.

    Reads from the finalized execution record (not request.state) so the
    signal reflects exactly what was audited. One signal per model — the
    runner filters by `target_model` on its side. Non-blocking: uses
    `submit()` which returns False on queue overflow, never raises.

    The `request_id` on the signal matches the value used by the verdict
    recording sites (`request_id_var.get()`), so harvesters that
    `UPDATE onnx_verdicts WHERE request_id=?` find the row we enqueued
    the signal for.
    """
    from gateway.intelligence.harvesters import HarvesterSignal

    ctx = get_pipeline_context()
    runner = ctx.harvester_runner
    if runner is None:
        return

    rid = request_id_var.get() or None
    meta = record.get("metadata") or {}
    prompt = record.get("prompt_text") or ""
    # Task 17: response_content travels in context so text-based harvesters
    # (safety, intent teacher) can store it as training_text on the
    # verdict row. Absent / empty responses are still passed — the
    # receiving harvester decides whether to store them.
    response = record.get("response_content") or ""
    common_context = {
        "session_id": session_id,
        "prompt": prompt,
        "response": response,
    }

    # Intent — `_intent` is populated in `classify_intent`'s post-process
    # path (orchestrator wiring above SchemaIntelligence). When the request
    # short-circuited before intent inference, skip the signal.
    intent_label = meta.get("_intent")
    if isinstance(intent_label, str) and intent_label:
        runner.submit(HarvesterSignal(
            request_id=rid,
            model_name="intent",
            prediction=intent_label,
            response_payload=meta,
            context=common_context,
        ))

    # SchemaMapper — `canonical` is present when `map_response` ran. The
    # prediction is a coarse label ("complete" when content was recovered,
    # "incomplete" otherwise) matching what SchemaMapper records to the
    # verdict log.
    canonical = meta.get("canonical")
    if isinstance(canonical, dict):
        runner.submit(HarvesterSignal(
            request_id=rid,
            model_name="schema_mapper",
            prediction="complete" if canonical.get("content_length", 0) > 0 else "incomplete",
            response_payload=meta,
            context=common_context,
        ))

    # Safety — look for a safety analyzer's decision in `analyzer_decisions`.
    # `truzenai.safety.v1` is the canonical analyzer_id for SafetyClassifier
    # (set on the class). Any decision with category != "safety" means it
    # flagged a specific category; prediction carries that label so the
    # harvester can diff against LlamaGuard.
    decisions = meta.get("analyzer_decisions") or []
    if isinstance(decisions, list):
        for d in decisions:
            if not isinstance(d, dict):
                continue
            if d.get("analyzer_id") != "truzenai.safety.v1":
                continue
            category = d.get("category") or "safe"
            runner.submit(HarvesterSignal(
                request_id=rid,
                model_name="safety",
                prediction=category,
                response_payload=meta,
                context=common_context,
            ))
            break


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

    # ── Phase 24.5: full-fidelity metadata capture ──────────────────────────
    # The metadata field is the COMPLETE audit record. ONNX models (SchemaMapper,
    # SafetyClassifier, IntentClassifier) and the adapter's parse_response produce
    # structured data — dump ALL of it so nothing is lost.
    full_metadata: dict = {
        **call.metadata, **params.audit_metadata, **tool_meta,
        "response_policy_version": params.rp_version,
        "response_policy_result": params.rp_result,
        "analyzer_decisions": params.rp_decisions,
        "token_usage": model_response.usage,
        "budget_remaining": params.budget_remaining,
        "enforcement_mode": settings.enforcement_mode,
        # Model response extras (thinking, provider ID)
        "thinking_content": model_response.thinking_content,
        "provider_response_id": model_response.provider_request_id,
    }

    # Full tool interactions with actual data, not just hashes.
    # Hashes are in tool_meta.tool_interactions (for tamper-proof linking);
    # full data is here for audit inspection without needing separate queries.
    if params.tool_interactions:
        full_metadata["tool_events_detail"] = [
            {
                "tool_id": t.tool_id,
                "tool_type": t.tool_type,
                "tool_name": t.tool_name,
                "input_data": t.input_data,
                "output_data": t.output_data,
                "sources": t.sources,
                "metadata": t.metadata,
            }
            for t in params.tool_interactions
        ]

    # SchemaMapper canonical output (ONNX-extracted structured response).
    # This is the provider-agnostic view: same shape regardless of whether
    # the upstream was OpenAI, Anthropic, Ollama, Gemini, or anything else.
    _canonical = getattr(request.state, "_canonical_response", None)
    if _canonical:
        _can_dict: dict = {
            "content_length": len(_canonical.content) if _canonical.content else 0,
            "thinking_content_length": len(_canonical.thinking_content) if _canonical.thinking_content else 0,
            "finish_reason": _canonical.finish_reason,
            "response_id": _canonical.response_id,
            "model": _canonical.model,
            "mapping_confidence": round(_canonical.mapping.confidence, 3),
        }
        if _canonical.usage:
            _can_dict["usage"] = {
                "prompt_tokens": _canonical.usage.prompt_tokens,
                "completion_tokens": _canonical.usage.completion_tokens,
                "total_tokens": _canonical.usage.total_tokens,
                "reasoning_tokens": _canonical.usage.reasoning_tokens,
                "cached_tokens": _canonical.usage.cached_tokens,
                "cache_creation_tokens": _canonical.usage.cache_creation_tokens,
                "cost_usd": _canonical.usage.cost_usd,
            }
        if _canonical.tool_calls:
            _can_dict["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments, "type": tc.type}
                for tc in _canonical.tool_calls
            ]
        if _canonical.citations:
            _can_dict["citations"] = [
                {"url": c.url, "title": c.title, "snippet": c.snippet}
                for c in _canonical.citations
            ]
        if _canonical.timing:
            _can_dict["timing"] = {
                "total_ms": _canonical.timing.total_ms,
                "prompt_ms": _canonical.timing.prompt_ms,
                "completion_ms": _canonical.timing.completion_ms,
                "queue_ms": _canonical.timing.queue_ms,
            }
        if _canonical.overflow:
            _can_dict["overflow_keys"] = list(_canonical.overflow.keys())[:30]
        full_metadata["canonical"] = _can_dict

    record = build_execution_record(
        call=call, model_response=model_response, attestation_id=params.attestation_id,
        policy_version=params.policy_version,
        policy_result=params.policy_result,
        tenant_id=settings.gateway_tenant_id, gateway_id=settings.gateway_id,
        user=call.metadata.get("user"), session_id=session_id,
        metadata=full_metadata,
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

    # Serialize chain reserve → record write → tracker update per
    # session so concurrent same-session requests can't race. The
    # consistency/anomaly/overflow steps run inside the lock too
    # because they only mutate `record["metadata"]` (fast, local),
    # never another session's state.
    async with _session_chain_lock(ctx, session_id):
        t_chain = time.perf_counter()
        record_hash_val = await _apply_session_chain(record, session_id, ctx, settings)
        if params.timings is not None:
            params.timings["chain_ms"] = round((time.perf_counter() - t_chain) * 1000, 1)

        # ── Consistency check (inline, < 1ms) ───────────────────────────
        _consistency_tracker = getattr(ctx, "consistency_tracker", None)
        if _consistency_tracker:
            try:
                _con_result = _consistency_tracker.check(
                    prompt=record.get("prompt_text", ""),
                    response=record.get("response_content", ""),
                    model_id=record.get("model_id", ""),
                    execution_id=record.get("execution_id", ""),
                    session_id=session_id or "",
                    user=record.get("user", ""),
                )
                if _con_result:
                    meta = record.get("metadata") or {}
                    meta["consistency_check"] = {
                        "compared_with": _con_result.execution_id_a[:12],
                        "prompt_similarity": _con_result.prompt_similarity,
                        "response_similarity": _con_result.response_similarity,
                        "consistent": _con_result.consistent,
                    }
                    if not _con_result.consistent:
                        meta.setdefault("anomalies", []).append("consistency_flag")
                    record["metadata"] = meta
            except Exception as _ct_err:
                logger.debug("Consistency check failed (non-fatal): %s", _ct_err)

        # ── Anomaly detection (inline, < 2ms) ────────────────────────────
        _anomaly_detector = getattr(ctx, "anomaly_detector", None)
        if _anomaly_detector:
            try:
                _anomaly_report = _anomaly_detector.detect(record)
                if _anomaly_report.to_list():
                    meta = record.get("metadata") or {}
                    meta["anomalies"] = _anomaly_report.to_list()
                    record["metadata"] = meta
            except Exception as _ad_err:
                logger.debug("Anomaly detection failed (non-fatal): %s", _ad_err)

        # ── Self-healing overflow (capture unknown response fields) ──────
        _schema_mapper = getattr(ctx, "schema_mapper", None)
        _field_registry = getattr(ctx, "field_registry", None)
        if _schema_mapper and _field_registry:
            try:
                _canonical = getattr(request.state, "_canonical_response", None)
                if _canonical and _canonical.overflow:
                    from gateway.schema.overflow import build_overflow_envelope
                    _overflow_env = build_overflow_envelope(
                        _canonical.overflow,
                        provider=record.get("provider", "unknown"),
                        registry=_field_registry,
                    )
                    if _overflow_env:
                        meta = record.get("metadata") or {}
                        meta["_overflow"] = _overflow_env
                        record["metadata"] = meta
            except Exception as _of_err:
                logger.debug("Overflow capture failed (non-fatal): %s", _of_err)

        await _store_execution(record, request, ctx)
        # Expose governance metadata for response headers (Phase 23)
        request.state.walacor_chain_seq = record.get("sequence_number")
        await _write_tool_events(params.tool_interactions, record["execution_id"], call, params.tool_strategy, ctx, settings)
        if session_id and ctx.session_chain and record_hash_val:
            try:
                await ctx.session_chain.update(session_id, record["sequence_number"], record_id=record.get("record_id"))
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

    # ── Background LLM intelligence (fire-and-forget, zero latency) ──
    _intel_worker = getattr(ctx, "intelligence_worker", None)
    if _intel_worker:
        try:
            from gateway.intelligence.worker import IntelligenceJob
            _meta = record.get("metadata") or {}
            _audit = _meta.get("walacor_audit") or {}
            job = IntelligenceJob(
                execution_id=record["execution_id"],
                prompt_text=record.get("prompt_text") or "",
                response_content=record.get("response_content") or "",
                model_id=record.get("model_id") or "",
                session_id=session_id or "",
                intent=_meta.get("_intent", "normal"),
                intent_confidence=_meta.get("_intent_confidence", 1.0),
                conversation_turns=_audit.get("conversation_turns", 1),
            )
            await _intel_worker.enqueue(job)
        except Exception:
            logger.debug("Intelligence enqueue failed (non-fatal)", exc_info=True)

    # ── harvester dispatch (fire-and-forget) ──────
    # Emits one HarvesterSignal per ONNX model that participated in the
    # request so per-model harvesters (Tasks 14-16) can back-write
    # divergence labels onto the matching verdict row. Guarded in its
    # own try/except because this runs AFTER the WAL write — the user
    # response is already out, but the orchestrator task is still on the
    # return path and must not raise.
    if getattr(ctx, "harvester_runner", None) is not None:
        try:
            _emit_harvester_signals(record, session_id)
        except Exception:
            logger.debug("harvester dispatch failed (non-fatal)", exc_info=True)


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
        _set_disposition(
            request,
            "error_method_not_allowed",
            reason=f"only POST accepted; got {request.method} on {request.url.path}",
        )
        _inc_request("unknown", "unknown", "error")
        resp = JSONResponse({"error": "Method not allowed"}, status_code=405)
        _record_status(405)
        return resp

    model_hint = await _peek_model_id(request) if get_settings().model_routes else ""
    adapter = _resolve_adapter(request.url.path, model_hint)
    if not adapter:
        _set_disposition(
            request,
            "error_no_adapter",
            reason=f"no adapter registered for path={request.url.path} model_hint={model_hint or '?'}",
        )
        _inc_request("unknown", "unknown", "error")
        resp = JSONResponse({"error": "No adapter for this path"}, status_code=404)
        _record_status(404)
        return resp

    try:
        call = await adapter.parse_request(request)
    except Exception as e:
        logger.warning("parse_request failed: %s", e)
        _set_disposition(request, "error_parse", reason=f"adapter parse_request failed: {e}")
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
    # `body_dict` must be bound in every branch — downstream code (lines
    # below) reads it to pull OpenWebUI chat_id / message_id out of the
    # body's `metadata` object. Start from the cached-parsed-body on
    # request.state so we don't re-parse unless necessary.
    body_dict = getattr(request.state, "_parsed_body", None)
    if body_dict is None:
        try:
            body_dict = json.loads(call.raw_body) if call.raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            body_dict = {}
    if _meta_rt:
        extra["request_type"] = _meta_rt
    elif _rc:
        extra["request_type"] = _rc.classify(
            call.prompt_text or "", dict(request.headers), body_dict)
    else:
        extra["request_type"] = _classify_request_type(call.prompt_text or "")
    # Propagate OpenWebUI message/chat IDs for per-message audit correlation
    msg_id = request.headers.get("x-openwebui-message-id")
    if msg_id:
        extra["message_id"] = msg_id
    # Also check body metadata for chat_id/message_id (from Walacor filter plugin)
    _body_meta_ids = (body_dict if isinstance(body_dict, dict) else {}).get("metadata")
    if isinstance(_body_meta_ids, dict):
        if not msg_id and _body_meta_ids.get("message_id"):
            extra["message_id"] = _body_meta_ids["message_id"]
        if _body_meta_ids.get("chat_id"):
            extra["chat_id"] = _body_meta_ids["chat_id"]
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
            logger.debug("Identity resolution fallback failed (non-fatal)", exc_info=True)
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

    # ── File/attachment extraction ─────────────────────────────────────────
    # Extract images from base64 content blocks + OpenWebUI file metadata.
    # Stores metadata (hash, mimetype, size) in request.state.file_metadata
    # so build_execution_record picks it up for the audit trail.
    _file_metadata: list[dict] = []
    _body = body_dict if isinstance(body_dict, dict) else {}
    try:
        from gateway.middleware.attachment_tracker import (
            extract_images_from_messages,
            extract_openwebui_files,
        )
        _messages = _body.get("messages", [])
        if _messages:
            _extracted_images = extract_images_from_messages(_messages)
            for img in _extracted_images:
                _file_metadata.append({
                    "filename": f"image_{img['index']}.{(img.get('mimetype') or 'png').split('/')[-1]}",
                    "mimetype": img.get("mimetype", "image/png"),
                    "size_bytes": img.get("size_bytes", 0),
                    "hash_sha3_512": img.get("hash_sha3_512", ""),
                    "source": "inline_base64",
                })
            # Run OCR + PII scan on extracted images if analyzer is available
            if _extracted_images and ctx.image_ocr_analyzer is not None:
                try:
                    from gateway.content.image_ocr import evaluate_image_ocr
                    _blocked, _block_resp, _ocr_results = await evaluate_image_ocr(
                        ctx.image_ocr_analyzer, _extracted_images,
                    )
                    # Merge OCR results into file metadata
                    for ocr_r in _ocr_results:
                        idx = ocr_r.get("image_index", 0)
                        if idx < len(_file_metadata):
                            _file_metadata[idx].update({
                                k: v for k, v in ocr_r.items()
                                if k.startswith("ocr_")
                            })
                    if _blocked and _block_resp is not None:
                        request.state.file_metadata = _file_metadata
                        _set_disposition(
                            request,
                            "blocked_image_pii",
                            reason="OCR PII detected in attached image(s); see file_metadata for details",
                        )
                        _inc_request(provider, model, "blocked")
                        return _block_resp
                except Exception:
                    logger.debug("Image OCR analysis failed (non-blocking)", exc_info=True)

        _owui_files = extract_openwebui_files(_body)
        _file_metadata.extend(_owui_files)

        # Correlate with webhook notification cache if available
        if _file_metadata and ctx.attachment_cache is not None:
            for fm in _file_metadata:
                fh = fm.get("hash_sha3_512")
                if fh:
                    cached = ctx.attachment_cache.get(fh)
                    if cached:
                        fm["filename"] = cached.get("filename", fm.get("filename", ""))
                        fm["upload_source"] = cached.get("source", "")
    except Exception:
        logger.debug("File extraction skipped", exc_info=True)

    if _file_metadata:
        request.state.file_metadata = _file_metadata
        logger.info("Extracted %d file(s)/image(s) from request", len(_file_metadata))

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

    # ── Unified Schema Intelligence ─────────────────────────────────────
    # Single decision point: extract prompt, classify intent, and enrich
    # metadata. Replaces scattered IntentClassifier + audit_classifier
    # + _concat_messages with one coherent system.
    _body_meta = (body_dict if isinstance(body_dict, dict) else {}).get("metadata")

    ctx = get_pipeline_context()
    from gateway.classifier.unified import SchemaIntelligence, WEB_SEARCH, SYSTEM_TASK, REASONING, MCP_TOOLS, RAG, NORMAL
    _si = getattr(ctx, "schema_intelligence", None)
    if _si is None:
        _has_mcp = bool(ctx.tool_registry and ctx.tool_registry.get_tool_count() > 0
                        and settings.mcp_servers_json)
        # Task 12: when the registry is wired, the ONNX session is loaded on
        # first `classify_intent` call from `production/intent.onnx`. Without
        # a registry, fall back to the packaged model path (pre-Phase-25
        # behavior).
        _onnx_path = (
            None
            if ctx.model_registry is not None
            else str(Path(__file__).parent.parent / "classifier" / "model.onnx")
        )
        _si = SchemaIntelligence(
            onnx_model_path=_onnx_path,
            has_mcp_tools=_has_mcp,
            verdict_buffer=ctx.verdict_buffer,
            registry=ctx.model_registry,
            model_name="intent" if ctx.model_registry is not None else None,
            # Task 22: shadow runner is wired when the intelligence layer
            # and a model registry are both active. Nothing else needs
            # to be true — the runner itself no-ops when no active
            # candidate is registered.
            shadow_runner=ctx.shadow_runner,
        )
        ctx.schema_intelligence = _si

    # Build metadata context for intent classification
    _intent_metadata = {**call.metadata}
    if isinstance(_body_meta, dict):
        _intent_metadata["_body_metadata"] = _body_meta

    # Extract messages from request body for prompt extraction
    _body = body_dict if isinstance(body_dict, dict) else {}
    _messages = _body.get("messages", [])

    # process_request() does prompt extraction + intent classification in one pass
    # Wrapped in try/except — SI failure must NEVER block the request pipeline
    _si_enrichment: dict[str, Any] = {}
    try:
        _si_enrichment = _si.process_request(
            messages=_messages,
            metadata=_intent_metadata,
            model_id=call.model_id or "",
        )
    except Exception as _si_err:
        logger.error("SchemaIntelligence.process_request failed (non-fatal): %s", _si_err)

    # Override prompt_text with the extracted user question (THE KEY FIX)
    # _concat_messages in adapters joins ALL messages — we replace that
    # with just the actual question the user asked.
    _user_question = _si_enrichment.get("user_question", "")
    if _user_question:
        call = dataclasses.replace(call, prompt_text=_user_question)

    # Update walacor_audit with better prompt extraction data
    _existing_audit = extra.get("walacor_audit", {})
    _existing_audit["user_question"] = _si_enrichment.get("user_question", _existing_audit.get("user_question", ""))
    _existing_audit["conversation_context"] = _si_enrichment.get("conversation_context", "")
    _existing_audit["conversation_turns"] = _si_enrichment.get("conversation_turns", 0)
    _existing_audit["question_fingerprint"] = _si_enrichment.get("question_fingerprint", "")
    _existing_audit["extraction_method"] = _si_enrichment.get("extraction_method", "fallback")
    _existing_audit["has_rag_context"] = _si_enrichment.get("has_rag_context", _existing_audit.get("has_rag_context", False))
    _existing_audit["has_files"] = _si_enrichment.get("has_files", _existing_audit.get("has_files", False))
    extra["walacor_audit"] = _existing_audit

    # Merge intent + routing into call metadata
    _meta_updates: dict[str, Any] = {
        k: v for k, v in _si_enrichment.items()
        if k.startswith("_") or k in ("chat_id", "message_id")
    }
    if isinstance(_body_meta, dict):
        if _body_meta.get("chat_id"):
            _meta_updates["chat_id"] = _body_meta["chat_id"]
        if _body_meta.get("message_id"):
            _meta_updates["message_id"] = _body_meta["message_id"]

    call = dataclasses.replace(call, metadata={**call.metadata, **_meta_updates})

    logger.info(
        "Intent: %s (confidence=%.2f tier=%s reason=%s) model=%s prompt=%d chars",
        _si_enrichment.get("_intent", "unknown"),
        _si_enrichment.get("_intent_confidence", 0.0),
        _si_enrichment.get("_intent_tier", ""),
        _si_enrichment.get("_intent_reason", ""),
        call.model_id,
        len(_user_question),
    )
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

    # Active tool strategy: force non-streaming for the tool loop (need full
    # response to parse tool_calls). The final answer is re-streamed by
    # tool_executor if the original request was streaming.
    _original_streaming = call.is_streaming
    if pre.tool_strategy == "active" and call.is_streaming:
        from gateway.pipeline.tool_executor import _force_non_streaming
        call = _force_non_streaming(call)
        logger.debug("Active tool strategy: stream=False for tool loop %s/%s", provider, model)

    # ── Step 2.9: PII Sanitization (pre-forward, non-streaming only) ─────────
    # Strip high-risk PII from the prompt before it reaches the LLM.
    # The mapping is stored in call.metadata["_pii_mapping"] and used post-
    # response to restore original values (so the user sees their data back).
    # PII sanitization only runs on non-streaming requests; streaming restore is not yet supported.
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
            _set_disposition(
                request,
                "error_overloaded",
                reason=f"adaptive concurrency limit reached for provider={provider} (limit={limiter.limit}, inflight={limiter.inflight})",
            )
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
            logger.warning(
                "Responses API stream=False override failed for model=%s; "
                "streaming request will proceed with tools stripped",
                call.model_id, exc_info=True,
            )

    # ── Step 3: Forward ───────────────────────────────────────────────────────
    if call.is_streaming:
        # If we reach here streaming with active strategy, the override failed — strip tools
        if pre.tool_strategy == "active":
            call = strip_tools_from_call(call)
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

    # ── Tool-unsupported retry: handled by execute_tools() below ──

    model_response = await _maybe_fetch_ollama_hash(adapter, call, model_response, ctx)
    # Use unified SchemaIntelligence for response normalization
    _si = getattr(ctx, "schema_intelligence", None)
    _pre_norm_content = model_response.content
    try:
        if _si:
            model_response, _norm_report = _si.process_response(model_response, provider)
            if _norm_report.changes:
                logger.debug("Normalization: %s", "; ".join(_norm_report.changes))
        else:
            from gateway.pipeline.normalizer import normalize_model_response
            model_response = normalize_model_response(model_response, provider)
    except Exception as _norm_err:
        logger.error("Response normalization failed (non-fatal): %s", _norm_err)

    # ── SchemaMapper ML cross-validation ─────────────────────────────
    # Run the ONNX schema mapper on the raw response to cross-validate
    # adapter parsing and capture overflow fields the adapter missed.
    _schema_mapper = getattr(ctx, "schema_mapper", None)
    if _schema_mapper is None:
        try:
            from gateway.schema.mapper import SchemaMapper
            # Task 12: same registry-wiring pattern as main.py init site.
            _schema_mapper = SchemaMapper(
                registry=ctx.model_registry,
                model_name="schema_mapper" if ctx.model_registry is not None else None,
            )
            ctx.schema_mapper = _schema_mapper
        except Exception as _sm_init_err:
            logger.debug("SchemaMapper init failed (non-fatal): %s", _sm_init_err)
    if _schema_mapper and http_response.status_code < 400:
        try:
            _raw_resp = json.loads(http_response.body)
            _canonical = _schema_mapper.map_response(_raw_resp)

            # Cross-validate: if adapter found no usage but ML did, enrich
            _adapter_usage = model_response.usage or {}
            if not _adapter_usage.get("prompt_tokens") and _canonical.usage.prompt_tokens:
                _adapter_usage = dict(_adapter_usage)
                _adapter_usage["prompt_tokens"] = _canonical.usage.prompt_tokens
                _adapter_usage["completion_tokens"] = _canonical.usage.completion_tokens
                _adapter_usage["total_tokens"] = _canonical.usage.total_tokens
                model_response = dataclasses.replace(model_response, usage=_adapter_usage)
                logger.info("SchemaMapper enriched missing token usage from ML classification")

            # If adapter found no content but ML did (edge case)
            if not model_response.content and _canonical.content:
                model_response = dataclasses.replace(model_response, content=_canonical.content)
                logger.info("SchemaMapper recovered content from ML classification")

            # If adapter found no thinking_content but ML did
            if not model_response.thinking_content and _canonical.thinking_content:
                model_response = dataclasses.replace(model_response, thinking_content=_canonical.thinking_content)
                logger.info("SchemaMapper recovered thinking_content from ML classification")

            # Store canonical response for overflow capture in _build_and_write_record
            request.state._canonical_response = _canonical

            # Store ML mapping metadata for audit trail
            _mapping_meta = {
                "schema_mapper_confidence": round(_canonical.mapping.confidence, 3),
                "schema_mapper_mapped": len(_canonical.mapping.mapped_fields),
                "schema_mapper_unmapped": len(_canonical.mapping.unmapped_fields),
            }
            if _canonical.overflow:
                _mapping_meta["schema_mapper_overflow_keys"] = list(_canonical.overflow.keys())[:20]
            if _canonical.timing:
                _mapping_meta["schema_mapper_timing"] = dataclasses.asdict(_canonical.timing)
            if _canonical.citations:
                _mapping_meta["schema_mapper_citations"] = len(_canonical.citations)

            call = dataclasses.replace(call, metadata={**call.metadata, **_mapping_meta})
        except Exception as _sm_err:
            logger.debug("SchemaMapper cross-validation failed (non-fatal): %s", _sm_err)
    # If normalizer changed content (e.g. thinking fallback for qwen3), rebuild the
    # client response body so the user sees the actual content, not the raw empty string.
    if model_response.content and model_response.content != _pre_norm_content and http_response.status_code == 200:
        try:
            resp_data = json.loads(http_response.body)
            if "choices" in resp_data and resp_data["choices"]:
                resp_data["choices"][0].setdefault("message", {})["content"] = model_response.content
                rebuilt_headers = {k: v for k, v in http_response.headers.items()
                                   if k.lower() not in ("content-length", "transfer-encoding")}
                http_response = Response(
                    content=json.dumps_bytes(resp_data),
                    status_code=http_response.status_code,
                    headers=rebuilt_headers,
                )
        except (json.JSONDecodeError, KeyError, TypeError) as _rb_err:
            logger.debug("Response body rebuild skipped (non-fatal): %s", _rb_err)
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
        _set_disposition(
            request,
            "error_provider",
            reason=f"{provider} returned HTTP {http_response.status_code} for model {model}",
        )
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

    # ── Step 3.5: Tool executor (handles retry, capability cache, active loop) ─
    tool_result = await execute_tools(
        pre.tool_strategy, call, model_response, http_response,
        adapter, request, ctx, settings, provider,
        original_streaming=_original_streaming,
    )
    # If tool executor retried without tools, update our references
    if tool_result.http_response is not None and tool_result.http_response is not http_response:
        http_response = tool_result.http_response
    if tool_result.error is not None:
        _set_disposition(
            request,
            "error_provider",
            reason=f"tool executor error for provider={provider} model={model} after {tool_result.iterations} iteration(s)",
        )
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

    # If the tool executor streamed the final answer, return it directly
    # (with after-stream background task for audit record writing).
    if tool_result.streaming_response is not None and tool_result.stream_buffer is not None:
        buf = tool_result.stream_buffer
        _prebuilt = tool_result.model_response if tool_result.synthetic_stream else None
        task = BackgroundTask(
            _after_stream_record, buf, call, adapter,
            pre.att_id, pre.pv, pre.pr,
            {**pre.audit_metadata, **build_tool_audit_metadata(tool_result.interactions, pre.tool_strategy, tool_result.iterations)},
            pre.budget_estimated, t0, None, request,
            prebuilt_model_response=_prebuilt,
        )
        tool_result.streaming_response.background = task
        if _concurrency_acquired and _concurrency_limiter is not None:
            _concurrency_limiter.release(time.perf_counter() - t0)
        pipeline_duration.labels(step="total").observe(time.perf_counter() - t0)
        outcome = "audit_only_allowed" if (is_audit_only and pre.whb) else "allowed"
        _set_disposition(request, "allowed")
        _inc_request(provider, model, outcome)
        _record_status(200, source="provider")
        return tool_result.streaming_response

    # If the active tool loop ran and produced a final HTTP response, use it
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
