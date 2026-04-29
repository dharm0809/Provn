# Gateway configuration

All configuration is via environment variables (prefix `WALACOR_`). The gateway loads `.env` or `.env.gateway` from the current working directory if present. Values in the environment override file values.

## Required variables

When `WALACOR_SKIP_GOVERNANCE` is false (default) and `WALACOR_CONTROL_PLANE_ENABLED` is false, the following are required:

| Variable | Description |
|----------|-------------|
| `WALACOR_GATEWAY_TENANT_ID` | Tenant this gateway serves (single-tenant V1) |
| `WALACOR_CONTROL_PLANE_URL` | Base URL of a remote control plane |

When `WALACOR_CONTROL_PLANE_ENABLED=true` (default), governance works without a remote control plane — models are auto-attested on first use and policies are managed locally via the embedded control plane.

When `WALACOR_SKIP_GOVERNANCE=true`, both can be omitted (transparent proxy only).

## Identity and auth

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_GATEWAY_TENANT_ID` | (empty) | Tenant this gateway serves |
| `WALACOR_GATEWAY_ID` | auto (gw-&lt;uuid&gt;) | Unique gateway instance ID |
| `WALACOR_GATEWAY_API_KEYS` | (empty) | Comma-separated API keys for caller auth |
| `WALACOR_CONTROL_PLANE_API_KEY` | (empty) | API key for gateway→control plane (X-API-Key / Bearer) |
| `WALACOR_SKIP_GOVERNANCE` | false | If true, run as transparent proxy only |
| `WALACOR_ENFORCEMENT_MODE` | enforced | `enforced` or `audit_only` |

## Walacor backend storage

When all three credentials are set, records go to Walacor backend AND local WAL (dual-write).

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_SERVER` | (empty) | Walacor backend URL (e.g. `https://sandbox.walacor.com/api`) |
| `WALACOR_USERNAME` | (empty) | Walacor backend username |
| `WALACOR_PASSWORD` | (empty) | Walacor backend password |
| `WALACOR_EXECUTIONS_ETID` | 9000001 | ETId for execution records table |
| `WALACOR_ATTEMPTS_ETID` | 9000002 | ETId for attempts table |
| `WALACOR_TOOL_EVENTS_ETID` | 9000003 | ETId for tool event records table |

## Provider URLs and keys

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_GATEWAY_PROVIDER` | openai | Default provider for path-based routing |
| `WALACOR_PROVIDER_OPENAI_URL` | https://api.openai.com | OpenAI API base URL |
| `WALACOR_PROVIDER_OPENAI_KEY` | (empty) | API key for OpenAI |
| `WALACOR_PROVIDER_ANTHROPIC_URL` | https://api.anthropic.com | Anthropic API base URL |
| `WALACOR_PROVIDER_ANTHROPIC_KEY` | (empty) | API key for Anthropic |
| `WALACOR_PROVIDER_OLLAMA_URL` | http://localhost:11434 | Ollama base URL |
| `WALACOR_PROVIDER_OLLAMA_KEY` | (empty) | Ollama API key (usually empty for local) |
| `WALACOR_PROVIDER_HUGGINGFACE_URL` | (empty) | HuggingFace Inference Endpoints URL |
| `WALACOR_PROVIDER_HUGGINGFACE_KEY` | (empty) | HuggingFace API key |
| `WALACOR_GENERIC_UPSTREAM_URL` | (empty) | Generic adapter upstream URL |
| `WALACOR_GENERIC_MODEL_PATH` | $.model | JSON path for model ID |
| `WALACOR_GENERIC_PROMPT_PATH` | $.messages[*].content | JSON path for prompt |
| `WALACOR_GENERIC_RESPONSE_PATH` | $.choices[0].message.content | JSON path for response |

## Cache and sync

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_ATTESTATION_CACHE_TTL` | 300 | Attestation cache TTL (seconds) |
| `WALACOR_POLICY_STALENESS_THRESHOLD` | 900 | Max policy staleness before fail-closed (seconds) |
| `WALACOR_SYNC_INTERVAL` | 60 | Pull sync interval seconds |

