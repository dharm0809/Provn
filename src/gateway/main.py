"""ASGI app entry point."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from gateway.config import get_settings
from gateway.pipeline.orchestrator import handle_request
from gateway.pipeline.context import get_pipeline_context
from gateway.health import health_response, metrics_response
from gateway.auth.api_key import require_api_key_if_configured
from gateway.middleware.completeness import completeness_middleware
from gateway.middleware.ip_rate_limiter import IPRateLimiter
from gateway.lineage.api import (
    lineage_sessions,
    lineage_session_timeline,
    lineage_execution,
    lineage_attempts,
    lineage_metrics_history,
    lineage_token_latency_history,
    lineage_trace,
    lineage_verify,
    lineage_attachments,
    lineage_ab_test_results,
)
from gateway.lineage.cost import lineage_cost_summary
from gateway.control.api import (
    control_list_attestations,
    control_upsert_attestation,
    control_delete_attestation,
    control_list_policies,
    control_create_policy,
    control_update_policy,
    control_delete_policy,
    control_list_budgets,
    control_upsert_budget,
    control_delete_budget,
    control_list_content_policies,
    control_upsert_content_policy,
    control_delete_content_policy,
    control_list_pricing,
    control_upsert_pricing,
    control_delete_pricing,
    control_status,
    control_discover_models,
    control_list_templates,
    control_apply_template,
    control_get_key_policies,
    control_set_key_policies,
    control_remove_key_policy,
    control_list_key_policy_assignments,
    control_get_key_tools,
    control_set_key_tools,
    control_remove_key_tool,
)
from gateway.control.sync_api import (
    sync_attestation_proofs,
    sync_policies,
)
from gateway.intelligence.api import (
    list_production_models as intel_list_production_models,
    list_candidates as intel_list_candidates,
    model_history as intel_model_history,
)
from gateway.models_api import list_models
from gateway.compliance.api import compliance_export
from gateway.openwebui.status_api import openwebui_status
from gateway.openwebui.events_api import openwebui_events_receive, openwebui_events_list

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass  # Fallback to default asyncio event loop

from gateway.util.json_logger import configure_json_logging
configure_json_logging(os.environ.get("WALACOR_LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


def _resolve_header_identity_fallback(request: Request) -> None:
    """Set caller_identity from headers if not already set by JWT."""
    try:
        from gateway.auth.identity import resolve_identity_from_headers
        identity = resolve_identity_from_headers(request)
        if identity is not None:
            request.state.header_identity = identity
            if not hasattr(request.state, "caller_identity"):
                request.state.caller_identity = identity
    except Exception:
        logger.debug("resolve_identity_from_headers failed", exc_info=True)


def _try_jwt_auth(request: Request, settings) -> bool:
    """Attempt JWT authentication. Returns True if valid JWT was found and identity set."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:].strip()
    if not token:
        return False
    # Check if this looks like a JWT (has dots) vs a plain API key
    if token.count(".") < 2:
        return False
    try:
        from gateway.auth.jwt_auth import validate_jwt
        identity = validate_jwt(
            token,
            secret=settings.jwt_secret,
            jwks_url=settings.jwt_jwks_url,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            algorithms=settings.jwt_algorithms_list,
            user_claim=settings.jwt_user_claim,
            email_claim=settings.jwt_email_claim,
            roles_claim=settings.jwt_roles_claim,
            team_claim=settings.jwt_team_claim,
        )
        if identity is not None:
            request.state.caller_identity = identity
            request.state.jwt_identity = identity
            return True
    except Exception as e:
        logger.debug("JWT auth attempt failed: %s", e)
    return False


def _cross_validate_identity(request: Request, settings) -> None:
    """Phase 23: Cross-validate JWT claims against header-claimed identity."""
    ctx = get_pipeline_context()
    if not settings.identity_validation_enabled or not ctx.identity_validator:
        return
    jwt_id = getattr(request.state, "jwt_identity", None)
    header_id = getattr(request.state, "header_identity", None)
    val_result = ctx.identity_validator.validate(jwt_id, header_id, request)
    if val_result.identity:
        request.state.caller_identity = val_result.identity
    if val_result.warnings:
        request.state.identity_warnings = val_result.warnings


async def body_size_middleware(request: Request, call_next):
    """Reject requests whose Content-Length exceeds max_request_body_mb (H5)."""
    settings = get_settings()
    if settings.max_request_body_mb > 0:
        cl = request.headers.get("content-length")
        max_bytes = int(settings.max_request_body_mb * 1024 * 1024)
        if cl and int(cl) > max_bytes:
            return JSONResponse(
                {"error": f"Request body too large (max {settings.max_request_body_mb}MB)"},
                status_code=413,
            )
    return await call_next(request)


_ip_limiter = IPRateLimiter()


async def api_key_middleware(request: Request, call_next):
    """When WALACOR_GATEWAY_API_KEYS is set, require valid API key on proxy routes.

    Supports auth_mode: 'api_key' (default), 'jwt', or 'both'.
    """
    if request.url.path in ("/", "/health", "/metrics", "/v1/models") or request.url.path.startswith(("/lineage/", "/v1/lineage/", "/v1/compliance")):
        return await call_next(request)

    # Pre-auth per-IP rate limiting (before any auth check)
    if request.url.path.startswith("/v1/"):
        client_ip = request.client.host if request.client else "unknown"
        if not _ip_limiter.check(client_ip):
            return JSONResponse(
                {"error": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": "60"},
            )

    settings = get_settings()
    mode = settings.auth_mode

    if mode == "jwt":
        # JWT-only: require valid JWT
        if _try_jwt_auth(request, settings):
            _resolve_header_identity_fallback(request)
            _cross_validate_identity(request, settings)
            return await call_next(request)
        request.state.walacor_disposition = "denied_auth"
        request.state.walacor_reason = "jwt_mode: missing or invalid JWT token"
        return JSONResponse({"error": "Missing or invalid JWT"}, status_code=401)

    if mode == "both":
        # Try JWT first, fall back to API key
        if _try_jwt_auth(request, settings):
            _resolve_header_identity_fallback(request)
            _cross_validate_identity(request, settings)
            return await call_next(request)
        # Fall through to API key check
        err = require_api_key_if_configured(request, settings.api_keys_list)
        if err is not None:
            request.state.walacor_disposition = "denied_auth"
            request.state.walacor_reason = "both_mode: JWT failed and API key missing/invalid"
            return err
        _resolve_header_identity_fallback(request)
        _cross_validate_identity(request, settings)
        return await call_next(request)

    # Default: api_key mode (unchanged behavior)
    err = require_api_key_if_configured(request, settings.api_keys_list)
    if err is not None:
        request.state.walacor_disposition = "denied_auth"
        request.state.walacor_reason = "api_key missing or not in allowlist"
        return err
    _resolve_header_identity_fallback(request)
    _cross_validate_identity(request, settings)
    return await call_next(request)


# CORS headers shared across all responses (methods, allowed headers, expose headers, max-age).
# Access-Control-Allow-Origin is set dynamically by _get_cors_headers() based on config.
_CORS_BASE_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": (
        "Content-Type, Authorization, X-API-Key, "
        "X-Session-ID, X-User-Id, X-User-Email, X-User-Roles, X-Team-Id, "
        "X-OpenWebUI-User-Name, X-OpenWebUI-User-Id, X-OpenWebUI-User-Email, X-OpenWebUI-User-Role"
    ),
    "Access-Control-Expose-Headers": (
        "x-walacor-execution-id, x-walacor-attestation-id, "
        "x-walacor-chain-seq, x-walacor-policy-result, "
        "x-walacor-content-analysis, x-walacor-budget-remaining, "
        "x-walacor-budget-percent, x-walacor-model-id, "
        "X-Session-Id, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset"
    ),
    "Access-Control-Max-Age": "86400",
}


def _get_cors_headers(request: Request) -> dict[str, str]:
    """Build CORS headers with a dynamic Access-Control-Allow-Origin.

    - cors_allowed_origins=""   -> no Allow-Origin header (same-origin only)
    - cors_allowed_origins="*"  -> wildcard (explicit opt-in, backward compat)
    - cors_allowed_origins="https://a.example.com,https://b.example.com"
        -> reflect the request Origin if it matches; omit otherwise.
    """
    settings = get_settings()
    configured = settings.cors_allowed_origins.strip()

    if not configured:
        # Same-origin only: return base headers without Allow-Origin.
        return dict(_CORS_BASE_HEADERS)

    if configured == "*":
        return {**_CORS_BASE_HEADERS, "Access-Control-Allow-Origin": "*"}

    # Check if the request Origin matches any configured origin.
    request_origin = (request.headers.get("origin") or "").strip()
    if not request_origin:
        return dict(_CORS_BASE_HEADERS)

    allowed = {o.strip().rstrip("/") for o in configured.split(",") if o.strip()}
    if request_origin.rstrip("/") in allowed:
        headers = {**_CORS_BASE_HEADERS, "Access-Control-Allow-Origin": request_origin}
        # Vary on Origin so caches don't mix up per-origin responses.
        headers["Vary"] = "Origin"
        return headers

    # Origin not in allowlist: omit Allow-Origin (browser blocks the response).
    return dict(_CORS_BASE_HEADERS)


