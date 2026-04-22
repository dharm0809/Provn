"""Environment-based configuration with pydantic-settings. Fail-fast on invalid/missing required vars."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WALACOR_",
        env_file=(".env", ".env.gateway"),
        extra="ignore",
    )

    # Parsed once at construction time; not an env var (Finding 5)
    _parsed_model_routes: list[dict] = PrivateAttr(default_factory=list)

    # Required when governance is on (skip_governance=False)
    gateway_tenant_id: str = Field(default="", description="Tenant this gateway serves (single-tenant V1)")
    control_plane_url: str = Field(default="", description="Base URL of the control plane")

    # Auth
    gateway_api_keys: str = Field(default="", description="Comma-separated API keys for caller auth")
    control_plane_api_key: str = Field(default="", description="API key for gateway→control plane (X-API-Key or Bearer); required when control plane has WALACOR_API_KEYS")

    # Phase 21: JWT/SSO authentication
    auth_mode: str = Field(default="api_key", description="Auth mode: 'api_key' (default), 'jwt', or 'both'")
    jwt_secret: str = Field(default="", description="Shared secret for HS256 JWT validation")
    jwt_jwks_url: str = Field(default="", description="JWKS endpoint for RS256/ES256 (OIDC providers)")
    jwt_issuer: str = Field(default="", description="Expected JWT issuer (iss claim)")
    jwt_audience: str = Field(default="", description="Expected JWT audience (aud claim)")
    jwt_algorithms: str = Field(default="RS256", description="Comma-separated JWT algorithms (RS256,ES256,HS256)")
    jwt_user_claim: str = Field(default="sub", description="JWT claim for user ID")
    jwt_email_claim: str = Field(default="email", description="JWT claim for email")
    jwt_roles_claim: str = Field(default="roles", description="JWT claim for roles")
    jwt_team_claim: str = Field(default="", description="JWT claim for team (empty = disabled)")

    # Optional identity
    gateway_id: str = Field(default_factory=lambda: f"gw-{uuid.uuid4().hex[:12]}", description="Unique gateway instance ID")

    # Cache
    attestation_cache_ttl: int = Field(default=300, description="Attestation cache TTL seconds")
    policy_staleness_threshold: int = Field(default=900, description="Max policy staleness before fail-closed (seconds)")
    sync_interval: int = Field(default=60, description="Pull sync interval seconds")

    # WAL
    wal_path: str = Field(default="/var/walacor/wal", description="WAL storage directory")
    wal_max_size_gb: float = Field(default=10.0, description="Max WAL disk usage GB before action")
    wal_max_age_hours: float = Field(default=72.0, description="Max WAL record age hours before action")
    wal_high_water_mark: int = Field(default=10000, description="Max undelivered records before rejecting new requests (enforced mode)")
    max_stream_buffer_bytes: int = Field(default=10_485_760, description="Max stream buffer for hashing (10MB)")
    wal_batch_enabled: bool = Field(default=True, description="Enable group commit batching for WAL writes")
    wal_batch_flush_ms: int = Field(default=10, description="Max flush delay in milliseconds")
    wal_batch_max_size: int = Field(default=50, description="Max records per batch before immediate flush")

    # Completeness Invariant (Phase 9)
    completeness_enabled: bool = Field(default=True, description="Enable gateway_attempts completeness tracking")
    attempts_retention_hours: float = Field(default=168.0, description="Retention for attempt records in hours (7 days)")

    # Phase 10: Response policy / content analysis
    response_policy_enabled: bool = Field(default=True, description="Enable post-inference content analysis")
    pii_detection_enabled: bool = Field(default=True, description="Enable built-in PII detector (walacor.pii.v1)")
    toxicity_detection_enabled: bool = Field(default=True, description="Enable built-in toxicity detector (walacor.toxicity.v1)")
    toxicity_deny_terms: str = Field(default="", description="Comma-separated extra deny-list terms for toxicity detector")

    # Stage B.7: Parallel content analysis
    content_analysis_parallel: bool = Field(
        default=True,
        description="Run input content analysis in parallel with LLM call (reduces latency for slow analyzers)",
    )

    # PII sanitization (strip-before-LLM, restore-after) — Stage B.1
    pii_sanitization_enabled: bool = Field(
        default=False,
        description="Strip high-risk PII from prompt before sending to LLM, restore in response. Requires pii_detection_enabled. Off by default.",
    )
    pii_sanitization_mode: str = Field(
        default="replace",
        description="Sanitization mode: 'replace' (placeholder tokens) or 'redact' (remove entirely, no restore).",
    )
    pii_sanitization_types: str = Field(
        default="SSN,CREDIT_CARD,AWS_ACCESS_KEY,API_KEY",
        description="Comma-separated PII types to sanitize before forwarding to LLM.",
    )

    # Phase 28: Prompt caching
    prompt_caching_enabled: bool = Field(
        default=True,
        description="Auto-inject Anthropic cache_control breakpoints on system messages.",
    )

    # Phase 17: Reasoning model support
    thinking_strip_enabled: bool = Field(
        default=True,
        description="Strip <think>...</think> reasoning tokens from Ollama responses before audit record.",
    )

    # Phase 17: Llama Guard safety classifier
    llama_guard_enabled: bool = Field(
        default=True,
        description="Enable Llama Guard 3 content analyzer (requires ollama pull llama-guard3:1b).",
    )
    llama_guard_model: str = Field(
        default="llama-guard3:1b",
        description="Ollama model for Llama Guard inference (1b is 5x faster than 8b).",
    )
    llama_guard_ollama_url: str = Field(
        default="",
        description="Ollama URL for Llama Guard inference. Defaults to WALACOR_PROVIDER_OLLAMA_URL if empty.",
    )
    llama_guard_timeout_ms: int = Field(
        default=1500,
        description="Llama Guard inference timeout in ms (inference takes 500ms–2s; default 1500ms).",
    )

    # Phase 23: Presidio NER PII detection
    presidio_pii_enabled: bool = Field(
        default=False,
        description="Enable Presidio NER PII analyzer (requires pip install 'walacor-gateway[presidio]')",
    )

    # System task filtering: skip audit for OpenWebUI auto-generated requests (tags, suggestions, titles)
    skip_system_task_audit: bool = Field(default=False, description="Skip audit records for system-generated requests (tag/title/suggestion generation)")

    # Multimodal audit: attachment tracking
    attachment_tracking_enabled: bool = Field(default=True, description="Track file/image metadata in execution records")

    # Multimodal audit: image OCR + PII detection
    image_ocr_enabled: bool = Field(default=False, description="Enable Tesseract OCR + PII detection on images")
    image_ocr_max_size_mb: int = Field(default=10, description="Skip OCR for images larger than this (MB)")

    # Prompt injection detection
    prompt_guard_enabled: bool = Field(
        default=False,
        description="Enable Prompt Guard 2 prompt injection detector (requires pip install 'walacor-gateway[guard]').",
    )
    prompt_guard_model: str = Field(
        default="meta-llama/Prompt-Guard-2-22M",
        description="HuggingFace model ID for Prompt Guard 2 (22M or 86M variant).",
    )
    prompt_guard_threshold: float = Field(
        default=0.9,
        description="Classification threshold for injection detection (0.0-1.0).",
    )

    # B.8: DLP data classification
    dlp_enabled: bool = Field(
        default=True,
        description="Enable DLP data classification for financial, health, secrets, and infrastructure data",
    )
    dlp_categories: str = Field(
        default="financial,health,secrets,infrastructure",
        description="Comma-separated DLP categories to scan (financial, health, secrets, infrastructure)",
    )
    dlp_action_financial: str = Field(
        default="warn",
        description="Action for financial data detection: warn or block",
    )
    dlp_action_health: str = Field(
        default="block",
        description="Action for health/PHI data detection (HIPAA): warn or block",
    )
    dlp_action_secrets: str = Field(
        default="block",
        description="Action for secrets/private keys detection: warn or block",
    )
    dlp_action_infrastructure: str = Field(
        default="warn",
        description="Action for infrastructure data detection: warn or block",
    )

    # Phase 11: Token budget
    token_budget_enabled: bool = Field(default=False, description="Enable token budget enforcement")
    token_budget_period: str = Field(default="monthly", description="Budget period: 'daily' or 'monthly'")
    token_budget_max_tokens: int = Field(default=0, description="Max tokens per period per tenant (0 = unlimited)")

    # B.6: Token-based rate limiting
    token_rate_limit_enabled: bool = Field(default=False, description="Enable token-based rate limiting (sliding window per scope)")
    token_rate_limit_window: int = Field(default=60, description="Rate limit window in seconds")
    token_rate_limit_max_tokens: int = Field(default=100000, description="Max tokens per window per scope")
    token_rate_limit_scope: str = Field(default="user", description="Rate limit scope: user, key, tenant, global")

    # Phase 26: Rate limiting
    rate_limit_enabled: bool = Field(default=True, description="Enable request rate limiting")
    rate_limit_rpm: int = Field(default=120, description="Requests per minute limit")
    rate_limit_per_model: bool = Field(default=True, description="Rate limit per user+model (vs per user only)")
    ip_rate_limit_rpm: int = Field(default=300, description="Per-IP pre-auth rate limit (requests per minute)")

    # Phase 26: Alerting
    webhook_urls: str = Field(default="", description="Comma-separated webhook URLs for alerts")
    pagerduty_routing_key: str = Field(default="", description="PagerDuty Events API v2 routing key")
    alert_budget_thresholds: str = Field(default="70,90,100", description="Comma-separated budget usage % thresholds for alerts")

    # Phase 13: Session chain integrity
    session_chain_enabled: bool = Field(default=True, description="Enable Merkle chain for session records (G5)")
    session_chain_max_sessions: int = Field(default=10000, description="Max concurrent sessions tracked in memory")
    session_chain_ttl: int = Field(default=3600, description="Session state TTL seconds (evict inactive sessions)")

    # Phase 11: Adaptive concurrency limiting (Gradient2)
    adaptive_concurrency_enabled: bool = Field(default=True, description="Enable Gradient2 adaptive concurrency limiting")
    adaptive_concurrency_min: int = Field(default=5, description="Min concurrency limit per provider")
    adaptive_concurrency_max: int = Field(default=100, description="Max concurrency limit per provider")

    # Phase 23 (OpenWebUI integration): configurable session header names
    session_header_names: str = Field(
        default="X-Session-ID,X-OpenWebUI-Chat-Id,X-Chat-Id",
        description=(
            "Comma-separated list of request header names to check for session ID, "
            "in priority order. First non-empty match wins. Falls back to UUID. "
            "Allows OpenWebUI (X-OpenWebUI-Chat-Id), LibreChat, and custom UIs to "
            "share the same session chain semantics."
        ),
    )

    # Shadow policy mode
    shadow_policy_enabled: bool = Field(
        default=False,
        description="Enable shadow policy mode (log decisions without enforcing).",
    )

    # Policy engine selection (builtin vs OPA)
    policy_engine: str = Field(
        default="builtin",
        description="Policy engine: 'builtin' or 'opa'.",
    )
    opa_url: str = Field(
        default="http://localhost:8181",
        description="OPA REST API URL.",
    )
    opa_policy_path: str = Field(
        default="/v1/data/walacor/gateway/allow",
        description="OPA decision document path.",
    )
    opa_fail_closed: bool = Field(
        default=False,
        description="Block requests when OPA is unavailable (fail-closed)",
    )

    # Mode
    enforcement_mode: Literal["enforced", "audit_only"] = Field(default="enforced")
    skip_governance: bool = Field(default=False, description="If True, run as transparent proxy (Phase 1 only)")

    # Provider (for attestation cache key; default openai)
    gateway_provider: str = Field(default="openai", description="Provider name for attestation sync (openai, anthropic, etc.)")

    # Provider URLs and keys
    provider_openai_url: str = Field(default="https://api.openai.com", description="OpenAI API base URL")
    provider_openai_key: str = Field(default="", description="API key for OpenAI forwarding")
    provider_anthropic_url: str = Field(default="https://api.anthropic.com", description="Anthropic API base URL")
    provider_anthropic_key: str = Field(default="", description="API key for Anthropic")
    provider_anthropic_beta_headers: str = Field(
        default="",
        description=(
            "Comma-separated list of anthropic-beta header values to send on every "
            "Anthropic upstream request (e.g. 'prompt-caching-2024-07-31,"
            "extended-thinking-2025-02-19'). Empty means no beta header."
        ),
    )
    provider_huggingface_url: str = Field(default="", description="HuggingFace Inference Endpoints URL")
    provider_huggingface_key: str = Field(default="", description="HuggingFace API key")
    provider_ollama_url: str = Field(default="http://localhost:11434", description="Ollama base URL")
    provider_ollama_key: str = Field(default="", description="Ollama API key (usually empty for local)")
    generic_upstream_url: str = Field(default="", description="Generic adapter upstream URL")
    generic_model_path: str = Field(default="$.model", description="JSON path for model ID")
    generic_prompt_path: str = Field(default="$.messages[*].content", description="JSON path for prompt")
    generic_response_path: str = Field(default="$.choices[0].message.content", description="JSON path for response")
    generic_auto_detect: bool = Field(
        default=True,
        description=(
            "When true, GenericAdapter auto-detects OpenAI-compat, HuggingFace, and "
            "Ollama-native REST formats and enriches audit metadata. "
            "Manual WALACOR_GENERIC_*_PATH overrides remain as fallback."
        ),
    )
    ollama_digest_cache_ttl: int = Field(
        default=1800,
        description=(
            "TTL in seconds for the per-instance Ollama model digest cache. "
            "Set to 0 to disable caching (always fetches fresh from /api/show)."
        ),
    )

    # Walacor backend storage (replaces SQLite WAL when configured)
    # validation_alias bypasses env_prefix so the env vars are exactly:
    #   WALACOR_SERVER, WALACOR_USERNAME, WALACOR_PASSWORD
    walacor_server: str = Field(
        default="",
        description="Walacor backend server URL (e.g. https://sandbox.walacor.com/api)",
        validation_alias=AliasChoices("WALACOR_SERVER", "walacor_server"),
    )
    walacor_username: str = Field(
        default="",
        description="Walacor backend username",
        validation_alias=AliasChoices("WALACOR_USERNAME", "walacor_username"),
    )
    walacor_password: str = Field(
        default="",
        description="Walacor backend password",
        validation_alias=AliasChoices("WALACOR_PASSWORD", "walacor_password"),
    )
    walacor_executions_etid: int = Field(
        default=9000021,
        description="Walacor ETId for gateway execution records table (v2 schema with chain+tool fields)",
        validation_alias=AliasChoices("WALACOR_EXECUTIONS_ETID", "walacor_executions_etid"),
    )
    walacor_attempts_etid: int = Field(
        default=9000022,
        description="Walacor ETId for gateway attempts table",
        validation_alias=AliasChoices("WALACOR_ATTEMPTS_ETID", "walacor_attempts_etid"),
    )
    walacor_tool_events_etid: int = Field(
        default=9000023,
        description="Walacor ETId for gateway tool events table (v2 schema with sources+prompt_id)",
        validation_alias=AliasChoices("WALACOR_TOOL_EVENTS_ETID", "walacor_tool_events_etid"),
    )
    walacor_lifecycle_events_etid: int = Field(
        default=9000024,
        description="Walacor ETId for ONNX lifecycle event records (training fingerprint, candidate, shadow, promote, reject)",
        validation_alias=AliasChoices("WALACOR_LIFECYCLE_EVENTS_ETID", "walacor_lifecycle_events_etid"),
    )

    # Phase 14: Tool-aware gateway
    tool_aware_enabled: bool = Field(default=True, description="Enable tool-call awareness and auditing")
    tool_max_iterations: int = Field(
        default=10,
        description="Max tool-call loop iterations (guard against infinite loops)",
    )
    tool_loop_total_timeout_ms: int = Field(
        default=300_000,
        description="Total wall-clock timeout for all tool loop iterations (ms)",
    )
    tool_execution_timeout_ms: int = Field(
        default=30_000,
        description="Per-tool execution timeout in ms",
    )
    tool_max_output_bytes: int = Field(
        default=1_048_576,
        description="Max tool output size in bytes (default 1MB)",
    )
    tool_content_analysis_enabled: bool = Field(
        default=True,
        description="Run content analyzers on tool inputs/outputs",
    )
    mcp_servers_json: str = Field(
        default="",
        description="JSON array of MCP server configs, or path to a JSON file.",
    )
    mcp_allowed_commands: str = Field(
        default="",
        description=(
            "Comma-separated list of additional commands allowed for MCP stdio transport. "
            "Default allowlist: python, python3, python3.12, node, npx, uvx. "
            "Example: 'deno,bun' to also allow deno and bun."
        ),
    )
    web_search_enabled: bool = Field(
        default=True,
        description=(
            "Enable built-in web search tool. Gateway executes searches and injects results "
            "into the model context with full audit trail. Works with all providers."
        ),
    )
    web_search_provider: str = Field(
        default="duckduckgo",
        description="Web search backend: 'duckduckgo' (no key), 'brave' (API key, free tier), 'serpapi' (API key).",
    )
    web_search_api_key: str = Field(
        default="",
        description="API key for web search provider (required for 'brave' and 'serpapi').",
    )
    web_search_max_results: int = Field(
        default=5,
        description="Default number of search results returned per query.",
    )
    # Backward compat: old flags that mapped to web_search_enabled
    openai_web_search_enabled: bool = Field(default=False, description="Deprecated — use web_search_enabled")
    gateway_web_search_enabled: bool = Field(default=True, description="Deprecated — use web_search_enabled")
    tool_strategy: str = Field(default="auto", description="Deprecated — strategy is automatic per-request")

    # OpenWebUI auto-integration
    openwebui_url: str = Field(
        default="",
        description=(
            "OpenWebUI base URL (e.g. http://localhost:3000). When set, the Gateway "
            "automatically installs its audit filter plugin into OpenWebUI at startup."
        ),
    )
    openwebui_api_key: str = Field(
        default="",
        description="OpenWebUI admin API key for auto-installing the filter plugin.",
    )

    # Phase 15: Multi-model routing + Redis state sharing
    redis_url: str = Field(
        default="",
        description=(
            "Redis URL for shared session chain and budget state (multi-replica). "
            "E.g. redis://redis-svc:6379/0. When empty, in-memory trackers are used."
        ),
    )
    model_groups_json: str = Field(
        default="",
        description=(
            "JSON object or file path for model groups with weighted endpoints. "
            "Format: {\"gpt-4\": [{\"url\": \"https://...\", \"key\": \"sk-1\", \"weight\": 7}, ...]}. "
            "Enables load balancing, failover, and circuit-breaking across multiple endpoints per model."
        ),
    )
    model_routing_json: str = Field(
        default="",
        description=(
            "JSON array or file path. Each entry: "
            "{\"pattern\": \"gpt-*\", \"provider\": \"openai\", "
            "\"url\": \"https://api.openai.com\", \"key\": \"sk-...\"}. "
            "Checked before path-based routing. Supports fnmatch patterns. "
            "If value does not start with '[' or '{', treated as a file path."
        ),
    )

    @property
    def model_routes(self) -> list[dict]:
        """Return cached parsed model routing rules (parsed once at startup; Finding 5)."""
        return self._parsed_model_routes

    @property
    def walacor_storage_enabled(self) -> bool:
        """True when all three Walacor credentials are set."""
        return bool(self.walacor_server and self.walacor_username and self.walacor_password)

    # Network tuning
    max_request_body_mb: float = Field(default=50.0, description="Max request body size in MB (0 = unlimited)")
    provider_timeout: float = Field(default=300.0, description="Provider HTTP request timeout in seconds (300s default for CPU inference + tool loops + thinking models)")
    provider_connect_timeout: float = Field(default=10.0, description="Provider connection timeout in seconds")
    provider_max_connections: int = Field(default=200, description="Max concurrent provider connections")
    provider_max_keepalive: int = Field(default=50, description="Max keepalive provider connections")
    sse_keepalive_interval: float = Field(default=15.0, description="SSE keepalive ping interval in seconds")
    http_pool_max_connections: int = Field(default=100, description="Max HTTP connections in pool")
    http_pool_max_keepalive: int = Field(default=20, description="Max keepalive connections per host")
    http_keepalive_expiry: int = Field(default=30, description="Keepalive expiry in seconds")
    completeness_timeout: float = Field(
        default=2.0,
        description="Timeout in seconds for completeness middleware storage writes",
    )

    # Hedged requests (tail latency reduction)
    hedged_requests_enabled: bool = Field(default=False, description="Enable hedged cross-provider requests")
    hedge_delay_factor: float = Field(default=1.5, description="Hedge after p95_latency * this factor")

    # Resilience tuning
    delivery_batch_size: int = Field(default=50, description="WAL delivery batch size per cycle")
    circuit_breaker_fail_max: int = Field(default=5, description="Failures before circuit opens")
    circuit_breaker_reset_timeout: float = Field(default=30.0, description="Seconds before circuit half-open retry")
    retry_max_attempts: int = Field(default=3, description="Max forward retry attempts on transient errors")
    disk_degraded_threshold: float = Field(default=0.8, description="WAL disk usage threshold (0-1) for degraded status")

    # CORS
    cors_allowed_origins: str = Field(default="", description="Comma-separated CORS origins (empty = same-origin only, * = allow all)")

    # Server
    gateway_host: str = Field(default="0.0.0.0", description="Bind host for uvicorn")
    gateway_port: int = Field(default=8000, description="Bind port for uvicorn")
    uvicorn_workers: int = Field(default=1, description="Uvicorn worker processes. >1 disables in-memory session chain/budget sharing across workers — use 1 for single-node deployments with those features enabled")

    # Observability
    metrics_enabled: bool = Field(default=True, description="Enable Prometheus /metrics")
    log_level: str = Field(default="INFO", description="Logging level")

    # Phase 20: Embedded control plane
    control_plane_enabled: bool = Field(
        default=True,
        description="Enable embedded control plane (SQLite-backed CRUD + dashboard tab).",
    )
    control_plane_db_path: str = Field(
        default="",
        description="SQLite path for control plane state. Default: alongside WAL db.",
    )

    # Phase 18: Lineage dashboard
    lineage_enabled: bool = Field(
        default=True,
        description="Enable /lineage/ dashboard and /v1/lineage/* API endpoints.",
    )
    lineage_local_reader: bool = Field(
        default=True,
        description=(
            "When no Walacor client is configured, fall back to the SQLite-backed "
            "`LineageReader` reading directly from the local WAL database. Keeps the "
            "lineage dashboard functional in local-only / dev / CI deployments. "
            "Production with a real Walacor backend continues to use "
            "`WalacorLineageReader`; this flag is ignored in that case."
        ),
    )

    # Phase 17: OpenTelemetry export
    otel_enabled: bool = Field(
        default=False,
        description="Enable OpenTelemetry span export (requires pip install 'walacor-gateway[telemetry]').",
    )
    otel_endpoint: str = Field(
        default="http://localhost:4317",
        description="OTLP gRPC endpoint for trace export (e.g. Jaeger, Datadog, Grafana).",
    )
    otel_service_name: str = Field(
        default="walacor-gateway",
        description="OTel service.name resource attribute.",
    )

    # ── Audit log export (B.2) ────────────────────────────────────────────────
    export_enabled: bool = Field(default=False, description="Export audit records to external destination")
    export_type: str = Field(default="file", description="Export type: file, webhook (s3 requires boto3)")
    export_batch_size: int = Field(default=50, description="Batch size before exporting")
    export_flush_interval: int = Field(default=30, description="Max seconds between flushes")
    export_s3_bucket: str = Field(default="", description="S3 bucket name for S3 exporter")
    export_s3_prefix: str = Field(default="walacor-audit/", description="S3 key prefix")
    export_s3_region: str = Field(default="us-east-1", description="AWS region for S3 exporter")
    export_webhook_url: str = Field(default="", description="Webhook URL (Splunk HEC, Datadog, etc.)")
    export_webhook_headers: str = Field(default="", description="JSON dict of extra HTTP headers")
    export_file_path: str = Field(default="/var/walacor/export/audit.jsonl", description="JSONL output path")
    export_file_max_size_mb: int = Field(default=100, description="Max file size before rotation (MB)")

    # ── Phase 23: Adaptive Gateway ────────────────────────────────────────────
    startup_probes_enabled: bool = Field(default=True, description="Run startup probes (provider health, disk, routing)")
    provider_health_check_on_startup: bool = Field(default=True, description="Ping providers at startup")
    capability_probe_ttl_seconds: int = Field(default=86400, description="Re-probe model capabilities after this many seconds")
    identity_validation_enabled: bool = Field(default=True, description="Cross-validate JWT claims against headers")
    disk_monitor_enabled: bool = Field(default=True, description="Monitor WAL disk space")
    disk_min_free_percent: float = Field(default=5.0, description="Minimum free disk % before warning")
    resource_monitor_interval_seconds: int = Field(default=60, description="Resource monitor check interval")
    # Phase 8: OpenSSF model signing verification
    model_signing_enabled: bool = Field(
        default=False,
        description="Verify OpenSSF model signatures during discovery.",
    )

    # Phase 26: Ed25519 record signing
    record_signing_enabled: bool = Field(default=False, description="Sign record hashes with Ed25519 for non-repudiation")
    record_signing_key_path: str = Field(default="", description="Path to Ed25519 private key PEM file")

    # Phase 24: Periodic Merkle tree checkpoints
    merkle_checkpoint_enabled: bool = Field(default=True, description="Enable periodic Merkle tree checkpoints for session chains")
    merkle_checkpoint_interval_seconds: int = Field(default=3600, description="Seconds between Merkle tree checkpoint builds")

    # Transparency log publishing
    transparency_log_enabled: bool = Field(default=False, description="Publish Merkle checkpoint roots to external transparency log")
    transparency_log_url: str = Field(default="", description="Transparency log endpoint URL for POST requests")

    # ── B.4: Semantic caching (exact-match tier) ──────────────────────────────
    semantic_cache_enabled: bool = Field(
        default=True,
        description="Cache LLM responses for identical prompts (exact-match SHA-256 key)",
    )
    semantic_cache_ttl: int = Field(
        default=3600,
        description="Cache TTL in seconds",
    )
    semantic_cache_max_entries: int = Field(
        default=10000,
        description="Max cached entries (oldest-first eviction)",
    )
    semantic_cache_similarity_threshold: float = Field(
        default=0.95,
        description="Cosine similarity threshold (Phase 2 embedding cache, unused in exact-match mode)",
    )
    semantic_cache_embedding_model: str = Field(
        default="",
        description="Ollama model for embeddings (empty = exact-match only)",
    )

    # B.9: A/B model testing
    ab_tests_json: str = Field(
        default="",
        description=(
            'JSON array of A/B test configs. Example: '
            '[{"name":"size-test","model_pattern":"qwen3:*",'
            '"variants":[{"model":"qwen3:1.7b","weight":50},{"model":"qwen3:4b","weight":50}]}]. '
            "When a request model matches model_pattern, a variant is selected by weight and "
            "the model field is rewritten. ab_variant + ab_original_model are stored in metadata."
        ),
    )

    # Enterprise extension points (comma-separated Python dotted class paths)
    custom_startup_probes: str = Field(default="", description="Custom StartupProbe classes")
    custom_request_classifiers: str = Field(default="", description="Custom RequestClassifier classes")
    custom_identity_validators: str = Field(default="", description="Custom IdentityValidator classes")
    custom_resource_monitors: str = Field(default="", description="Custom ResourceMonitor classes")

    # ── Phase 25: Intelligence / ONNX Self-Learning ──────────────────────────
    intelligence_enabled: bool = Field(default=True, description="Enable ONNX intelligence layer (intent, schema, safety verdict capture and distillation)")
    intelligence_db_path: str = Field(default="", description="SQLite path for intelligence verdict store. Empty defaults to {wal_path}/intelligence.db")
    onnx_models_base_path: str = Field(default="", description="Base directory for ONNX model artifacts. Empty defaults to src/gateway/models/")
    verdict_retention_days: int = Field(default=30, ge=1, description="Retention for captured ONNX verdicts in days")
    distillation_schedule_cron: str = Field(default="0 2 * * *", description="Cron expression for nightly distillation job")
    distillation_min_divergences: int = Field(default=500, ge=1, description="Minimum student↔teacher divergences required to trigger distillation")
    shadow_sample_target: int = Field(default=1000, ge=1, description="Target sample size for shadow evaluation of a candidate model")
    shadow_min_accuracy_delta: float = Field(default=0.02, ge=0.0, le=1.0, description="Minimum accuracy improvement (vs. production) required to promote a candidate")
    shadow_max_disagreement: float = Field(default=0.40, ge=0.0, le=1.0, description="Maximum allowed disagreement rate (candidate vs. production) during shadow eval")
    shadow_max_error_rate: float = Field(default=0.05, ge=0.0, le=1.0, description="Maximum allowed inference error rate for a candidate during shadow eval")
    auto_promote_models: str = Field(default="", description="Comma-separated list of model names eligible for auto-promotion (empty = human-in-loop only)")
    teacher_llm_url: str = Field(default="", description="URL for teacher LLM used in distillation (empty = disabled)")
    teacher_llm_sample_rate: float = Field(default=0.01, ge=0.0, le=1.0, description="Fraction of requests sampled for teacher LLM labeling (0.0–1.0)")

    # ── Phase 26: Readiness self-check ───────────────────────────────────────
    readiness_enabled: bool = Field(default=True, description="Enable GET /v1/readiness endpoint")
    lineage_auth_required: bool = Field(default=True, description="Require API key on /v1/lineage/* endpoints")

    @property
    def auto_promote_models_list(self) -> list[str]:
        return [m.strip() for m in self.auto_promote_models.split(",") if m.strip()]

    @property
    def api_keys_list(self) -> list[str]:
        return [k.strip() for k in self.gateway_api_keys.split(",") if k.strip()]

    @property
    def jwt_algorithms_list(self) -> list[str]:
        return [a.strip() for a in self.jwt_algorithms.split(",") if a.strip()]

    @property
    def session_header_names_list(self) -> list[str]:
        """Parsed session header names in priority order."""
        return [h.strip() for h in self.session_header_names.split(",") if h.strip()]

    @model_validator(mode="after")
    def _parse_and_cache_model_routes(self) -> "Settings":
        """Parse model_routing_json once at construction and cache in _parsed_model_routes (Finding 5)."""
        if not self.model_routing_json:
            self._parsed_model_routes = []
            return self
        raw = self.model_routing_json.strip()
        if not raw.startswith(("[", "{")):
            path = Path(raw)
            if not path.exists():
                self._parsed_model_routes = []
                return self
            raw = path.read_text()
        try:
            data = json.loads(raw)
            self._parsed_model_routes = data if isinstance(data, list) else [data]
        except Exception:
            logging.getLogger(__name__).error(
                "WALACOR_MODEL_ROUTING_JSON parse failed — model routing disabled. Fix the JSON or remove the env var.",
                exc_info=True,
            )
            self._parsed_model_routes = []
        return self

    @model_validator(mode="after")
    def require_tenant_and_control_plane_when_governance_on(self) -> "Settings":
        if not self.skip_governance and not self.walacor_storage_enabled:
            # Embedded control plane (control_plane_enabled=True) removes the
            # hard requirement for a remote control_plane_url — models are
            # auto-attested and policies loaded from local SQLite.
            if self.control_plane_enabled:
                return self
            if not (self.gateway_tenant_id and self.control_plane_url):
                raise ValueError(
                    "WALACOR_GATEWAY_TENANT_ID and WALACOR_CONTROL_PLANE_URL are required "
                    "when skip_governance is False, control_plane_enabled is False, "
                    "and Walacor storage is not configured"
                )
        return self

    @model_validator(mode="after")
    def _validate_provider_urls(self) -> "Settings":
        for field_name in (
            "provider_openai_url",
            "provider_anthropic_url",
            "provider_ollama_url",
            "provider_huggingface_url",
            "generic_upstream_url",
        ):
            url = getattr(self, field_name, "") or ""
            if url and urlparse(url).scheme not in ("http", "https", ""):
                raise ValueError(
                    f"{field_name} must use http or https scheme, got: {url}"
                )
        return self


from functools import lru_cache


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