## WAL (local SQLite audit log)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_WAL_PATH` | /var/walacor/wal | WAL storage directory |
| `WALACOR_WAL_MAX_SIZE_GB` | 10 | Max WAL disk usage (GB) |
| `WALACOR_WAL_MAX_AGE_HOURS` | 72 | Max WAL record age (hours) |
| `WALACOR_WAL_HIGH_WATER_MARK` | 10000 | Max undelivered records; gateway returns 503 when exceeded (enforced mode) |

## Content analysis (Phase 10)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_RESPONSE_POLICY_ENABLED` | true | Enable post-inference content analysis |
| `WALACOR_PII_DETECTION_ENABLED` | true | Enable PII detector |
| `WALACOR_TOXICITY_DETECTION_ENABLED` | false | Enable toxicity detector |
| `WALACOR_TOXICITY_DENY_TERMS` | (empty) | Comma-separated extra deny-list terms |

## Token budget (Phase 11)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_TOKEN_BUDGET_ENABLED` | false | Enable token budget enforcement |
| `WALACOR_TOKEN_BUDGET_PERIOD` | monthly | Budget period: `daily` or `monthly` |
| `WALACOR_TOKEN_BUDGET_MAX_TOKENS` | 0 | Max tokens per period per tenant (0 = unlimited) |

## Session chain integrity (Phase 13)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_SESSION_CHAIN_ENABLED` | true | Enable ID-pointer chain (record_id + previous_record_id) for session records (G5) |
| `WALACOR_SESSION_CHAIN_MAX_SESSIONS` | 10000 | Max concurrent sessions tracked |
| `WALACOR_SESSION_CHAIN_TTL` | 3600 | Session state TTL seconds |

## Tool-aware gateway (Phase 14/16)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_TOOL_AWARE_ENABLED` | false | Enable tool-call awareness and auditing |
| `WALACOR_TOOL_STRATEGY` | auto | Tool strategy: `auto`, `passive`, `active`, or `disabled` |
| `WALACOR_TOOL_MAX_ITERATIONS` | 10 | Max tool-call loop iterations (active strategy) |
| `WALACOR_TOOL_EXECUTION_TIMEOUT_MS` | 30000 | Per-tool execution timeout in ms |
| `WALACOR_TOOL_CONTENT_ANALYSIS_ENABLED` | true | Run content analyzers on tool inputs/outputs |
| `WALACOR_MCP_SERVERS_JSON` | (empty) | JSON array or file path for MCP server configs |
| `WALACOR_WEB_SEARCH_ENABLED` | false | Enable built-in web search tool |
| `WALACOR_WEB_SEARCH_PROVIDER` | duckduckgo | `duckduckgo`, `brave`, or `serpapi` |
| `WALACOR_WEB_SEARCH_API_KEY` | (empty) | Required for `brave` and `serpapi` |
| `WALACOR_WEB_SEARCH_MAX_RESULTS` | 5 | Results per query |

## Multi-model routing + Redis (Phase 15)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_REDIS_URL` | (empty) | Redis URL for shared state (e.g. `redis://redis-svc:6379/0`) |
| `WALACOR_MODEL_ROUTING_JSON` | (empty) | JSON array or file path for model routing rules |

## Reasoning model support (Phase 17)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_THINKING_STRIP_ENABLED` | true | Strip `<think>` blocks from reasoning model responses |

## Llama Guard safety classifier (Phase 17)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_LLAMA_GUARD_ENABLED` | false | Enable Llama Guard 3 content analyzer |
| `WALACOR_LLAMA_GUARD_MODEL` | llama-guard3 | Ollama model name for Llama Guard inference |
| `WALACOR_LLAMA_GUARD_OLLAMA_URL` | (empty) | Ollama URL for Llama Guard (defaults to PROVIDER_OLLAMA_URL) |
| `WALACOR_LLAMA_GUARD_TIMEOUT_MS` | 5000 | Inference timeout in ms |

## OpenTelemetry export (Phase 17)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_OTEL_ENABLED` | false | Enable OpenTelemetry span export |
| `WALACOR_OTEL_ENDPOINT` | http://localhost:4317 | OTLP gRPC endpoint |
| `WALACOR_OTEL_SERVICE_NAME` | walacor-gateway | OTel service.name resource attribute |

## Lineage dashboard (Phase 18)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_LINEAGE_ENABLED` | true | Enable `/lineage/` dashboard and `/v1/lineage/*` API |