async def cors_middleware(request: Request, call_next):
    """Handle CORS preflight (OPTIONS) and add CORS headers to responses."""
    if request.method == "OPTIONS":
        return Response(status_code=200, headers=_get_cors_headers(request))
    response = await call_next(request)
    for key, value in _get_cors_headers(request).items():
        response.headers[key] = value
    return response


# Security response headers — applied to ALL responses (XSS / clickjack / MIME-sniff protection).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# CSP is only relevant for HTML pages served by the lineage dashboard, not API responses.
_LINEAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'"
)


async def security_headers_middleware(request: Request, call_next):
    """Append security headers to every response; add CSP for /lineage/ paths."""
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers[key] = value
    if request.url.path.startswith("/lineage/"):
        response.headers["Content-Security-Policy"] = _LINEAGE_CSP
    return response


async def _root_redirect(request: Request):
    return RedirectResponse(url="/lineage/", status_code=302)


async def catch_all_post(request: Request):
    return await handle_request(request)


async def _attachment_notify(request: Request) -> Response:
    from gateway.middleware.attachment_tracker import attachment_notify_handler
    ctx = get_pipeline_context()
    if ctx.attachment_cache is None:
        return JSONResponse({"error": "Attachment tracking not enabled"}, status_code=503)
    return await attachment_notify_handler(request, ctx.attachment_cache)


