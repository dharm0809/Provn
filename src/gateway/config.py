"""Environment-based configuration with pydantic-settings. Fail-fast on invalid/missing required vars."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Literal

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

    # Completeness Invariant (Phase 9)
    completeness_enabled: bool = Field(default=True, description="Enable gateway_attempts completeness tracking")
    attempts_retention_hours: float = Field(default=168.0, description="Retention for attempt records in hours (7 days)")

    # Phase 10: Response policy / content analysis
    response_policy_enabled: bool = Field(default=True, description="Enable post-inference content analysis")
    pii_detection_enabled: bool = Field(default=True, description="Enable built-in PII detector (walacor.pii.v1)")
    toxicity_detection_enabled: bool = Field(default=False, description="Enable built-in toxicity detector (walacor.toxicity.v1)")
    toxicity_deny_terms: str = Field(default="", description="Comma-separated extra deny-list terms for toxicity detector")

    # Phase 17: Reasoning model support
    thinking_strip_enabled: bool = Field(
        default=True,
        description="Strip <think>...</think> reasoning tokens from Ollama responses before audit record.",
    )

    # Phase 17: Llama Guard safety classifier
    llama_guard_enabled: bool = Field(
        default=True,
        description="Enable Llama Guard 3 content analyzer (requires ollama pull llama-guard3).",
    )
    llama_guard_model: str = Field(
        default="llama-guard3",
        description="Ollama model name for Llama Guard 3 inference.",
    )
    llama_guard_ollama_url: str = Field(
        default="",
        description="Ollama URL for Llama Guard inference. Defaults to WALACOR_PROVIDER_OLLAMA_URL if empty.",
    )
    llama_guard_timeout_ms: int = Field(
        default=5000,
        description="Llama Guard inference timeout in ms (inference takes 500ms–2s; default 5000ms).",
    )

    # Phase 11: Token budget
    token_budget_enabled: bool = Field(default=False, description="Enable token budget enforcement")
    token_budget_period: str = Field(default="monthly", description="Budget period: 'daily' or 'monthly'")
    token_budget_max_tokens: int = Field(default=0, description="Max tokens per period per tenant (0 = unlimited)")

    # Phase 13: Session chain integrity
    session_chain_enabled: bool = Field(default=True, description="Enable Merkle chain for session records (G5)")
    session_chain_max_sessions: int = Field(default=10000, description="Max concurrent sessions tracked in memory")
    session_chain_ttl: int = Field(default=3600, description="Session state TTL seconds (evict inactive sessions)")

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
        default=9000001,
        description="Walacor ETId for gateway execution records table",
        validation_alias=AliasChoices("WALACOR_EXECUTIONS_ETID", "walacor_executions_etid"),
    )
    walacor_attempts_etid: int = Field(
        default=9000002,
        description="Walacor ETId for gateway attempts table",
        validation_alias=AliasChoices("WALACOR_ATTEMPTS_ETID", "walacor_attempts_etid"),
    )
    walacor_tool_events_etid: int = Field(
        default=9000003,
        description="Walacor ETId for gateway tool event records table",
        validation_alias=AliasChoices("WALACOR_TOOL_EVENTS_ETID", "walacor_tool_events_etid"),
    )

    # Phase 14: Tool-aware gateway
    tool_aware_enabled: bool = Field(default=False, description="Enable tool-call awareness and auditing (Phase 14)")
    tool_strategy: str = Field(
        default="auto",
        description="Tool strategy: 'auto' (detect from provider), 'passive', 'active', or 'disabled'",
    )
    tool_max_iterations: int = Field(
        default=10,
        description="Max tool-call loop iterations for the active strategy (guard against infinite loops)",
    )
    tool_execution_timeout_ms: int = Field(
        default=30_000,
        description="Per-tool execution timeout in ms (active strategy)",
    )
    tool_content_analysis_enabled: bool = Field(
        default=True,
        description="Run content analyzers on tool inputs/outputs (active strategy)",
    )
    mcp_servers_json: str = Field(
        default="",
        description="JSON array of MCP server configs, or path to a JSON file. Required for active strategy.",
    )
    web_search_enabled: bool = Field(
        default=False,
        description=(
            "Enable built-in web search tool for local/private models (Ollama active strategy). "
            "When enabled, the 'web_search' tool is auto-registered and injected into Ollama requests."
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

    @property
    def api_keys_list(self) -> list[str]:
        return [k.strip() for k in self.gateway_api_keys.split(",") if k.strip()]

    @property
    def jwt_algorithms_list(self) -> list[str]:
        return [a.strip() for a in self.jwt_algorithms.split(",") if a.strip()]

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


from functools import lru_cache


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