## Embedded control plane (Phase 20)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_CONTROL_PLANE_ENABLED` | true | Enable embedded control plane (CRUD + dashboard tab) |
| `WALACOR_CONTROL_PLANE_DB_PATH` | (empty) | SQLite path for control plane state (default: alongside WAL db) |

## JWT / SSO authentication (Phase 21)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_AUTH_MODE` | api_key | Authentication mode: `api_key`, `jwt`, or `both` |
| `WALACOR_JWT_SECRET` | (empty) | Shared secret for HS256 JWT validation |
| `WALACOR_JWT_JWKS_URL` | (empty) | JWKS endpoint for RS256/ES256 JWT validation |
| `WALACOR_JWT_ISSUER` | (empty) | Expected JWT issuer (iss claim) |
| `WALACOR_JWT_AUDIENCE` | (empty) | Expected JWT audience (aud claim) |
| `WALACOR_JWT_ALGORITHMS` | HS256 | Comma-separated algorithms (HS256, RS256, ES256) |
| `WALACOR_JWT_USER_CLAIM` | sub | JWT claim for user ID |
| `WALACOR_JWT_EMAIL_CLAIM` | email | JWT claim for email |
| `WALACOR_JWT_ROLES_CLAIM` | roles | JWT claim for roles |
| `WALACOR_JWT_TEAM_CLAIM` | team | JWT claim for team |

## Compliance export (Phase 22)

| Variable | Default | Description |
|----------|---------|-------------|
| (no env vars) | — | Compliance export is always available at `/v1/compliance/export` when lineage is enabled. Supports `format=json|csv|pdf`, `framework=eu_ai_act|nist|soc2|iso42001`, `start=YYYY-MM-DD`, `end=YYYY-MM-DD`. PDF generation requires WeasyPrint + system pango/cairo libraries. |

## Adaptive Gateway (Phase 23)

Self-configuring intelligence layer — startup probes, request classification, model capability probing, identity validation, and resource monitoring.

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_STARTUP_PROBES_ENABLED` | true | Run startup probes at boot (provider health, disk space, routing validation) |
| `WALACOR_PROVIDER_HEALTH_CHECK_ON_STARTUP` | true | Ping all configured providers during startup |
| `WALACOR_CAPABILITY_PROBE_TTL_SECONDS` | 86400 | Re-probe model capabilities after this many seconds (0 = never re-probe) |
| `WALACOR_IDENTITY_VALIDATION_ENABLED` | true | Cross-validate JWT sub claim against X-User-Id header; JWT wins on mismatch |
| `WALACOR_DISK_MONITOR_ENABLED` | true | Monitor WAL disk space and log warnings when free space drops below threshold |
| `WALACOR_DISK_MIN_FREE_PERCENT` | 5.0 | Minimum free disk percentage before warning (float) |
| `WALACOR_RESOURCE_MONITOR_INTERVAL_SECONDS` | 60 | Background resource monitor check interval (seconds) |
| `WALACOR_CUSTOM_STARTUP_PROBES` | (empty) | Comma-separated Python class paths for custom `StartupProbe` implementations |
| `WALACOR_CUSTOM_REQUEST_CLASSIFIERS` | (empty) | Comma-separated Python class paths for custom `RequestClassifier` implementations |
| `WALACOR_CUSTOM_IDENTITY_VALIDATORS` | (empty) | Comma-separated Python class paths for custom `IdentityValidator` implementations |
| `WALACOR_CUSTOM_RESOURCE_MONITORS` | (empty) | Comma-separated Python class paths for custom `ResourceMonitor` implementations |

All probes and monitors fail-open — a failed probe never blocks traffic.

## Prompt caching (Phase 28)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_PROMPT_CACHING_ENABLED` | true | Auto-inject cache_control on Anthropic system messages; detect cache hits from Anthropic and OpenAI |

## Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_METRICS_ENABLED` | true | Enable `/metrics` endpoint |
| `WALACOR_LOG_LEVEL` | INFO | Logging level |

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_GATEWAY_HOST` | 0.0.0.0 | Bind host |
| `WALACOR_GATEWAY_PORT` | 8000 | Bind port |
| `WALACOR_UVICORN_WORKERS` | 1 | Worker processes (>1 disables in-memory state sharing) |