async def _self_test() -> None:
    """Verify critical subsystems before accepting traffic. Raises on failure."""
    from datetime import datetime, timezone
    from urllib.parse import urlparse

    settings = get_settings()
    ctx = get_pipeline_context()

    # Hash self-test: SHA3-512 available (used for session chain); gateway does not hash prompt/response.
    from gateway.core import compute_sha3_512_string
    h = compute_sha3_512_string("self-test")
    if len(h) != 128:
        raise RuntimeError(f"Hash self-test failed: expected length 128, got {len(h)}")

    # Control plane URL validation (governance mode only, skip when embedded CP handles it).
    if not settings.skip_governance and not settings.walacor_storage_enabled and settings.control_plane_url:
        parsed = urlparse(settings.control_plane_url)
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError(f"Control plane URL invalid scheme: {parsed.scheme}")

    # WAL write/deliver smoke-test (WAL mode only). Record is dict (no prompt_hash/response_hash).
    if ctx.wal_writer:
        record = {
            "execution_id": "self-test-startup",
            "model_attestation_id": "self-test",
            "policy_version": 0,
            "policy_result": "pass",
            "tenant_id": settings.gateway_tenant_id,
            "gateway_id": settings.gateway_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        ctx.wal_writer.write_and_fsync(record)
        ctx.wal_writer.mark_delivered(record["execution_id"])

    logger.info("Startup self-test passed")


async def _init_governance(settings, ctx) -> None:
    """Phase 1-4: caches, sync client, startup sync.

    When no control_plane_url is configured, governance features (session chain,
    budget, content analysis) still work — models are auto-attested on first use
    and an empty (pass-all) policy set is seeded.
    """
    from gateway.cache.attestation_cache import AttestationCache
    from gateway.cache.policy_cache import PolicyCache

    ctx.attestation_cache = AttestationCache(ttl_seconds=settings.attestation_cache_ttl)
    ctx.policy_cache = PolicyCache(staleness_threshold_seconds=settings.policy_staleness_threshold)

    if not settings.control_plane_url:
        # No control plane — seed empty policy set (prevents staleness fail-close)
        # and leave sync_client as None (models will be auto-attested on first use).
        v = ctx.policy_cache.next_version()
        ctx.policy_cache.set_policies(v, [])
        logger.info(
            "Governance mode: no control plane configured — auto-attest enabled, "
            "policies pass-all (connect a control plane to enforce attestation and policy rules)"
        )
        return

    from gateway.sync.sync_client import SyncClient

    control_plane_key = (settings.control_plane_api_key or "").strip() or None
    ctx.sync_client = SyncClient(
        control_plane_url=settings.control_plane_url,
        tenant_id=settings.gateway_tenant_id,
        attestation_cache=ctx.attestation_cache,
        policy_cache=ctx.policy_cache,
        api_key=control_plane_key,
    )
    await ctx.sync_client.startup_sync(provider=settings.gateway_provider)


def _init_wal(settings, ctx) -> None:
    """Phase 2: WAL writer and delivery worker."""
    from gateway.wal.writer import WALWriter
    from gateway.wal.delivery_worker import DeliveryWorker

    wal_dir = Path(settings.wal_path)
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx.wal_writer = WALWriter(str(wal_dir / "wal.db"))
    ctx.wal_writer.start()
    if settings.control_plane_url:
        # Delivery worker ships WAL records to a remote control plane aggregator.
        # Only needed when a separate control plane URL is configured.
        # When walacor_storage_enabled=True, records go directly to Walacor
        # via the storage router (dual-write) — no delivery worker needed.
        ctx.delivery_worker = DeliveryWorker(ctx.wal_writer)
        ctx.delivery_worker.start()
    elif not settings.walacor_storage_enabled:
        logger.info("Delivery worker skipped: no Walacor backend or control plane configured")


async def _init_batch_writer(settings, ctx) -> None:
    """Task 18: BatchWriter for group commit WAL writes."""
    from gateway.wal.batch_writer import BatchWriter

    ctx.batch_writer = BatchWriter(
        wal_writer=ctx.wal_writer,
        flush_interval_ms=settings.wal_batch_flush_ms,
        max_size=settings.wal_batch_max_size,
    )
    await ctx.batch_writer.start()
    logger.info(
        "BatchWriter enabled: flush_ms=%d max_size=%d",
        settings.wal_batch_flush_ms,
        settings.wal_batch_max_size,
    )


async def _init_walacor(settings, ctx) -> None:
    """Walacor backend storage: authenticate and warm up the client."""
    from gateway.walacor.client import WalacorClient

    ctx.walacor_client = WalacorClient(
        server=settings.walacor_server,
        username=settings.walacor_username,
        password=settings.walacor_password,
        executions_etid=settings.walacor_executions_etid,
        attempts_etid=settings.walacor_attempts_etid,
        tool_events_etid=settings.walacor_tool_events_etid,
        lifecycle_events_etid=settings.walacor_lifecycle_events_etid,
    )
    await ctx.walacor_client.start()
    logger.info(
        "Walacor storage ready: executions_etid=%d attempts_etid=%d",
        settings.walacor_executions_etid, settings.walacor_attempts_etid,
    )


def _init_storage(settings, ctx) -> None:
    """Build StorageRouter from available backends."""
    from gateway.storage import StorageRouter, WALBackend, WalacorBackend

    backends = []
    if ctx.wal_writer:
        backends.append(WALBackend(ctx.wal_writer, batch_writer=ctx.batch_writer))
    if ctx.walacor_client:
        backends.append(WalacorBackend(ctx.walacor_client))
    ctx.storage = StorageRouter(backends)
    logger.info("Storage router ready: backends=%s", [b.name for b in backends])


def _init_content_analyzers(settings, ctx) -> None:
    """Phase 10: PII and toxicity content analyzers."""
    from gateway.content.pii_detector import PIIDetector
    from gateway.content.toxicity_detector import ToxicityDetector

    if settings.pii_detection_enabled:
        ctx.content_analyzers.append(PIIDetector())
        logger.info("Content analyzer loaded: walacor.pii.v1")
    if settings.toxicity_detection_enabled:
        extra = [t.strip() for t in settings.toxicity_deny_terms.split(",") if t.strip()]
        ctx.content_analyzers.append(ToxicityDetector(extra_terms=extra or None))
        logger.info("Content analyzer loaded: walacor.toxicity.v1 (extra_terms=%d)", len(extra))


def _init_safety_classifier(settings, ctx) -> None:
    """ONNX Safety Classifier — lightweight Llama Guard replacement. Always-on."""
    try:
        from gateway.content.safety_classifier import SafetyClassifier
        # Registry wiring (Task 12): when registered, the ONNX session is
        # loaded on first `analyze()` from `production/safety.onnx` and
        # rebuilds after promote/rollback. When no registry is available,
        # the classifier falls back to its packaged `_ONNX_PATH`.
        classifier = SafetyClassifier(
            verdict_buffer=ctx.verdict_buffer,
            registry=ctx.model_registry,
            model_name="safety" if ctx.model_registry is not None else None,
        )
        # When a registry is wired the session load is deferred to first
        # inference, so `_loaded` stays False at init. Trust the registry
        # setup — a missing production file has already been logged by the
        # migration helper and reload will fail-safe.
        if classifier._loaded or ctx.model_registry is not None:
            ctx.content_analyzers.append(classifier)
            logger.info(
                "Content analyzer loaded: truzenai.safety.v1 (ONNX, %d categories, registry=%s)",
                len(classifier._labels),
                ctx.model_registry is not None,
            )
        else:
            logger.warning("SafetyClassifier ONNX not found — skipping")
    except Exception as e:
        logger.warning("SafetyClassifier init failed (non-fatal): %s", e)


def _init_llama_guard(settings, ctx) -> None:
    """Phase 17: Llama Guard 3 content analyzer (local Ollama, fail-open)."""
    from gateway.content.llama_guard import LlamaGuardAnalyzer

    ollama_url = settings.llama_guard_ollama_url or settings.provider_ollama_url
    if not ollama_url:
        logger.warning("llama_guard_enabled=True but no Ollama URL configured — skipping")
        return
    analyzer = LlamaGuardAnalyzer(
        ollama_url=ollama_url,
        model=settings.llama_guard_model,
        timeout_ms=settings.llama_guard_timeout_ms,
        http_client=ctx.http_client,
    )
    ctx.content_analyzers.append(analyzer)
    logger.info(
        "Content analyzer loaded: walacor.llama_guard.v3 (model=%s url=%s timeout_ms=%d)",
        settings.llama_guard_model, ollama_url, settings.llama_guard_timeout_ms,
    )


def _init_presidio_pii(settings, ctx) -> None:
    """Optional Presidio NER PII analyzer (fail-open on missing deps)."""
    try:
        from gateway.content.presidio_pii import PresidioPIIAnalyzer
        analyzer = PresidioPIIAnalyzer()
        if analyzer._available:
            ctx.content_analyzers.append(analyzer)
            logger.info("Content analyzer loaded: walacor.presidio_pii.v1")
    except Exception as e:
        logger.warning("Failed to initialize Presidio PII analyzer: %s", e)



def _init_image_ocr(settings, ctx):
    """Initialize image OCR analyzer if enabled."""
    from gateway.content.image_ocr import ImageOCRAnalyzer
    ctx.image_ocr_analyzer = ImageOCRAnalyzer(max_size_mb=settings.image_ocr_max_size_mb)
    logger.info("Image OCR analyzer enabled: max_size=%dMB", settings.image_ocr_max_size_mb)


async def _init_audit_exporter(settings, ctx) -> None:
    """B.2: Audit log exporter — file (JSONL), webhook (Splunk/Datadog/Elastic)."""
    if not settings.export_enabled:
        return
    if settings.export_type == "file":
        from gateway.export.file_exporter import FileExporter
        ctx.audit_exporter = FileExporter(
            file_path=settings.export_file_path,
            max_size_mb=settings.export_file_max_size_mb,
        )
    elif settings.export_type == "webhook":
        from gateway.export.webhook_exporter import WebhookExporter
        extra_headers: dict = {}
        if settings.export_webhook_headers:
            import gateway.util.json_utils as _json
            try:
                extra_headers = _json.loads(settings.export_webhook_headers)
            except Exception:
                logger.warning("export_webhook_headers is not valid JSON; ignoring")
        exporter = WebhookExporter(
            url=settings.export_webhook_url,
            headers=extra_headers,
            batch_size=settings.export_batch_size,
            flush_interval=settings.export_flush_interval,
        )
        exporter.start()
        ctx.audit_exporter = exporter
    else:
        logger.warning("Unknown export_type=%r; audit exporter disabled", settings.export_type)
        return
    logger.info("Audit exporter initialized: type=%s", settings.export_type)


def _init_prompt_guard(settings, ctx) -> None:
    """Prompt Guard 2 injection detection (CPU-based, 2-5ms)."""
    from gateway.content.prompt_guard import PromptGuardAnalyzer
    analyzer = PromptGuardAnalyzer(
        model_id=settings.prompt_guard_model,
        threshold=settings.prompt_guard_threshold,
    )
    if analyzer._available:
        ctx.content_analyzers.append(analyzer)
        logger.info("Content analyzer loaded: walacor.prompt_guard.v2 (model=%s)", settings.prompt_guard_model)


def _init_dlp_classifier(settings, ctx) -> None:
    """B.8: DLP data classification — financial, health, secrets, infrastructure."""
    from gateway.content.dlp_classifier import DLPClassifier
    categories = {c.strip() for c in settings.dlp_categories.split(",") if c.strip()}
    classifier = DLPClassifier(enabled_categories=categories)
    # Apply per-category action overrides from config
    action_overrides = [
        {"category": "financial", "action": settings.dlp_action_financial},
        {"category": "health", "action": settings.dlp_action_health},
        {"category": "secrets", "action": settings.dlp_action_secrets},
        {"category": "infrastructure", "action": settings.dlp_action_infrastructure},
    ]
    classifier.configure(action_overrides)
    ctx.content_analyzers.append(classifier)
    logger.info("Content analyzer loaded: walacor.dlp.v1 (categories=%s)", sorted(categories))


async def _init_redis(settings) -> "Any | None":
    """Phase 15: Redis client for shared state (multi-replica). Returns None when redis_url is empty."""
    if not settings.redis_url:
        return None
    try:
        import redis.asyncio as aioredis
    except ImportError:
        raise RuntimeError(
            "WALACOR_REDIS_URL is set but 'redis' package is not installed. "
            "Install with: pip install 'walacor-gateway[redis]'"
        )
    client = aioredis.from_url(settings.redis_url, decode_responses=False)
    await client.ping()  # fail fast at startup if unreachable
    # Redact password from URL before logging to avoid credential leakage in logs.
    from urllib.parse import urlparse, urlunparse
    _parsed = urlparse(settings.redis_url)
    _safe_url = (
        urlunparse(_parsed._replace(netloc=_parsed.netloc.replace(f":{_parsed.password}@", ":***@")))
        if _parsed.password else settings.redis_url
    )
    logger.info("Redis connected: %s", _safe_url)
    return client


def _init_budget_tracker(settings, ctx) -> None:
    """Phase 11: token budget tracker (in-memory or Redis-backed)."""
    from gateway.pipeline.budget_tracker import make_budget_tracker

    # Parse alert thresholds and pass alert_bus to in-memory tracker
    alert_thresholds = [int(t.strip()) for t in settings.alert_budget_thresholds.split(",") if t.strip()]
    ctx.budget_tracker = make_budget_tracker(ctx.redis_client, settings, alert_bus=ctx.alert_bus, alert_thresholds=alert_thresholds)
    if settings.token_budget_enabled and settings.token_budget_max_tokens > 0:
        if ctx.redis_client is None:
            # In-memory tracker supports synchronous configure
            ctx.budget_tracker.configure(
                settings.gateway_tenant_id, None,
                settings.token_budget_period, settings.token_budget_max_tokens,
            )
        logger.info(
            "Token budget enabled: period=%s max_tokens=%d",
            settings.token_budget_period, settings.token_budget_max_tokens,
        )


def _init_session_chain(settings, ctx) -> None:
    """Phase 13: Merkle session chain tracker (in-memory or Redis-backed)."""
    if not settings.session_chain_enabled:
        return
    from gateway.pipeline.session_chain import make_session_chain_tracker

    ctx.session_chain = make_session_chain_tracker(ctx.redis_client, settings)
    logger.info(
        "Session chain tracker enabled: max_sessions=%s ttl=%ds",
        getattr(ctx.session_chain, '_max', 'redis'),
        settings.session_chain_ttl,
    )

    # Warm in-memory tracker from WAL so chains survive gateway restarts
    if ctx.redis_client is None and ctx.wal_writer is not None:
        try:
            heads = ctx.wal_writer.get_chain_heads(ttl_hours=settings.session_chain_ttl // 3600 or 1)
            if heads and hasattr(ctx.session_chain, 'warm'):
                ctx.session_chain.warm(heads)
        except Exception:
            logger.warning("Chain warm from WAL failed — new sessions will start fresh", exc_info=True)


async def _init_tool_registry(settings, ctx) -> None:
    """Phase 14: MCP tool registry for the active tool strategy."""
    from gateway.mcp.registry import ToolRegistry, parse_mcp_server_configs

    configs = parse_mcp_server_configs(settings.mcp_servers_json)
    if not configs:
        logger.info("tool_aware_enabled=True but no MCP server configs found — tool registry not started")
        return

    # Parse extra allowed commands from config
    extra_cmds: set[str] | None = None
    if settings.mcp_allowed_commands:
        extra_cmds = {c.strip() for c in settings.mcp_allowed_commands.split(",") if c.strip()}

    ctx.tool_registry = ToolRegistry(configs, extra_allowed_commands=extra_cmds)
    await ctx.tool_registry.startup()
    logger.info(
        "Tool registry ready: %d server(s), %d tool(s) — %s",
        len(ctx.tool_registry.server_names()),
        ctx.tool_registry.get_tool_count(),
        ctx.tool_registry.server_names(),
    )


async def _init_web_search_tool(settings, ctx) -> None:
    """Register built-in web search in ToolRegistry for the active tool strategy."""
    from gateway.tools.web_search import WebSearchTool
    from gateway.mcp.registry import ToolRegistry

    if ctx.tool_registry is None:
        ctx.tool_registry = ToolRegistry([])

    tool = WebSearchTool(
        provider=settings.web_search_provider,
        api_key=settings.web_search_api_key,
        max_results=settings.web_search_max_results,
        http_client=ctx.http_client,
    )
    await ctx.tool_registry.register_builtin_client("builtin_web_search", tool)
    logger.info(
        "Built-in web search registered: provider=%s max_results=%d",
        settings.web_search_provider, settings.web_search_max_results,
    )


def _init_lineage(settings, ctx) -> None:
    """Phase 18: Lineage dashboard reader.

    Prefers WalacorLineageReader (reads from Walacor API) when walacor_client
    is available. Falls back to SQLite LineageReader for local-only mode.
    """
    if not settings.lineage_enabled:
        return

    # Dashboard reads exclusively from Walacor. The local SQLite WAL remains a
    # durability sink for the delivery worker (replay on outage), but is never
    # used as a read source for the dashboard. Lineage requires a Walacor client.
    if ctx.walacor_client is None:
        logger.warning(
            "Lineage dashboard disabled: no Walacor client configured. "
            "Set WALACOR_SERVER + credentials to enable the dashboard."
        )
        return

    from gateway.lineage.walacor_reader import WalacorLineageReader
    ctx.lineage_reader = WalacorLineageReader(
        client=ctx.walacor_client,
        executions_etid=settings.walacor_executions_etid,
        attempts_etid=settings.walacor_attempts_etid,
        tool_events_etid=settings.walacor_tool_events_etid,
    )
    logger.info(
        "Lineage dashboard enabled: reading from Walacor API (ETId %d/%d/%d)",
        settings.walacor_executions_etid, settings.walacor_attempts_etid,
        settings.walacor_tool_events_etid,
    )


def _init_otel(settings, ctx) -> None:
    """Phase 17: OpenTelemetry tracer (optional; fail-open if SDK not installed)."""
    from gateway.telemetry.otel import init_tracer

    ctx.tracer = init_tracer(
        service_name=settings.otel_service_name,
        endpoint=settings.otel_endpoint,
    )


def _init_control_plane(settings, ctx) -> None:
    """Phase 20: Embedded control plane — SQLite-backed CRUD + local sync."""
    from gateway.control.store import ControlPlaneStore
    from gateway.control.loader import load_into_caches

    db_path = settings.control_plane_db_path
    if not db_path:
        db_path = str(Path(settings.wal_path) / "control.db")
    ctx.control_store = ControlPlaneStore(db_path)
    # Force table creation
    ctx.control_store._ensure_conn()
    load_into_caches(ctx.control_store, ctx, settings)
    logger.info("Embedded control plane ready: %s", db_path)


async def _auto_register_models(settings, ctx) -> None:
    """Auto-discover provider models and register any new ones in the control store."""
    if ctx.control_store is None or ctx.http_client is None:
        return
    try:
        from gateway.control.discovery import discover_provider_models

        discovered = await discover_provider_models(settings, ctx.http_client)
        if not discovered:
            return
        existing = ctx.control_store.list_attestations(settings.gateway_tenant_id)
        existing_keys = {(a["provider"], a["model_id"]) for a in existing}
        registered = 0
        for m in discovered:
            key = (m["provider"], m["model_id"])
            if key not in existing_keys:
                ctx.control_store.upsert_attestation({
                    "model_id": m["model_id"],
                    "provider": m["provider"],
                    "status": "active",
                    "verification_level": "auto_attested",
                    "tenant_id": settings.gateway_tenant_id,
                    "notes": "Auto-registered on startup",
                })
                registered += 1
        if registered:
            # Refresh attestation cache with newly registered models
            from gateway.control.loader import load_into_caches
            load_into_caches(ctx.control_store, ctx, settings)
            logger.info("Auto-registered %d model(s) from providers", registered)
    except Exception:
        logger.warning("Auto-register models failed (non-fatal)", exc_info=True)


def _init_alert_bus(settings, ctx) -> None:
    """Phase 26: Alert event bus with webhook/Slack/PagerDuty dispatchers."""
    from gateway.alerts.bus import AlertBus
    from gateway.alerts.dispatcher import WebhookDispatcher, SlackDispatcher, PagerDutyDispatcher

    bus = AlertBus()
    for url in (u.strip() for u in settings.webhook_urls.split(",") if u.strip()):
        if "hooks.slack.com" in url:
            bus.add_dispatcher(SlackDispatcher(url))
        else:
            bus.add_dispatcher(WebhookDispatcher(url))
    if settings.pagerduty_routing_key:
        bus.add_dispatcher(PagerDutyDispatcher(settings.pagerduty_routing_key))
    ctx.alert_bus = bus
    logger.info(
        "Alert bus ready: %d dispatcher(s)",
        len(bus._dispatchers),
    )


def _extract_provider_from_url(url: str) -> str:
    """Infer provider name from a provider URL."""
    if "ollama" in url or ":11434" in url:
        return "ollama"
    if "openai" in url:
        return "openai"
    if "anthropic" in url:
        return "anthropic"
    return "unknown"


def _init_rate_limiter(settings, ctx) -> None:
    """Phase 26: Request rate limiter (in-memory or Redis-backed)."""
    from gateway.pipeline.rate_limiter import SlidingWindowRateLimiter, RedisRateLimiter

    if ctx.redis_client is not None:
        ctx.rate_limiter = RedisRateLimiter(ctx.redis_client)
    else:
        ctx.rate_limiter = SlidingWindowRateLimiter()
    logger.info(
        "Rate limiter enabled: type=%s rpm=%d per_model=%s",
        type(ctx.rate_limiter).__name__,
        settings.rate_limit_rpm,
        settings.rate_limit_per_model,
    )


def _init_semantic_cache(settings, ctx) -> None:
    """B.4: Initialize in-memory semantic cache (exact-match tier)."""
    if not settings.semantic_cache_enabled:
        return
    from gateway.cache.semantic_cache import SemanticCache
    ctx.semantic_cache = SemanticCache(
        max_entries=settings.semantic_cache_max_entries,
        ttl=settings.semantic_cache_ttl,
    )
    logger.info(
        "Semantic cache initialized: max_entries=%d ttl=%ds",
        settings.semantic_cache_max_entries,
        settings.semantic_cache_ttl,
    )


# Phase 25 Task 12: in-repo packaged `.onnx` files. The source tree ships a
# baseline model per canonical name — on first run we copy them into the
# registry's `production/` so the directory-backed registry is self-bootstrapping
# and all clients converge on a single source of truth for live models.
_PACKAGED_MODEL_SOURCES: dict[str, Path] = {
    "intent": Path(__file__).parent / "classifier" / "model.onnx",
    "schema_mapper": Path(__file__).parent / "schema" / "schema_mapper.onnx",
    "safety": Path(__file__).parent / "content" / "safety_classifier.onnx",
}


def _migrate_packaged_models_to_registry(registry) -> None:
    """Copy in-repo baseline `.onnx` files into `{base}/production/`.

    Idempotent — any destination that already exists is left untouched, so
    a redeployment never clobbers a promoted model. Missing packaged source
    files are also silently skipped (a model not shipped with the wheel is
    a legitimate runtime state — e.g. custom builds).

    Called exactly once at startup after `ModelRegistry.ensure_structure()`.
    """
    import shutil

    copied = 0
    for model_name, src in _PACKAGED_MODEL_SOURCES.items():
        dst = registry.production_path(model_name)
        if dst.exists():
            continue
        if not src.exists():
            logger.debug(
                "Model registry migration: packaged source %s missing for %r",
                src, model_name,
            )
            continue
        try:
            shutil.copy2(src, dst)
            copied += 1
            logger.info(
                "Model registry migration: seeded production/%s from %s",
                dst.name, src,
            )
        except Exception:
            # Never let migration break startup. The client will fail-open
            # to heuristics / no session if no production file exists.
            logger.warning(
                "Model registry migration: copy failed for %r", model_name,
                exc_info=True,
            )
    if copied == 0:
        logger.debug("Model registry migration: nothing to seed (all destinations present)")


def _init_model_registry(settings, ctx) -> None:
    """Phase 25 Task 12: initialize the directory-backed ONNX model registry.

    Attached to `ctx.model_registry` for client wiring. The base path
    defaults to `{wal_path}/models` so model artifacts live alongside other
    runtime state (WAL, intelligence.db) and stay out of the source tree.

    Fail-open: any failure logs a warning and leaves `ctx.model_registry`
    as `None`, at which point the ONNX clients keep using their pre-Task-12
    packaged paths (the reload-on-promote signal is simply inert).
    """
    if not settings.intelligence_enabled:
        return
    try:
        from gateway.intelligence.registry import ModelRegistry

        base = settings.onnx_models_base_path or f"{settings.wal_path}/models"
        registry = ModelRegistry(base_path=base)
        registry.ensure_structure()
        _migrate_packaged_models_to_registry(registry)
        ctx.model_registry = registry
        logger.info("Model registry initialized at %s", base)
    except Exception as e:
        logger.warning("Model registry init failed (non-fatal): %s", e)
        ctx.model_registry = None


def _init_shadow_runner(settings, ctx) -> None:
    """Phase 25 Task 22: wire the shadow-inference runner.

    Depends on `ctx.intelligence_db` for the `shadow_comparisons`
    write target. Fail-open: missing DB or init failure leaves
    `ctx.shadow_runner=None` and every ONNX client's shadow hook
    silently no-ops.
    """
    if not settings.intelligence_enabled:
        return
    if ctx.intelligence_db is None:
        return
    try:
        from gateway.intelligence.shadow import ShadowRunner
        ctx.shadow_runner = ShadowRunner(ctx.intelligence_db)
        logger.info("Shadow runner initialized")
    except Exception as e:
        logger.warning("Shadow runner init failed (non-fatal): %s", e)
        ctx.shadow_runner = None


def _init_harvesters(settings, ctx) -> None:
    """Phase 25 Task 13+: verdict harvester runner with per-model harvesters.

    Creates a `HarvesterRunner` with any available per-model harvesters
    registered, then starts its background consumer task. Each harvester
    is optional — missing dependencies (e.g. `ctx.intelligence_db`) just
    skip that harvester's registration so the runner still dispatches to
    the ones that ARE available.

    Fail-open: any failure leaves `ctx.harvester_runner=None` and the
    orchestrator hook short-circuits.
    """
    if not settings.intelligence_enabled:
        return
    try:
        from gateway.intelligence.harvesters import HarvesterRunner

        harvesters: list = []

        # Tasks 14-15: per-model harvesters — each needs the intelligence
        # DB for the divergence back-write UPDATE. Skip individual ones
        # on import/init failure so a partial registration still produces
        # a working runner for the remaining harvesters.
        if ctx.intelligence_db is not None:
            try:
                from gateway.intelligence.harvesters.schema_mapper import (
                    SchemaMapperHarvester,
                )
                harvesters.append(SchemaMapperHarvester(ctx.intelligence_db))
            except Exception as _sm_err:
                logger.warning(
                    "SchemaMapperHarvester init failed (non-fatal): %s", _sm_err,
                )
            try:
                from gateway.intelligence.harvesters.safety import SafetyHarvester
                harvesters.append(SafetyHarvester(ctx.intelligence_db))
            except Exception as _sh_err:
                logger.warning(
                    "SafetyHarvester init failed (non-fatal): %s", _sh_err,
                )
            # Task 16: Intent harvester — also optionally consumes the
            # teacher LLM (costs $$ at high sample rates, so default is
            # whatever the operator set in config). `http_client` is the
            # shared `httpx.AsyncClient` already initialized upstream;
            # falling back to `None` disables sampling cleanly.
            try:
                from gateway.intelligence.harvesters.intent import IntentHarvester
                harvesters.append(IntentHarvester(
                    ctx.intelligence_db,
                    teacher_url=settings.teacher_llm_url,
                    teacher_sample_rate=settings.teacher_llm_sample_rate,
                    http_client=ctx.http_client,
                ))
            except Exception as _ih_err:
                logger.warning(
                    "IntentHarvester init failed (non-fatal): %s", _ih_err,
                )

        runner = HarvesterRunner(harvesters=harvesters, max_queue=1000)
        runner.start()
        ctx.harvester_runner = runner
        logger.info(
            "Harvester runner started (queue size=1000, harvesters=%d)",
            len(harvesters),
        )
    except Exception as e:
        logger.warning("Harvester runner init failed (non-fatal): %s", e)
        ctx.harvester_runner = None


def _init_intelligence(settings, ctx) -> None:
    """Phase 25: ONNX self-learning intelligence layer.

    Creates the verdict SQLite store, an in-memory bounded buffer, and a
    background flush worker. The buffer is stored on `ctx.verdict_buffer`
    so ONNX clients (Intent / SchemaMapper / Safety) can record verdicts
    fire-and-forget from the inference hot path.

    Fail-open: any failure logs a warning and leaves `ctx.verdict_buffer`
    set to None, which cleanly disables verdict capture across all ONNX
    clients (they guard with `if self._verdict_buffer is not None`).
    """
    if not settings.intelligence_enabled:
        logger.debug("intelligence layer disabled (WALACOR_INTELLIGENCE_ENABLED=false)")
        return

    try:
        from gateway.intelligence.db import IntelligenceDB
        from gateway.intelligence.retention import RetentionSweeper
        from gateway.intelligence.verdict_buffer import VerdictBuffer
        from gateway.intelligence.verdict_flush import VerdictFlushWorker

        db_path = settings.intelligence_db_path or f"{settings.wal_path}/intelligence.db"
        db = IntelligenceDB(db_path)
        db.init_schema()

        buffer = VerdictBuffer(max_size=10_000)
        worker = VerdictFlushWorker(buffer, db, flush_interval_s=1.0, batch_size=500)

        ctx.intelligence_db = db
        ctx.verdict_buffer = buffer
        ctx.intelligence_flush_worker = worker
        ctx.intelligence_flush_task = asyncio.create_task(worker.run())

        sweeper = RetentionSweeper(
            db,
            retention_days=settings.verdict_retention_days,
            sweep_interval_s=3600.0,
        )
        ctx.intelligence_retention_sweeper = sweeper
        ctx.intelligence_retention_task = asyncio.create_task(sweeper.run())
        logger.info(
            "Intelligence verdict capture initialized (db=%s, retention_days=%d)",
            db_path,
            settings.verdict_retention_days,
        )
    except Exception as e:
        logger.warning("Intelligence layer init failed (non-fatal): %s", e)
        ctx.intelligence_db = None
        ctx.verdict_buffer = None
        ctx.intelligence_flush_worker = None
        ctx.intelligence_flush_task = None
        ctx.intelligence_retention_sweeper = None
        ctx.intelligence_retention_task = None


def _init_load_balancer(settings, ctx) -> None:
    """Phase 25: Initialize load balancer and circuit breakers from model_groups_json."""
    from gateway.routing.balancer import Endpoint, LoadBalancer, ModelGroup
    from gateway.routing.circuit import CircuitBreakerRegistry

    raw = settings.model_groups_json.strip()
    if not raw:
        ctx.circuit_breakers = CircuitBreakerRegistry(
            fail_max=settings.circuit_breaker_fail_max,
            reset_timeout=settings.circuit_breaker_reset_timeout,
        )
        return

    import json as _json
    if not raw.startswith("{"):
        raw = Path(raw).read_text()
    groups_dict = _json.loads(raw)

    groups = []
    for pattern, endpoints_list in groups_dict.items():
        endpoints = [
            Endpoint(
                url=ep["url"],
                api_key=ep.get("key", ""),
                weight=ep.get("weight", 1.0),
            )
            for ep in endpoints_list
        ]
        groups.append(ModelGroup(pattern=pattern, endpoints=endpoints))

    ctx.load_balancer = LoadBalancer(groups)
    ctx.circuit_breakers = CircuitBreakerRegistry(
        fail_max=settings.circuit_breaker_fail_max,
        reset_timeout=settings.circuit_breaker_reset_timeout,
    )
    logger.info("Load balancer initialized with %d model groups", len(groups))


def _next_backoff(current: float, cap: float) -> float:
    """Exponential backoff: 5 s initial, doubles each step, capped at cap."""
    step = current * 2 + 5.0 if current else 5.0
    return min(step, cap)


async def _sync_once(ctx, provider: str, current_backoff: float, backoff_max: float) -> float:
    """Run one sync cycle and return the updated backoff value."""
    try:
        a_ok = await ctx.sync_client.sync_attestations(provider=provider)
        p_ok = await ctx.sync_client.sync_policies()
        return 0.0 if (a_ok and p_ok) else _next_backoff(current_backoff, backoff_max)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Sync loop error: %s", e, exc_info=True)
        return _next_backoff(current_backoff, backoff_max)


async def _run_sync_loop(settings, ctx) -> None:
    """Periodic pull-sync with exponential backoff on failure."""
    backoff = 0.0
    backoff_max = 60.0
    while True:
        await asyncio.sleep(settings.sync_interval + backoff)
        if ctx.sync_client:
            backoff = await _sync_once(ctx, settings.gateway_provider, backoff, backoff_max)


async def _event_loop_lag_monitor():
    """Periodically measure asyncio event loop scheduling lag."""
    from gateway.metrics.prometheus import event_loop_lag_seconds
    while True:
        t0 = asyncio.get_event_loop().time()
        await asyncio.sleep(1.0)
        lag = asyncio.get_event_loop().time() - t0 - 1.0
        event_loop_lag_seconds.set(max(0.0, lag))


async def _merkle_checkpoint_loop(ctx, interval: int) -> None:
    """Periodically build Merkle tree checkpoint from recent session chain hashes."""
    from gateway.crypto.merkle_tree import build_merkle_tree

    while True:
        await asyncio.sleep(interval)
        try:
            if ctx.wal_writer is None:
                continue
            # Read recent record hashes from WAL
            conn = ctx.wal_writer._ensure_conn()
            cur = conn.execute(
                "SELECT json_extract(record_json, '$.record_hash') FROM wal_records "
                "WHERE json_extract(record_json, '$.record_hash') IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1000"
            )
            hashes = [row[0] for row in cur.fetchall() if row[0]]
            if not hashes:
                continue
            root, levels = build_merkle_tree(hashes)
            logger.info("Merkle checkpoint: root=%s leaves=%d levels=%d", root[:16], len(hashes), len(levels))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Merkle checkpoint failed", exc_info=True)


async def on_startup() -> None:
    settings = get_settings()
    ctx = get_pipeline_context()

    try:
        # Walacor storage is mode-independent: init before the skip_governance shortcut
        # so completeness attempts are always written when credentials are configured.
        if settings.walacor_storage_enabled:
            await _init_walacor(settings, ctx)

        # Shared HTTP client for all modes. Without this, skip_governance would create
        # a new one-off httpx.AsyncClient per request (Finding 7).
        async def _on_provider_response(response):
            """Phase 23: Record provider results for resource monitor."""
            if ctx.resource_monitor:
                provider = _extract_provider_from_url(str(response.url))
                ctx.resource_monitor.record_provider_result(
                    provider, response.status_code < 500)

        _limits = httpx.Limits(
            max_connections=settings.http_pool_max_connections,
            max_keepalive_connections=settings.http_pool_max_keepalive,
            keepalive_expiry=settings.http_keepalive_expiry,
        )
        ctx.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.provider_timeout, connect=settings.provider_connect_timeout),
            limits=_limits,
            http2=True,
            event_hooks={"response": [_on_provider_response]},
        )

        # Always init WAL when lineage is enabled (lineage reads from local WAL).
        if settings.lineage_enabled and not ctx.wal_writer:
            _init_wal(settings, ctx)

        if settings.skip_governance:
            ctx.skip_governance = True
            if settings.wal_batch_enabled and ctx.wal_writer:
                await _init_batch_writer(settings, ctx)
            _init_storage(settings, ctx)
            _init_lineage(settings, ctx)
            ctx.event_loop_lag_task = asyncio.create_task(_event_loop_lag_monitor())
            logger.info("Gateway running in skip_governance (transparent proxy) mode")
            return

        await _init_governance(settings, ctx)
        if not settings.walacor_storage_enabled and not ctx.wal_writer:
            _init_wal(settings, ctx)
        if settings.wal_batch_enabled and ctx.wal_writer:
            await _init_batch_writer(settings, ctx)
        _init_storage(settings, ctx)
        _init_lineage(settings, ctx)
        ctx.redis_client = await _init_redis(settings)
        # Phase 25: Intelligence verdict capture must init BEFORE any ONNX client
        # (SafetyClassifier, SchemaMapper) so the buffer is available for wiring.
        # Model registry (Task 12) initializes first so clients can resolve their
        # ONNX file via `ctx.model_registry.production_path(...)` and pick up
        # promoted candidates via the Task 11 reload hook.
        _init_model_registry(settings, ctx)
        _init_intelligence(settings, ctx)
        _init_shadow_runner(settings, ctx)
        _init_harvesters(settings, ctx)
        _init_content_analyzers(settings, ctx)
        _init_safety_classifier(settings, ctx)  # ONNX — always-on, replaces Llama Guard
        if settings.llama_guard_enabled:
            _init_llama_guard(settings, ctx)  # Optional Tier 3 — max accuracy when Ollama available
        if settings.prompt_guard_enabled:
            _init_prompt_guard(settings, ctx)
        if settings.presidio_pii_enabled:
            _init_presidio_pii(settings, ctx)
        if settings.dlp_enabled:
            _init_dlp_classifier(settings, ctx)
        if settings.image_ocr_enabled:
            _init_image_ocr(settings, ctx)
        _init_alert_bus(settings, ctx)
        _init_budget_tracker(settings, ctx)
        _init_session_chain(settings, ctx)
        if settings.rate_limit_enabled:
            _init_rate_limiter(settings, ctx)
        else:
            logger.warning("SECURITY: Rate limiting is DISABLED — enable WALACOR_RATE_LIMIT_ENABLED=true for production")
        if settings.tool_aware_enabled and settings.mcp_servers_json:
            await _init_tool_registry(settings, ctx)
        if settings.tool_aware_enabled and settings.web_search_enabled:
            await _init_web_search_tool(settings, ctx)
        if settings.otel_enabled:
            _init_otel(settings, ctx)
        if settings.export_enabled:
            await _init_audit_exporter(settings, ctx)
        if settings.control_plane_enabled:
            _init_control_plane(settings, ctx)
            await _auto_register_models(settings, ctx)
            # Seed default content policies if control plane is active
            if ctx.control_store:
                ctx.control_store.seed_default_content_policies()
        _init_semantic_cache(settings, ctx)
        _init_load_balancer(settings, ctx)
        # JWT configuration validation warnings
        if settings.auth_mode in ("jwt", "both"):
            if not settings.jwt_issuer:
                logger.warning("SECURITY: auth_mode=%s but jwt_issuer not set — any JWT issuer will be accepted", settings.auth_mode)
            if not settings.jwt_audience:
                logger.warning("SECURITY: auth_mode=%s but jwt_audience not set — any JWT audience will be accepted", settings.auth_mode)
            if settings.jwt_secret and len(settings.jwt_secret) < 32:
                logger.error("SECURITY: jwt_secret is too short (%d chars) — use at least 32 characters for HS256", len(settings.jwt_secret))
        # Auto-generate API key if control plane is active but no keys configured
        if settings.control_plane_enabled and not settings.api_keys_list:
            import secrets
            auto_key = f"wgk-{secrets.token_urlsafe(32)}"
            settings.gateway_api_keys = auto_key
            logger.warning(
                "SECURITY: Control plane enabled without API keys. "
                "Auto-generated key: %s — set WALACOR_GATEWAY_API_KEYS to use your own.",
                auto_key,
            )
        # Phase 23: Startup probes (provider health, disk, routing)
        if settings.startup_probes_enabled:
            from gateway.adaptive.startup_probes import run_startup_probes
            ctx.startup_probe_results = await run_startup_probes(ctx.http_client, settings)
            # Apply disk auto-scaling
            disk_probe = ctx.startup_probe_results.get("disk_space")
            if disk_probe and disk_probe.detail.get("auto_max_gb") is not None:
                ctx.effective_wal_max_gb = disk_probe.detail["auto_max_gb"]
                logger.info("WAL max size auto-scaled to %.2f GB", ctx.effective_wal_max_gb)
        # Phase 23: Request classifier + identity validator
        from gateway.adaptive.request_classifier import DefaultRequestClassifier
        from gateway.adaptive.identity_validator import DefaultIdentityValidator
        ctx.request_classifier = DefaultRequestClassifier()
        ctx.identity_validator = DefaultIdentityValidator()
        # Load custom implementations if configured
        if settings.custom_request_classifiers:
            from gateway.adaptive import load_custom_class, parse_custom_paths
            paths = parse_custom_paths(settings.custom_request_classifiers)
            if paths:
                try:
                    cls = load_custom_class(paths[0])
                    ctx.request_classifier = cls()
                except Exception as e:
                    logger.warning("Failed to load custom classifier %s: %s", paths[0], e)
        if settings.custom_identity_validators:
            from gateway.adaptive import load_custom_class, parse_custom_paths
            paths = parse_custom_paths(settings.custom_identity_validators)
            if paths:
                try:
                    cls = load_custom_class(paths[0])
                    ctx.identity_validator = cls()
                except Exception as e:
                    logger.warning("Failed to load custom validator %s: %s", paths[0], e)
        # Phase 23: Capability registry + resource monitor
        from gateway.adaptive.capability_registry import CapabilityRegistry
        from gateway.adaptive.resource_monitor import DefaultResourceMonitor
        ctx.capability_registry = CapabilityRegistry(
            ttl_seconds=settings.capability_probe_ttl_seconds,
            control_store=ctx.control_store)
        if settings.disk_monitor_enabled:
            ctx.resource_monitor = DefaultResourceMonitor(
                wal_path=settings.wal_path,
                min_free_pct=settings.disk_min_free_percent)
        await _self_test()
        # Phase 23: Resource monitor background task
        if ctx.resource_monitor and settings.disk_monitor_enabled:
            async def _resource_monitor_loop():
                while True:
                    await asyncio.sleep(settings.resource_monitor_interval_seconds)
                    try:
                        status = await ctx.resource_monitor.check()
                        if not status.disk_healthy:
                            logger.warning("Resource monitor: disk %.1f%% free", status.disk_free_pct)
                    except Exception as e:
                        logger.debug("Resource monitor check failed: %s", e)
            ctx.resource_monitor_task = asyncio.create_task(_resource_monitor_loop())
        # Start alert bus background consumer
        if ctx.alert_bus and ctx.alert_bus._dispatchers:
            ctx.alert_bus_task = asyncio.create_task(ctx.alert_bus.run())
        if ctx.sync_client:
            ctx.sync_loop_task = asyncio.create_task(_run_sync_loop(settings, ctx))
        elif settings.control_plane_enabled:
            # Local sync loop keeps policy_cache.fetched_at fresh (fixes fail_closed)
            from gateway.control.loader import _run_local_sync_loop
            ctx.local_sync_task = asyncio.create_task(_run_local_sync_loop(settings, ctx))
        # Phase 24: Merkle tree checkpoint background task
        if settings.merkle_checkpoint_enabled:
            ctx.merkle_checkpoint_task = asyncio.create_task(
                _merkle_checkpoint_loop(ctx, settings.merkle_checkpoint_interval_seconds)
            )
        # Event loop lag monitor (RED metrics)
        ctx.event_loop_lag_task = asyncio.create_task(_event_loop_lag_monitor())
        # Multimodal audit: attachment notification cache
        if settings.attachment_tracking_enabled:
            from gateway.middleware.attachment_tracker import AttachmentNotificationCache
            ctx.attachment_cache = AttachmentNotificationCache()
            logger.info("Attachment tracking cache enabled")
        # ── Schema Intelligence v2: SchemaMapper + Anomaly + Overflow + LLM Intelligence ──
        try:
            from gateway.schema.mapper import SchemaMapper
            # Task 12: resolve model via registry. When `ctx.model_registry`
            # is set, the packaged-default load is skipped and the session
            # is built from `production/schema_mapper.onnx` on first
            # `map_response()` call (Task 11 reload hook).
            ctx.schema_mapper = SchemaMapper(
                verdict_buffer=ctx.verdict_buffer,
                registry=ctx.model_registry,
                model_name="schema_mapper" if ctx.model_registry is not None else None,
            )
            logger.info(
                "SchemaMapper initialized (ONNX=%s, registry=%s)",
                ctx.schema_mapper._session is not None,
                ctx.model_registry is not None,
            )
        except Exception as e:
            logger.warning("SchemaMapper init failed (non-fatal): %s", e)

        try:
            from gateway.intelligence.consistency import ConsistencyTracker
            ctx.consistency_tracker = ConsistencyTracker()
            logger.info("Consistency tracker initialized (AuditLLM passive mode)")
        except Exception as e:
            logger.warning("Consistency tracker init failed (non-fatal): %s", e)

        try:
            from gateway.schema.anomaly import AnomalyDetector
            ctx.anomaly_detector = AnomalyDetector()
            logger.info("Anomaly detector initialized")
        except Exception as e:
            logger.warning("Anomaly detector init failed (non-fatal): %s", e)

        try:
            from gateway.schema.overflow import FieldRegistry
            ctx.field_registry = FieldRegistry()
            logger.info("Field registry initialized")
        except Exception as e:
            logger.warning("Field registry init failed (non-fatal): %s", e)

        # Background LLM intelligence worker (only if Ollama is configured)
        if settings.gateway_provider == "ollama" or settings.provider_ollama_url:
            try:
                from gateway.intelligence.worker import IntelligenceWorker
                _ollama_url = settings.provider_ollama_url or "http://localhost:11434"
                ctx.intelligence_worker = IntelligenceWorker(
                    ollama_url=_ollama_url,
                    enabled=True,
                )
                ctx.intelligence_worker_task = asyncio.create_task(ctx.intelligence_worker.run())
                logger.info("Intelligence worker started (ollama=%s)", _ollama_url)

                # AuditLLM probe generator for active consistency testing
                from gateway.intelligence.consistency import ProbeGenerator
                ctx.probe_generator = ProbeGenerator(ollama_url=_ollama_url)
            except Exception as e:
                logger.warning("Intelligence worker init failed (non-fatal): %s", e)

        logger.info(
            "Gateway config: tenant=%s provider=%s auth_mode=%s enforcement=%s "
            "tool_aware=%s web_search=%s rate_limit=%s",
            settings.gateway_tenant_id, settings.gateway_provider, settings.auth_mode,
            settings.enforcement_mode, settings.tool_aware_enabled,
            settings.web_search_enabled, settings.rate_limit_enabled,
        )
        # Auto-install audit filter into OpenWebUI (non-blocking, fail-open)
        if settings.openwebui_url and settings.openwebui_api_key:
            try:
                from gateway.integrations.openwebui_setup import install_openwebui_filter
                # Determine the gateway's own URL for the filter to call back
                gateway_self_url = f"http://localhost:{os.environ.get('PORT', '8000')}"
                gw_api_key = (settings.gateway_api_keys or [""])[0] if settings.gateway_api_keys else ""
                ok = await install_openwebui_filter(
                    openwebui_url=settings.openwebui_url,
                    openwebui_api_key=settings.openwebui_api_key,
                    gateway_url=gateway_self_url,
                    gateway_api_key=gw_api_key,
                )
                if ok:
                    logger.info("OpenWebUI filter auto-installed at %s", settings.openwebui_url)
                else:
                    logger.warning("OpenWebUI filter auto-install failed (non-fatal)")
            except Exception:
                logger.warning("OpenWebUI filter auto-install skipped", exc_info=True)

        logger.info("Gateway startup complete: governance pipeline ready, WAL and delivery worker started")
    except Exception:
        logger.critical("Gateway startup FAILED — cleaning up partially initialized resources", exc_info=True)
        await on_shutdown()
        raise


async def on_shutdown() -> None:
    """Graceful shutdown: each step runs independently so one failure doesn't skip the rest."""
    ctx = get_pipeline_context()
    errors: list[str] = []

    if ctx.http_client:
        try:
            await ctx.http_client.aclose()
        except Exception as e:
            errors.append(f"http_client.aclose: {e}")
        ctx.http_client = None

    if ctx.sync_loop_task and not ctx.sync_loop_task.done():
        ctx.sync_loop_task.cancel()
        try:
            await ctx.sync_loop_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            errors.append(f"sync_loop_task: {e}")

    if ctx.delivery_worker:
        try:
            ctx.delivery_worker.stop()
        except Exception as e:
            # Data-loss risk: WAL records in flight may not be flushed.
            logger.error("Gateway shutdown: delivery_worker.stop failed — WAL records may not be flushed", exc_info=True)
            errors.append(f"delivery_worker.stop: {e}")

    if ctx.sync_client:
        try:
            await ctx.sync_client.close()
        except Exception as e:
            errors.append(f"sync_client.close: {e}")

    if ctx.batch_writer:
        try:
            await ctx.batch_writer.stop()
        except Exception as e:
            errors.append(f"batch_writer.stop: {e}")
        ctx.batch_writer = None

    if ctx.wal_writer:
        try:
            ctx.wal_writer.close()
        except Exception as e:
            # Data-loss risk: SQLite WAL file may be left in a partially-written state.
            logger.error("Gateway shutdown: wal_writer.close failed — WAL file may be corrupt", exc_info=True)
            errors.append(f"wal_writer.close: {e}")

    if ctx.walacor_client:
        try:
            await ctx.walacor_client.close()
        except Exception as e:
            errors.append(f"walacor_client.close: {e}")
        ctx.walacor_client = None

    if ctx.tool_registry:
        try:
            await ctx.tool_registry.shutdown()
        except Exception as e:
            errors.append(f"tool_registry.shutdown: {e}")
        ctx.tool_registry = None

    # Stop intelligence worker
    _intel_worker = getattr(ctx, "intelligence_worker", None)
    if _intel_worker:
        try:
            await _intel_worker.stop()
        except Exception as e:
            errors.append(f"intelligence_worker.stop: {e}")

    if ctx.redis_client:
        try:
            await ctx.redis_client.aclose()
        except Exception as e:
            errors.append(f"redis_client.aclose: {e}")
        ctx.redis_client = None

    if ctx.lineage_reader:
        try:
            ctx.lineage_reader.close()
        except Exception as e:
            errors.append(f"lineage_reader.close: {e}")
        ctx.lineage_reader = None

    if ctx.event_loop_lag_task and not ctx.event_loop_lag_task.done():
        ctx.event_loop_lag_task.cancel()
        try:
            await ctx.event_loop_lag_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            errors.append(f"event_loop_lag_task: {e}")

    if ctx.resource_monitor_task and not ctx.resource_monitor_task.done():
        ctx.resource_monitor_task.cancel()
        try:
            await ctx.resource_monitor_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            errors.append(f"resource_monitor_task: {e}")

    if ctx.local_sync_task and not ctx.local_sync_task.done():
        ctx.local_sync_task.cancel()
        try:
            await ctx.local_sync_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            errors.append(f"local_sync_task: {e}")

    if ctx.alert_bus_task and not ctx.alert_bus_task.done():
        ctx.alert_bus_task.cancel()
        try:
            await ctx.alert_bus_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            errors.append(f"alert_bus_task: {e}")

    if ctx.merkle_checkpoint_task and not ctx.merkle_checkpoint_task.done():
        ctx.merkle_checkpoint_task.cancel()
        try:
            await ctx.merkle_checkpoint_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            errors.append(f"merkle_checkpoint_task: {e}")

    # Phase 25: Intelligence flush worker — stop() flips the running flag,
    # await drains any in-flight tick. Short timeout so shutdown never hangs
    # on a stuck SQLite write.
    if ctx.intelligence_flush_worker:
        try:
            ctx.intelligence_flush_worker.stop()
        except Exception as e:
            errors.append(f"intelligence_flush_worker.stop: {e}")
    if ctx.intelligence_flush_task and not ctx.intelligence_flush_task.done():
        try:
            await asyncio.wait_for(ctx.intelligence_flush_task, timeout=2.0)
        except asyncio.TimeoutError:
            ctx.intelligence_flush_task.cancel()
            try:
                await ctx.intelligence_flush_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                errors.append(f"intelligence_flush_task: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            errors.append(f"intelligence_flush_task: {e}")
    ctx.intelligence_flush_task = None
    ctx.intelligence_flush_worker = None

    # Phase 25: Intelligence retention sweeper — same drain pattern as the
    # flush worker. stop() flips the running flag; the await drains the
    # in-flight sleep / sweep.
    if ctx.intelligence_retention_sweeper:
        try:
            ctx.intelligence_retention_sweeper.stop()
        except Exception as e:
            errors.append(f"intelligence_retention_sweeper.stop: {e}")
    if ctx.intelligence_retention_task and not ctx.intelligence_retention_task.done():
        try:
            await asyncio.wait_for(ctx.intelligence_retention_task, timeout=2.0)
        except asyncio.TimeoutError:
            ctx.intelligence_retention_task.cancel()
            try:
                await ctx.intelligence_retention_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                errors.append(f"intelligence_retention_task: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            errors.append(f"intelligence_retention_task: {e}")
    ctx.intelligence_retention_task = None
    ctx.intelligence_retention_sweeper = None

    # Phase 25 Task 13: drain the harvester runner. stop() injects a
    # sentinel so the background task wakes from its queue-get and exits;
    # a cancel fallback handles the pathologically-full queue case.
    if ctx.harvester_runner is not None:
        try:
            await asyncio.wait_for(ctx.harvester_runner.stop(), timeout=2.0)
        except asyncio.TimeoutError:
            errors.append("harvester_runner: stop timed out")
        except Exception as e:
            errors.append(f"harvester_runner: {e}")
        ctx.harvester_runner = None

    ctx.intelligence_db = None
    ctx.verdict_buffer = None

    if ctx.control_store:
        try:
            ctx.control_store.close()
        except Exception as e:
            errors.append(f"control_store.close: {e}")
        ctx.control_store = None

    if ctx.audit_exporter:
        try:
            await ctx.audit_exporter.close()
        except Exception as e:
            errors.append(f"audit_exporter.close: {e}")
        ctx.audit_exporter = None

    if errors:
        logger.warning("Gateway shutdown completed with errors: %s", "; ".join(errors))
    else:
        logger.info("Gateway shutdown complete")


def create_app() -> Starlette:
    _static_dir = Path(__file__).parent / "lineage" / "static"
    routes: list = [
        Route("/", _root_redirect, methods=["GET"]),
        Route("/health", health_response, methods=["GET"]),
        Route("/metrics", metrics_response, methods=["GET"]),
        # Lineage API
        Route("/v1/lineage/sessions", lineage_sessions, methods=["GET"]),
        Route("/v1/lineage/sessions/{session_id:path}", lineage_session_timeline, methods=["GET"]),
        Route("/v1/lineage/executions/{execution_id:path}", lineage_execution, methods=["GET"]),
        Route("/v1/lineage/attempts", lineage_attempts, methods=["GET"]),
        Route("/v1/lineage/metrics", lineage_metrics_history, methods=["GET"]),
        Route("/v1/lineage/token-latency", lineage_token_latency_history, methods=["GET"]),
        Route("/v1/lineage/trace/{execution_id:path}", lineage_trace, methods=["GET"]),
        Route("/v1/lineage/verify/{session_id:path}", lineage_verify, methods=["GET"]),
        Route("/v1/lineage/cost", lineage_cost_summary, methods=["GET"]),
        Route("/v1/lineage/attachments", lineage_attachments, methods=["GET"]),
        Route("/v1/lineage/ab-tests/{test_name}/results", lineage_ab_test_results, methods=["GET"]),
        # Control plane CRUD
        Route("/v1/control/attestations", control_list_attestations, methods=["GET"]),
        Route("/v1/control/attestations", control_upsert_attestation, methods=["POST"]),
        Route("/v1/control/attestations/{id:path}", control_delete_attestation, methods=["DELETE"]),
        Route("/v1/control/policies", control_list_policies, methods=["GET"]),
        Route("/v1/control/policies", control_create_policy, methods=["POST"]),
        Route("/v1/control/policies/{id:path}", control_update_policy, methods=["PUT"]),
        Route("/v1/control/policies/{id:path}", control_delete_policy, methods=["DELETE"]),
        Route("/v1/control/budgets", control_list_budgets, methods=["GET"]),
        Route("/v1/control/budgets", control_upsert_budget, methods=["POST"]),
        Route("/v1/control/budgets/{id:path}", control_delete_budget, methods=["DELETE"]),
        Route("/v1/control/content-policies", control_list_content_policies, methods=["GET"]),
        Route("/v1/control/content-policies", control_upsert_content_policy, methods=["POST"]),
        Route("/v1/control/content-policies/{policy_id:path}", control_delete_content_policy, methods=["DELETE"]),
        Route("/v1/control/pricing", control_list_pricing, methods=["GET"]),
        Route("/v1/control/pricing", control_upsert_pricing, methods=["POST"]),
        Route("/v1/control/pricing/{id:path}", control_delete_pricing, methods=["DELETE"]),
        Route("/v1/control/status", control_status, methods=["GET"]),
        Route("/v1/control/discover", control_discover_models, methods=["GET"]),
        Route("/v1/control/templates", control_list_templates, methods=["GET"]),
        Route("/v1/control/templates/{name}/apply", control_apply_template, methods=["POST"]),
        Route("/v1/control/keys/assignments", control_list_key_policy_assignments, methods=["GET"]),
        Route("/v1/control/keys/{key_hash}/policies", control_get_key_policies, methods=["GET"]),
        Route("/v1/control/keys/{key_hash}/policies", control_set_key_policies, methods=["PUT"]),
        Route("/v1/control/keys/{key_hash}/policies/{policy_id:path}", control_remove_key_policy, methods=["DELETE"]),
        Route("/v1/control/keys/{key_hash}/tools", control_get_key_tools, methods=["GET"]),
        Route("/v1/control/keys/{key_hash}/tools", control_set_key_tools, methods=["PUT"]),
        Route("/v1/control/keys/{key_hash}/tools/{tool_name:path}", control_remove_key_tool, methods=["DELETE"]),
        # Phase 25 Task 26: intelligence read endpoints
        Route("/v1/control/intelligence/models", intel_list_production_models, methods=["GET"]),
        Route("/v1/control/intelligence/candidates", intel_list_candidates, methods=["GET"]),
        Route("/v1/control/intelligence/history/{model}", intel_model_history, methods=["GET"]),
        # Sync-contract endpoints (for fleet sync)
        Route("/v1/attestation-proofs", sync_attestation_proofs, methods=["GET"]),
        Route("/v1/policies", sync_policies, methods=["GET"]),
        # OpenWebUI integration
        Route("/v1/openwebui/status", openwebui_status, methods=["GET"]),
        Route("/v1/openwebui/events", openwebui_events_receive, methods=["POST"]),
        Route("/v1/openwebui/events", openwebui_events_list, methods=["GET"]),
        # Models API (OpenAI-compatible)
        Route("/v1/models", list_models, methods=["GET"]),
        # Compliance export
        Route("/v1/compliance/export", compliance_export, methods=["GET"]),
        # Attachment tracking webhook
        Route("/v1/attachments/notify", _attachment_notify, methods=["POST"]),
        # Proxy routes
        Route("/v1/chat/completions", catch_all_post, methods=["POST"]),
        Route("/v1/chat/completions/", catch_all_post, methods=["POST"]),
        Route("/v1/completions", catch_all_post, methods=["POST"]),
        Route("/v1/completions/", catch_all_post, methods=["POST"]),
        Route("/v1/messages", catch_all_post, methods=["POST"]),
        Route("/v1/messages/", catch_all_post, methods=["POST"]),
        Route("/v1/custom", catch_all_post, methods=["POST"]),
        Route("/v1/custom/", catch_all_post, methods=["POST"]),
        Route("/generate", catch_all_post, methods=["POST"]),
    ]
    # Lineage dashboard static files (only if directory exists in package)
    if _static_dir.is_dir():
        routes.append(Mount("/lineage", app=StaticFiles(directory=str(_static_dir), html=True)))
    @asynccontextmanager
    async def lifespan(app):
        await on_startup()
        yield
        await on_shutdown()

    app = Starlette(debug=False, routes=routes, lifespan=lifespan)
    # Middleware order: last registered = outermost (first to run).
    # CORS first so OPTIONS preflight succeeds for browser clients.
    app.add_middleware(BaseHTTPMiddleware, dispatch=cors_middleware)
    # Security headers (XSS, clickjack, MIME-sniff) on every response; CSP on /lineage/ only.
    app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers_middleware)
    # Body size limit runs outside auth so oversized requests are rejected early (H5).
    app.add_middleware(BaseHTTPMiddleware, dispatch=body_size_middleware)
    # api_key runs inside completeness so denied_auth attempts are always recorded.
    app.add_middleware(BaseHTTPMiddleware, dispatch=api_key_middleware)
    # Token rate limiter runs inside api_key (auth already checked) but outside
    # completeness so 429 responses are recorded as attempts.
    settings = get_settings()
    if settings.token_rate_limit_enabled:
        from gateway.middleware.token_rate_limiter import TokenRateLimiter
        app.add_middleware(
            TokenRateLimiter,
            max_tokens=settings.token_rate_limit_max_tokens,
            window_seconds=settings.token_rate_limit_window,
            scope=settings.token_rate_limit_scope,
            enabled=True,
        )
        logger.info(
            "Token rate limiter enabled: max_tokens=%d window=%ds scope=%s",
            settings.token_rate_limit_max_tokens,
            settings.token_rate_limit_window,
            settings.token_rate_limit_scope,
        )
    app.add_middleware(BaseHTTPMiddleware, dispatch=completeness_middleware)
    return app


app = create_app()


def main() -> None:
    import uvicorn
    settings = get_settings()

    try:
        import uvloop  # noqa: F401
        loop = "uvloop"
    except ImportError:
        loop = "auto"

    uvicorn.run(
        "gateway.main:app",
        host=settings.gateway_host,
        port=settings.gateway_port,
        log_level=settings.log_level.lower(),
        loop=loop,
        workers=settings.uvicorn_workers,
    )


if __name__ == "__main__":
    main()
