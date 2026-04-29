# Walacor Gateway — Complete Reference

> Every tool, method, technology, protocol, and subsystem in the gateway.

---

## 1. What It Is

An ASGI audit/governance proxy for LLM providers. Single port (8000), multi-provider, multi-model. Intercepts every LLM request, enforces governance (attestation, policy, budget, content safety), records cryptographic audit trails, and forwards to upstream providers.

---

## 2. Tech Stack

### Core Runtime
| Technology | Purpose | Package |
|---|---|---|
| Python 3.12+ | Runtime | — |
| Starlette | ASGI web framework | `starlette>=0.40` |
| Uvicorn | ASGI server | `uvicorn[standard]>=0.34` |
| uvloop | High-performance event loop | `uvloop>=0.21.0` |
| httpx | Async HTTP client (HTTP/2) | `httpx[http2]>=0.28` |
| Pydantic Settings | Configuration management | `pydantic-settings>=2.0` |
| Prometheus Client | Metrics export | `prometheus-client>=0.20` |
| walacor-core | Policy engine, sync models | `walacor-core` |
| Tenacity | Retry logic | `tenacity>=9.0` |
| PyBreaker | Circuit breaker pattern | `pybreaker>=1.2` |

### Optional Dependencies
| Extra | Package | Purpose |
|---|---|---|
| `[redis]` | `redis>=5.0` | Multi-replica state (session chain, budget, rate limiting) |
| `[auth]` | `pyjwt[crypto]>=2.8` | JWT/SSO authentication (HS256, RS256, ES256) |
| `[telemetry]` | `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc` | Distributed tracing |
| `[search]` | `ddgs>=7.0` | DuckDuckGo web search |
| `[compliance]` | `weasyprint>=62.0`, `jinja2>=3.1` | PDF compliance reports |
| `[dev]` | `pytest`, `anyio`, `pytest-asyncio` | Testing |

### Storage Technologies
| Technology | Purpose |
|---|---|
| SQLite (WAL mode) | Local write-ahead log, lineage dashboard, embedded control plane |
| Walacor Cloud API | Remote audit record storage (REST + JWT auth) |
| Redis | Shared state across replicas (optional) |

### Protocols & Standards
| Standard | Where Used |
|---|---|
| OpenAI Chat Completions API | Request/response format, provider compatibility |
| Anthropic Messages API | Anthropic adapter |
| Server-Sent Events (SSE) | Streaming responses |
| SHA3-512 | Used by Walacor backend to issue `DH` (data hash) on ingest |
| JWT (RFC 7519) | SSO authentication (HS256/RS256/ES256) |
| JWKS (RFC 7517) | Public key discovery for RS256/ES256 |
| MCP (Model Context Protocol) | External tool integration |
| OpenTelemetry (OTLP gRPC) | Distributed tracing |
| Prometheus text format | Metrics export |
| EU AI Act (Articles 9/12/14/15/61) | Compliance mapping |
| NIST AI RMF | Compliance mapping |
| SOC 2 Trust Criteria | Compliance mapping |

---

## 3. Architecture Overview

### Request Flow (8-Step Pipeline)

```
Client Request
    │
    ▼
[1] Parse Request ──────────── Adapter selection (fnmatch routes → path-based)
    │                          → ModelCall (model, messages, metadata)
    ▼
[2] Pre-Checks ────────────── Attestation → Policy → WAL backpressure
    │                          → Budget → Rate limit
    ▼
[3] Tool Strategy ─────────── auto/active/passive/none
    │                          Model capability registry check
    ▼
[4] Forward to Provider ───── Load balancer → Circuit breaker → Retry
    │                          httpx async HTTP/2 call
    ▼
[5] Tool Loop (if active) ─── Execute tools → Content analysis on output
    │                          → Record tool events (Walacor hashes on ingest)
    ▼
[6] Post-Inference Policy ─── Content analysis (PII, toxicity, Llama Guard)
    │                          → Response policy evaluation
    ▼
[7] Record & Write ────────── Session chain update → Build execution record
    │                          → StorageRouter fan-out (WAL + Walacor)
    │                          → Token usage → OTel span → Metrics
    ▼
[8] Response ──────────────── Governance headers (X-Walacor-*)
                               Streaming: SSE with background audit task
```

### Middleware Stack (outermost to innermost)
1. **CORS** — preflight handling, expose governance headers
2. **API Key / JWT Auth** — authentication enforcement
3. **Completeness** — attempt record for every request (finally block)

---

## 4. Provider Adapters

### Supported Providers

| Provider | Adapter | Endpoint | Key Features |
|---|---|---|---|
| OpenAI | `OpenAIAdapter` | `/v1/chat/completions` | SSE streaming, tool calls, multimodal, cache hit detection |
| Anthropic | `AnthropicAdapter` | `/v1/messages` | Content blocks, tool_use, cache control injection |
| Ollama | `OllamaAdapter` | `/v1/chat/completions` | Model digest caching, native reasoning, thinking strip |
| HuggingFace | `HuggingFaceAdapter` | Inference API | OpenAI-compatible format |
| Generic | `GenericAdapter` | Configurable | JSONPath extraction, auto-detection |

### Adapter Interface (`ProviderAdapter` ABC)

| Method | Returns | Purpose |
|---|---|---|
| `parse_request(request)` | `ModelCall` | Extract model, messages, metadata from HTTP request |
| `build_forward_request(call, original)` | `httpx.Request` | Build upstream provider request |
| `parse_response(response)` | `ModelResponse` | Parse non-streaming response |
| `parse_streamed_response(chunks)` | `ModelResponse` | Parse accumulated SSE chunks |
| `supports_streaming()` | `bool` | Whether adapter supports SSE |
| `get_provider_name()` | `str` | Provider identifier string |
| `build_tool_result_call(call, interactions)` | `ModelCall` | Inject tool results for next iteration |

### Data Models

```python
ModelCall:       model, messages, metadata, tools, system_prompt, inference_params, is_streaming
ModelResponse:   content, thinking_content, tool_calls, finish_reason, usage, metadata, model_hash
ToolInteraction: tool_name, input_hash, output_hash, input_data, sources, error, latency_ms, iteration
```

### Supporting Modules
- **`thinking.py`** — `strip_thinking_tokens(text)` → `(clean, thinking)` — regex-based `<think>` block extraction
- **`caching.py`** — `inject_cache_control(body)` for Anthropic, `detect_cache_hit(headers)` for response headers

---

## 5. Governance System

### 5.1 Attestation

| Component | File | Purpose |
|---|---|---|
| `AttestationCache` | `cache/attestation_cache.py` | TTL-based in-memory cache of model attestations |
| `CachedAttestation` | same | Dataclass: attestation_id, model_id, provider, status, verification_level |
| Auto-attestation | `orchestrator.py` | Creates `self-attested:{model_id}` on first use (no control plane) |

**Attestation statuses:** `active`, `revoked`, `blocked`
**Verification levels:** `self_attested`, `vendor_verified`, `third_party`

### 5.2 Policy Engine

| Component | File | Purpose |
|---|---|---|
| `PolicyCache` | `cache/policy_cache.py` | Versioned policy storage with staleness detection |
| `evaluate_pre_inference()` | `pipeline/policy_evaluator.py` | Pre-forward policy check (attestation context) |
| `evaluate_post_inference()` | `pipeline/response_evaluator.py` | Post-forward policy check (content analysis results) |
| Policy engine | `walacor_core.policy_engine` | Rule evaluation (`equals`, `contains`, `greater_than`, etc.) |

**Policy context fields (pre-inference):** model_id, provider, status, verification_level, tenant_id, prompt.text
**Policy context fields (post-inference):** toxicity, pii_detected, content_analysis results

**Fail-closed:** If `policy_cache.fetched_at` exceeds `policy_staleness_threshold` (900s), all requests are denied.

### 5.3 Token Budget

| Component | File | Purpose |
|---|---|---|
| `BudgetTracker` | `pipeline/budget_tracker.py` | In-memory token budget tracking |
| `RedisBudgetTracker` | same | Redis-backed (Lua atomic check-and-reserve) |
| `make_budget_tracker()` | same | Factory: Redis if available, else in-memory |

**Operations:** `configure()`, `check_and_reserve()`, `record_usage()`, `get_snapshot()`, `remove()`
**Budget periods:** `daily`, `monthly`
**Alert thresholds:** configurable (default 70%, 90%, 100%)

### 5.4 Session Chain (ID-pointer chain)

| Component | File | Purpose |
|---|---|---|
| `SessionChainTracker` | `pipeline/session_chain.py` | In-memory ID-pointer chain per session |
| `RedisSessionChainTracker` | same | Redis-backed (HASH per session) |
| `make_session_chain_tracker()` | same | Factory function |

**Chain construction:**
```
record_id is a UUIDv7 (time-ordered) assigned by the gateway
previous_record_id = the prior turn's record_id (null for the genesis record)
```
The Walacor backend issues a tamper-evident `DH` (data hash) on ingest as the cryptographic checkpoint.

**Chain verification:** Server-side via `/v1/lineage/verify/{session_id}` walks the `previous_record_id` linkage and reports any breaks.

### 5.5 Rate Limiting

| Component | File | Purpose |
|---|---|---|
| `SlidingWindowRateLimiter` | `pipeline/rate_limiter.py` | In-memory sliding window |
| `RedisRateLimiter` | same | Redis-backed rate limiting |

**Key structure:** per user+model (or per user only if `rate_limit_per_model=False`)

### 5.6 Content Analysis

| Analyzer | File | Method | Categories |
|---|---|---|---|
| `PIIDetector` | `content/pii_detector.py` | Regex-based | credit_card, ssn, aws_key, api_key (BLOCK); ip, email, phone (WARN) |
| `ToxicityDetector` | `content/toxicity_detector.py` | Keyword list | Configurable deny terms |
| `LlamaGuardAnalyzer` | `content/llama_guard.py` | Ollama `/api/chat` | S1-S14 safety categories; S4 (child safety) → BLOCK, others → WARN |
| `StreamSafetyAnalyzer` | `content/stream_safety.py` | Streaming content analysis | Real-time analysis during SSE streaming |

**ContentAnalyzer Protocol:**
```python
async def analyze(text: str) -> ContentAnalysisResult | None
    # Returns: verdict (PASS/WARN/BLOCK), category, confidence, analyzer_id
```

**Fail-open:** Llama Guard returns PASS with confidence=0.0 on Ollama unavailability.

---

## 6. Storage System

### 6.1 Storage Abstraction Layer

| Component | File | Purpose |
|---|---|---|
| `StorageBackend` | `storage/backend.py` | Protocol: `write_execution`, `write_attempt`, `write_tool_event`, `close` |
| `StorageRouter` | `storage/router.py` | Fan-out to all backends independently |
| `WriteResult` | `storage/router.py` | Frozen dataclass: `succeeded: list[str]`, `failed: list[str]` |
| `WALBackend` | `storage/wal_backend.py` | Wraps `WALWriter` (sync SQLite) |
| `WalacorBackend` | `storage/walacor_backend.py` | Wraps `WalacorClient` (async REST) |

**Write semantics:**
- `write_execution()` → returns `WriteResult` (success/failure per backend)
- `write_attempt()` → fire-and-forget (never raises)
- `write_tool_event()` → fire-and-forget (never raises)

### 6.2 WAL (Write-Ahead Log)

| Component | File | Purpose |
|---|---|---|
| `WALWriter` | `wal/writer.py` | SQLite WAL mode, append-only, crash-safe, fsync |
| `DeliveryWorker` | `wal/delivery_worker.py` | Background flush from WAL → Walacor backend |

**Tables:**
- `wal_records` — execution records (execution_id PK, record_json, created_at, delivered, delivered_at)
- `gateway_attempts` — completeness invariant (request_id PK, timestamp, tenant_id, provider, model_id, path, disposition, execution_id, status_code, user)

**Methods:** `write_durable()`, `write_attempt()`, `write_tool_event()`, `get_undelivered()`, `mark_delivered()`, `pending_count()`, `oldest_pending_seconds()`, `disk_usage_bytes()`, `purge_delivered()`, `purge_attempts()`, `close()`

### 6.3 Walacor Cloud Client

| Component | File | Purpose |
|---|---|---|
| `WalacorClient` | `walacor/client.py` | Async HTTP client with JWT auth, proactive refresh |

**API Endpoints:**
- `POST /auth/login` → JWT token (includes "Bearer " prefix)
- `POST /envelopes/submit` with `ETId` header → data write

**ETIds (entity types):**
- `9000011` — gateway_executions (31 fields, 4 indexes)
- `9000012` — gateway_attempts (10 fields, 3 indexes)
- `9000013` — gateway_tool_events (18 fields, 4 indexes)

**Field filtering:** `_EXECUTION_SCHEMA_FIELDS` (25 fields) and `_TOOL_EVENT_SCHEMA_FIELDS` (17 fields) — strips unknown fields before submit to prevent silent rejection.

**Auth:** JWT with proactive refresh before expiry (`_REFRESH_LEAD_SECONDS = 300`), re-auth on 401.

---

## 7. Authentication & Identity

### 7.1 API Key Auth

| Component | File | Purpose |
|---|---|---|
| `require_api_key_if_configured()` | `auth/api_key.py` | Check `X-API-Key` header or `Authorization: Bearer` |

### 7.2 JWT/SSO Auth

| Component | File | Purpose |
|---|---|---|
| `validate_jwt()` | `auth/jwt_auth.py` | HS256 (shared secret), RS256/ES256 (JWKS endpoint) |
| `PyJWKClient` | pyjwt library | JWKS key caching (1h TTL) |

**Supported algorithms:** HS256, RS256, ES256
**Auth modes:** `api_key` (default), `jwt` (JWT-only), `both` (JWT first, API key fallback)

### 7.3 Caller Identity

| Component | File | Purpose |
|---|---|---|
| `CallerIdentity` | `auth/identity.py` | Frozen dataclass: user_id, email, roles, team, source |
| `resolve_identity_from_headers()` | same | Reads X-User-Id, X-Team-Id, X-User-Roles headers |

**Identity sources:** jwt, header, openwebui
**Cross-validation:** JWT claims vs header-claimed identity (Phase 23)

---

## 8. Tool System

### 8.1 MCP Integration

| Component | File | Purpose |
|---|---|---|
| `MCPClient` | `mcp/client.py` | Subprocess-based MCP server connection |
| `ToolRegistry` | `mcp/registry.py` | Multi-server tool registry with startup/shutdown |
| `ToolDefinition` | `mcp/client.py` | Tool schema (name, description, parameters) |
| `ToolResult` | `mcp/client.py` | Execution result (content, is_error, sources) |

**MCPClient duck-type interface:**
```python
get_tools() -> list[ToolDefinition]
async call_tool(name, args, timeout_ms) -> ToolResult
```

### 8.2 Built-in Web Search

| Component | File | Purpose |
|---|---|---|
| `WebSearchTool` | `tools/web_search.py` | DuckDuckGo / Brave / SerpAPI web search |

**Providers:** `duckduckgo` (free, Wikipedia-indexed topics), `brave` (API key required), `serpapi` (API key required)
**Returns:** `ToolResult` with `sources: [{title, url, snippet}]`

### 8.3 Tool Strategy

| Strategy | Behavior |
|---|---|
| `auto` | Detect from adapter capabilities + model capability registry |
| `active` | Inject tools, run execution loop (max 10 iterations) |
| `passive` | Accept tool_calls from model, no injection |
| `none` | Strip tools, force non-tool response |

### 8.4 Model Capability Registry

```python
_model_capabilities: dict  # {model_id: {"supports_tools": bool}}
_TOOL_UNSUPPORTED_PHRASES: list[str]  # 7 error patterns (Ollama, OpenAI, Anthropic, generic)
```
**Behavior:** First request to non-tool model → retry without tools → cache `supports_tools=False`. All subsequent requests skip tool injection.

### 8.5 Tool Event Recording

Each tool execution produces a record with:
- `event_id`, `execution_id`, `session_id`, `tenant_id`, `gateway_id`
- `tool_name`, `tool_type`, `tool_source`
- `input_data`, `output_data` (Walacor backend computes SHA3-512 on ingest and returns the `DH`)
- `duration_ms`, `iteration`, `is_error`
- `content_analysis` (PII/toxicity/Llama Guard results on tool output)
- `metadata_json`, `sources` (for web search)

---

## 9. Routing & Resilience

### 9.1 Model Routing

| Mechanism | Config | Priority |
|---|---|---|
| fnmatch pattern routes | `WALACOR_MODEL_ROUTING_JSON` | First (checked before path) |
| Path-based routing | URL path (`/v1/chat/completions` → OpenAI) | Fallback |

**Route format:** `[{"pattern": "qwen3*", "provider": "ollama", "url": "http://localhost:11434"}]`

### 9.2 Load Balancing

| Component | File | Purpose |
|---|---|---|
| `LoadBalancer` | `routing/balancer.py` | Weighted round-robin per model group |
| `Endpoint` | same | Dataclass: url, api_key, weight |
| `ModelGroup` | same | Dataclass: pattern (fnmatch), endpoints |

### 9.3 Circuit Breaker

| Component | File | Purpose |
|---|---|---|
| `CircuitBreakerRegistry` | `routing/circuit.py` | Per-endpoint circuit breakers |

**States:** closed → open (after `fail_max` failures) → half-open (after `reset_timeout` seconds)

### 9.4 Retry

| Component | File | Purpose |
|---|---|---|
| `RetryStrategy` | `routing/retry.py` | Configurable retry with tenacity |

**Retry on:** 503 (service unavailable), 429 (rate limited)
**Max attempts:** configurable (default 3)

### 9.5 Fallback

| Component | File | Purpose |
|---|---|---|
| `FallbackRouter` | `routing/fallback.py` | Cross-provider fallback on failure |

---

## 10. Observability

### 10.1 Prometheus Metrics

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `gateway_requests_total` | Counter | provider, model, outcome | Total requests |
| `gateway_attempts_total` | Counter | disposition | Completeness invariant |
| `pipeline_duration_seconds` | Histogram | step | Pipeline step latency |
| `tool_calls_total` | Counter | provider, tool_type, source | Tool executions |
| `tokens_total` | Counter | provider, model, type | Token usage |

**Endpoint:** `GET /metrics` → Prometheus text format

### 10.2 OpenTelemetry

| Component | File | Purpose |
|---|---|---|
| `init_tracer()` | `telemetry/otel.py` | OTLP gRPC span exporter setup |
| `emit_inference_span()` | same | Retroactive span per request |

**Semantic conventions (GenAI):**
- `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens`
- `walacor.*` custom attributes (execution_id, policy_result, etc.)

### 10.3 Health Endpoint

**`GET /health`** returns:
- `status` (healthy/degraded/unhealthy)
- `uptime_seconds`
- `wal_pending`, `wal_oldest_seconds`, `wal_disk_bytes`
- `budget_snapshot` (remaining tokens, utilization %)
- `model_capabilities` (supports_tools per model)
- `content_analyzers` count
- `control_plane_status`

### 10.4 Structured Logging

| Component | File | Purpose |
|---|---|---|
| `configure_json_logging()` | `util/json_logger.py` | JSON-formatted log output |
| `redact_sensitive()` | `util/redact.py` | Password/key redaction in logs |

### 10.5 Request Context

| Variable | Purpose |
|---|---|
| `request_id_var` | ContextVar for request correlation |
| `disposition_var` | ContextVar for request outcome |
| `execution_id_var` | ContextVar for audit record ID |
| `provider_var` | ContextVar for provider name |
| `model_id_var` | ContextVar for model name |

---

## 11. Lineage Dashboard

### 11.1 Backend

| Component | File | Purpose |
|---|---|---|
| `LineageReader` | `lineage/reader.py` | Read-only SQLite connection (`?mode=ro`) |
| Lineage API | `lineage/api.py` | 8 GET endpoints |

**API Endpoints:**
| Endpoint | Returns |
|---|---|
| `GET /v1/lineage/sessions` | Paginated session list with model, user, request count |
| `GET /v1/lineage/sessions/{id}` | Session timeline (all executions in order) |
| `GET /v1/lineage/executions/{id}` | Execution detail + tool events |
| `GET /v1/lineage/attempts` | Recent attempts + disposition breakdown |
| `GET /v1/lineage/metrics` | Time-bucketed request/allow/block counts |
| `GET /v1/lineage/token-latency` | Time-bucketed token usage + latency aggregation |
| `GET /v1/lineage/trace/{id}` | Execution waterfall trace |
| `GET /v1/lineage/verify/{id}` | Chain verification proof (walks `previous_record_id` linkage) |

### 11.2 Frontend (SPA)

**Location:** `src/gateway/lineage/static/`
- Vanilla JS SPA (no framework)
- Dark theme with gold accent
- Client-side chain verification via js-sha3 CDN

**Views:**
- **Overview** — live throughput chart (canvas-based, polls `/metrics` every 3s), session list
- **Session Timeline** — chain cards with tool badges, sequence numbers
- **Execution Detail** — full record, tool event cards, content analysis verdicts
- **Attempts** — disposition pie chart, recent attempt table
- **Token/Latency Charts** — dual canvas (stacked area for tokens, line+area for latency)
- **Control Panel** — Models, Policies, Budgets, Status sub-views (auth-gated)
- **Chain Verification** — glow animations (green pass / red fail)

---

## 12. Embedded Control Plane

### 12.1 Store

| Component | File | Purpose |
|---|---|---|
| `ControlPlaneStore` | `control/store.py` | SQLite CRUD for attestations, policies, budgets |

**Tables:** `attestations`, `policies`, `budgets`
**Mode:** WAL, synchronous=FULL, lazy init

### 12.2 API (15 endpoints)

| Route | Method | Purpose |
|---|---|---|
| `/v1/control/attestations` | GET | List attestations |
| `/v1/control/attestations` | POST | Upsert attestation |
| `/v1/control/attestations/{id}` | DELETE | Delete attestation |
| `/v1/control/policies` | GET | List policies |
| `/v1/control/policies` | POST | Create policy |
| `/v1/control/policies/{id}` | PUT | Update policy |
| `/v1/control/policies/{id}` | DELETE | Delete policy |
| `/v1/control/budgets` | GET | List budgets |
| `/v1/control/budgets` | POST | Upsert budget |
| `/v1/control/budgets/{id}` | DELETE | Delete budget |
| `/v1/control/content-policies` | GET | List content policies |
| `/v1/control/content-policies` | POST | Upsert content policy |
| `/v1/control/content-policies/{id}` | DELETE | Delete content policy |
| `/v1/control/status` | GET | Gateway status overview |
| `/v1/control/discover` | GET | Discover provider models (Ollama/OpenAI) |

### 12.3 Sync & Loader

| Component | File | Purpose |
|---|---|---|
| `load_into_caches()` | `control/loader.py` | Load DB → in-memory caches at startup |
| `_run_local_sync_loop()` | same | Refresh every `sync_interval` seconds |
| `sync_attestation_proofs()` | `control/sync_api.py` | Fleet sync endpoint |
| `sync_policies()` | same | Fleet sync endpoint |

### 12.4 Model Discovery

| Component | File | Purpose |
|---|---|---|
| `discover_provider_models()` | `control/discovery.py` | Scan Ollama `/api/tags` + OpenAI `/v1/models` |

---

## 13. Alerting System

| Component | File | Purpose |
|---|---|---|
| `AlertBus` | `alerts/bus.py` | Async event bus for alert distribution |
| `WebhookDispatcher` | `alerts/dispatcher.py` | HTTP POST to webhook URLs |
| `SlackDispatcher` | same | Slack-formatted webhook |
| `PagerDutyDispatcher` | same | PagerDuty Events API v2 |

**Alert triggers:** Budget threshold crossings (70%, 90%, 100%)

---

## 14. Compliance

| Component | File | Purpose |
|---|---|---|
| `compliance_export()` | `compliance/api.py` | JSON/CSV/PDF compliance report |
| `COMPLIANCE_FRAMEWORKS` | `compliance/frameworks.py` | EU AI Act, NIST AI RMF, SOC 2 mappings |
| `generate_pdf_report()` | `compliance/pdf_report.py` | WeasyPrint PDF generation |

**Export endpoint:** `GET /v1/compliance/export?format=json|csv|pdf`
**Frameworks mapped:** EU AI Act (Articles 9/12/14/15/61), NIST AI RMF, SOC 2 Trust Criteria

---

## 15. Adaptive Gateway (Phase 23)

### 15.1 Startup Probes

| Component | File | Purpose |
|---|---|---|
| `run_startup_probes()` | `adaptive/startup_probes.py` | Provider health, disk space, routing validation |

### 15.2 Request Classifier

| Component | File | Purpose |
|---|---|---|
| `DefaultRequestClassifier` | `adaptive/request_classifier.py` | Classify requests by type, complexity |
| Custom classifiers | Enterprise extension point | `WALACOR_CUSTOM_REQUEST_CLASSIFIERS` |

### 15.3 Identity Validator

| Component | File | Purpose |
|---|---|---|
| `DefaultIdentityValidator` | `adaptive/identity_validator.py` | Cross-validate JWT vs header identity |
| Custom validators | Enterprise extension point | `WALACOR_CUSTOM_IDENTITY_VALIDATORS` |

### 15.4 Resource Monitor

| Component | File | Purpose |
|---|---|---|
| `DefaultResourceMonitor` | `adaptive/resource_monitor.py` | Disk usage monitoring, WAL size tracking |

**Background task:** Checks every `resource_monitor_interval_seconds` (default 60s)

### 15.5 Capability Registry

| Component | File | Purpose |
|---|---|---|
| `CapabilityRegistry` | `adaptive/capability_registry.py` | Cache model capabilities with TTL |

### 15.6 Interfaces

| Interface | File | Purpose |
|---|---|---|
| `StartupProbe` | `adaptive/interfaces.py` | Protocol for custom probes |
| `RequestClassifier` | same | Protocol for request classification |
| `IdentityValidator` | same | Protocol for identity validation |
| `ResourceMonitor` | same | Protocol for resource monitoring |

---

## 16. OpenWebUI Integration

| Component | File | Purpose |
|---|---|---|
| `openwebui_status()` | `openwebui/status_api.py` | OpenWebUI-compatible status endpoint |

**Endpoint:** `GET /v1/openwebui/status`
**Headers recognized:** `X-OpenWebUI-User-Name`, `X-OpenWebUI-User-Id`, `X-OpenWebUI-User-Email`, `X-OpenWebUI-User-Role`

---

## 17. Models API

| Component | File | Purpose |
|---|---|---|
| `list_models()` | `models_api.py` | OpenAI-compatible `GET /v1/models` |

Returns available models from all configured providers.

---

## 18. Sync Client (Remote Control Plane)

| Component | File | Purpose |
|---|---|---|
| `SyncClient` | `sync/sync_client.py` | Pull-sync from remote control plane |

**Operations:** `startup_sync()`, `sync_attestations()`, `sync_policies()`
**Backoff:** Exponential (5s initial, doubles, capped at 60s)

---

## 19. Execution Record Schema

Every LLM request produces an execution record with these fields:

| Field | Type | Source |
|---|---|---|
| `execution_id` | UUID | Gateway-generated |
| `model_attestation_id` | string | From attestation cache |
| `model_id` | string | From request body |
| `provider` | string | From adapter |
| `policy_version` | int | From policy cache |
| `policy_result` | string | pass/fail/blocked |
| `tenant_id` | string | Config |
| `gateway_id` | string | Config |
| `timestamp` | ISO 8601 | Gateway clock |
| `user` | string | From caller identity |
| `session_id` | UUID | From header or generated |
| `metadata_json` | JSON string | Caller, tool info, enforcement mode |
| `prompt_text` | string | Full prompt (Walacor backend hashes) |
| `response_content` | string | Full response |
| `thinking_content` | string | Extracted `<think>` reasoning |
| `provider_request_id` | string | Upstream provider request ID |
| `model_hash` | string | Ollama model digest |
| `latency_ms` | float | Pipeline latency |
| `prompt_tokens` | int | From provider usage |
| `completion_tokens` | int | From provider usage |
| `total_tokens` | int | From provider usage |
| `cache_hit` | bool | From provider response |
| `cached_tokens` | int | Anthropic cache tokens |
| `cache_creation_tokens` | int | Anthropic cache creation |
| `retry_of` | string | Execution ID of retried request |
| `variant_id` | string | A/B test variant |
| `record_id` | UUIDv7 | Time-ordered record identifier |
| `previous_record_id` | UUIDv7 | Prior turn's `record_id`; null for genesis |
| `sequence_number` | int | Chain sequence (WAL only) |
| `response_policy_result` | string | Post-inference policy result (WAL only) |
| `analyzer_decisions_json` | JSON | Content analysis decisions (WAL only) |

---

## 20. HTTP Endpoints (Complete List)

### Proxy Routes (POST)
| Path | Handler |
|---|---|
| `/v1/chat/completions` | `handle_request()` |
| `/v1/completions` | `handle_request()` |
| `/v1/messages` | `handle_request()` |
| `/v1/custom` | `handle_request()` |
| `/generate` | `handle_request()` |

### System Routes (GET)
| Path | Handler | Auth |
|---|---|---|
| `/` | Redirect → `/lineage/` | None |
| `/health` | `health_response()` | None |
| `/metrics` | `metrics_response()` | None |
| `/v1/models` | `list_models()` | None |

### Lineage Routes (GET, no auth)
| Path | Handler |
|---|---|
| `/lineage/` | Static SPA |
| `/v1/lineage/sessions` | `lineage_sessions()` |
| `/v1/lineage/sessions/{id}` | `lineage_session_timeline()` |
| `/v1/lineage/executions/{id}` | `lineage_execution()` |
| `/v1/lineage/attempts` | `lineage_attempts()` |
| `/v1/lineage/metrics` | `lineage_metrics_history()` |
| `/v1/lineage/token-latency` | `lineage_token_latency_history()` |
| `/v1/lineage/trace/{id}` | `lineage_trace()` |
| `/v1/lineage/verify/{id}` | `lineage_verify()` |

### Control Plane Routes (require API key)
| Path | Methods | Handler |
|---|---|---|
| `/v1/control/attestations` | GET, POST | list, upsert |
| `/v1/control/attestations/{id}` | DELETE | delete |
| `/v1/control/policies` | GET, POST | list, create |
| `/v1/control/policies/{id}` | PUT, DELETE | update, delete |
| `/v1/control/budgets` | GET, POST | list, upsert |
| `/v1/control/budgets/{id}` | DELETE | delete |
| `/v1/control/content-policies` | GET, POST | list, upsert |
| `/v1/control/content-policies/{id}` | DELETE | delete |
| `/v1/control/status` | GET | status overview |
| `/v1/control/discover` | GET | model discovery |

### Sync Routes (require API key)
| Path | Method | Handler |
|---|---|---|
| `/v1/attestation-proofs` | GET | `sync_attestation_proofs()` |
| `/v1/policies` | GET | `sync_policies()` |

### Integration Routes
| Path | Method | Handler |
|---|---|---|
| `/v1/compliance/export` | GET | `compliance_export()` |
| `/v1/openwebui/status` | GET | `openwebui_status()` |

---

## 21. Configuration Variables (Complete List — 90+ variables)

All use `WALACOR_` prefix. See `.env.example` for full documentation.

### Identity & Mode
`GATEWAY_TENANT_ID`, `GATEWAY_ID`, `SKIP_GOVERNANCE`, `ENFORCEMENT_MODE`

### Provider URLs & Keys
`PROVIDER_OPENAI_URL`, `PROVIDER_OPENAI_KEY`, `PROVIDER_ANTHROPIC_URL`, `PROVIDER_ANTHROPIC_KEY`, `PROVIDER_OLLAMA_URL`, `PROVIDER_OLLAMA_KEY`, `PROVIDER_HUGGINGFACE_URL`, `PROVIDER_HUGGINGFACE_KEY`, `GENERIC_UPSTREAM_URL`, `GENERIC_MODEL_PATH`, `GENERIC_PROMPT_PATH`, `GENERIC_RESPONSE_PATH`, `GENERIC_AUTO_DETECT`, `GATEWAY_PROVIDER`

### Walacor Backend
`SERVER`, `USERNAME`, `PASSWORD`, `EXECUTIONS_ETID`, `ATTEMPTS_ETID`, `TOOL_EVENTS_ETID`

### Control Plane
`CONTROL_PLANE_URL`, `CONTROL_PLANE_API_KEY`, `CONTROL_PLANE_ENABLED`, `CONTROL_PLANE_DB_PATH`

### Auth
`GATEWAY_API_KEYS`, `AUTH_MODE`, `JWT_SECRET`, `JWT_JWKS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_ALGORITHMS`, `JWT_USER_CLAIM`, `JWT_EMAIL_CLAIM`, `JWT_ROLES_CLAIM`, `JWT_TEAM_CLAIM`

### Routing & Resilience
`MODEL_ROUTING_JSON`, `MODEL_GROUPS_JSON`, `CIRCUIT_BREAKER_FAIL_MAX`, `CIRCUIT_BREAKER_RESET_TIMEOUT`, `RETRY_MAX_ATTEMPTS`

### Tools & Search
`TOOL_AWARE_ENABLED`, `TOOL_STRATEGY`, `TOOL_MAX_ITERATIONS`, `TOOL_EXECUTION_TIMEOUT_MS`, `TOOL_CONTENT_ANALYSIS_ENABLED`, `WEB_SEARCH_ENABLED`, `WEB_SEARCH_PROVIDER`, `WEB_SEARCH_API_KEY`, `WEB_SEARCH_MAX_RESULTS`, `MCP_SERVERS_JSON`

### Content Analysis
`RESPONSE_POLICY_ENABLED`, `PII_DETECTION_ENABLED`, `TOXICITY_DETECTION_ENABLED`, `TOXICITY_DENY_TERMS`, `THINKING_STRIP_ENABLED`, `LLAMA_GUARD_ENABLED`, `LLAMA_GUARD_MODEL`, `LLAMA_GUARD_OLLAMA_URL`, `LLAMA_GUARD_TIMEOUT_MS`, `PROMPT_CACHING_ENABLED`

### Token Budget & Rate Limiting
`TOKEN_BUDGET_ENABLED`, `TOKEN_BUDGET_PERIOD`, `TOKEN_BUDGET_MAX_TOKENS`, `RATE_LIMIT_ENABLED`, `RATE_LIMIT_RPM`, `RATE_LIMIT_PER_MODEL`

### Alerting
`WEBHOOK_URLS`, `PAGERDUTY_ROUTING_KEY`, `ALERT_BUDGET_THRESHOLDS`

### Session Chain
`SESSION_CHAIN_ENABLED`, `SESSION_CHAIN_MAX_SESSIONS`, `SESSION_CHAIN_TTL`

### Completeness
`COMPLETENESS_ENABLED`, `ATTEMPTS_RETENTION_HOURS`

### WAL Storage
`WAL_PATH`, `WAL_MAX_SIZE_GB`, `WAL_MAX_AGE_HOURS`, `WAL_HIGH_WATER_MARK`, `MAX_STREAM_BUFFER_BYTES`

### Cache & Sync
`ATTESTATION_CACHE_TTL`, `POLICY_STALENESS_THRESHOLD`, `SYNC_INTERVAL`, `OLLAMA_DIGEST_CACHE_TTL`

### Network Tuning
`PROVIDER_TIMEOUT`, `PROVIDER_CONNECT_TIMEOUT`, `PROVIDER_MAX_CONNECTIONS`, `PROVIDER_MAX_KEEPALIVE`, `SSE_KEEPALIVE_INTERVAL`, `DELIVERY_BATCH_SIZE`, `DISK_DEGRADED_THRESHOLD`

### Observability
`METRICS_ENABLED`, `LOG_LEVEL`, `LINEAGE_ENABLED`, `OTEL_ENABLED`, `OTEL_ENDPOINT`, `OTEL_SERVICE_NAME`

### Adaptive Gateway
`STARTUP_PROBES_ENABLED`, `PROVIDER_HEALTH_CHECK_ON_STARTUP`, `CAPABILITY_PROBE_TTL_SECONDS`, `IDENTITY_VALIDATION_ENABLED`, `DISK_MONITOR_ENABLED`, `DISK_MIN_FREE_PERCENT`, `RESOURCE_MONITOR_INTERVAL_SECONDS`

### Server
`GATEWAY_HOST`, `GATEWAY_PORT`, `UVICORN_WORKERS`, `REDIS_URL`

---

## 22. File Map (93 Python source files)

```
src/gateway/
├── __init__.py
├── config.py                          # Pydantic-Settings, 90+ env vars
├── health.py                          # /health and /metrics endpoints
├── main.py                            # ASGI app, startup/shutdown, middleware
├── models_api.py                      # /v1/models endpoint
│
├── adapters/
│   ├── base.py                        # ProviderAdapter ABC, ModelCall, ModelResponse, ToolInteraction
│   ├── openai.py                      # OpenAI adapter (SSE, tools, multimodal)
│   ├── anthropic.py                   # Anthropic adapter (content blocks, cache control)
│   ├── ollama.py                      # Ollama adapter (digest cache, native reasoning)
│   ├── huggingface.py                 # HuggingFace adapter
│   ├── generic.py                     # Generic adapter (JSONPath extraction)
│   ├── thinking.py                    # strip_thinking_tokens() — <think> block extraction
│   └── caching.py                     # Prompt cache injection (Anthropic)
│
├── adaptive/
│   ├── __init__.py                    # load_custom_class(), parse_custom_paths()
│   ├── interfaces.py                  # Protocols: StartupProbe, RequestClassifier, etc.
│   ├── startup_probes.py             # Provider health, disk space, routing checks
│   ├── request_classifier.py         # Request type/complexity classification
│   ├── identity_validator.py         # JWT vs header cross-validation
│   ├── resource_monitor.py           # Disk/WAL monitoring
│   └── capability_registry.py        # Model capability caching
│
├── alerts/
│   ├── bus.py                         # AlertBus (async event bus)
│   └── dispatcher.py                  # Webhook, Slack, PagerDuty dispatchers
│
├── auth/
│   ├── api_key.py                     # X-API-Key auth
│   ├── jwt_auth.py                    # JWT validation (HS256/RS256/ES256, JWKS)
│   └── identity.py                    # CallerIdentity dataclass
│
├── cache/
│   ├── attestation_cache.py          # CachedAttestation + AttestationCache
│   └── policy_cache.py              # PolicyCache (versioned, staleness detection)
│
├── compliance/
│   ├── api.py                         # /v1/compliance/export endpoint
│   ├── frameworks.py                  # EU AI Act, NIST, SOC 2 mappings
│   └── pdf_report.py                 # WeasyPrint PDF generation
│
├── content/
│   ├── base.py                        # ContentAnalyzer protocol, ContentAnalysisResult
│   ├── pii_detector.py               # Regex-based PII detection
│   ├── toxicity_detector.py          # Keyword-based toxicity detection
│   ├── llama_guard.py                # LlamaGuardAnalyzer (Ollama-based, S1-S14)
│   └── stream_safety.py             # Streaming content analysis
│
├── control/
│   ├── store.py                       # ControlPlaneStore (SQLite CRUD)
│   ├── api.py                         # 15 control plane route handlers
│   ├── loader.py                      # load_into_caches() + local sync loop
│   ├── discovery.py                   # Provider model discovery
│   └── sync_api.py                   # Fleet sync endpoints
│
├── lineage/
│   ├── reader.py                      # LineageReader (read-only SQLite)
│   ├── api.py                         # 8 lineage API endpoints
│   └── static/                        # Dashboard SPA (HTML/CSS/JS)
│
├── mcp/
│   ├── client.py                      # MCPClient, ToolDefinition, ToolResult
│   └── registry.py                   # ToolRegistry (multi-server)
│
├── metrics/
│   └── prometheus.py                 # Prometheus counters, histograms
│
├── middleware/
│   └── completeness.py               # Completeness invariant middleware
│
├── openwebui/
│   └── status_api.py                 # OpenWebUI status endpoint
│
├── pipeline/
│   ├── context.py                     # PipelineContext singleton
│   ├── orchestrator.py               # 8-step request pipeline (1674 lines)
│   ├── hasher.py                      # build_execution_record()
│   ├── forwarder.py                   # HTTP forwarding with SSE tee
│   ├── session_chain.py              # ID-pointer chain tracker (in-memory/Redis)
│   ├── budget_tracker.py             # Token budget (in-memory/Redis)
│   ├── rate_limiter.py               # Sliding window rate limiter
│   ├── model_resolver.py             # Adapter selection logic
│   ├── policy_evaluator.py           # Pre-inference policy evaluation
│   └── response_evaluator.py         # Post-inference policy evaluation
│
├── routing/
│   ├── balancer.py                    # LoadBalancer, Endpoint, ModelGroup
│   ├── circuit.py                     # CircuitBreakerRegistry
│   ├── retry.py                       # RetryStrategy (tenacity)
│   └── fallback.py                   # FallbackRouter
│
├── storage/
│   ├── __init__.py                    # Exports: StorageBackend, StorageRouter, WriteResult, WALBackend, WalacorBackend
│   ├── backend.py                     # StorageBackend protocol
│   ├── router.py                      # StorageRouter (fan-out) + WriteResult
│   ├── wal_backend.py                # WALBackend (wraps WALWriter)
│   └── walacor_backend.py           # WalacorBackend (wraps WalacorClient)
│
├── sync/
│   └── sync_client.py               # SyncClient (remote control plane pull-sync)
│
├── telemetry/
│   └── otel.py                       # OpenTelemetry init + span emission
│
├── tools/
│   └── web_search.py                 # WebSearchTool (DuckDuckGo/Brave/SerpAPI)
│
├── util/
│   ├── json_logger.py                # JSON structured logging
│   ├── redact.py                      # Sensitive data redaction
│   ├── request_context.py           # ContextVars (request_id, disposition, etc.)
│   └── session_id.py                 # Session ID resolution from headers
│
├── wal/
│   ├── writer.py                      # WALWriter (SQLite WAL mode)
│   └── delivery_worker.py           # Background WAL → Walacor delivery
│
└── walacor/
    └── client.py                      # WalacorClient (async REST + JWT)
```

---

## 23. Deployment

### Docker Compose

**File:** `docker-compose.yml`

**Services:**
- `gateway` — main ASGI app (uvicorn)
- `redis` — shared state (profile: redis)
- `ollama` — local LLM provider (profile: demo, ollama)
- `demo-init` — auto-pull model + test request (profile: demo)

### Helm Chart

**Location:** `deploy/helm/`
- Kubernetes deployment with configurable replicas
- Service, ConfigMap, optional Redis sidecar

### Entry Points
- CLI: `walacor-gateway` (calls `gateway.main:main`)
- ASGI: `gateway.main:app`
- Docker: `uvicorn gateway.main:app`
- Demo: `python demo/quickstart.py`

---

## 24. Test Suite

**Framework:** pytest + anyio (asyncio backend)
**Pattern:** `@pytest.mark.anyio` with `anyio_backend` fixture
**Location:** `tests/unit/`, `tests/compliance/`
**Current count:** 465 pass, 2 skip

### Test Files
- `test_storage_router.py` — 18 tests (StorageRouter, WALBackend, WalacorBackend)
- `test_control_store.py` — 20 tests
- `test_control_api.py` — 10 tests
- `test_discovery.py` — 12 tests
- `test_jwt_auth.py` — 11 tests
- `test_identity.py` — 7 tests
- `test_lineage_reader.py` — 17 tests
- `test_web_search.py` — 14 tests
- `test_thinking_strip.py`, `test_llama_guard.py`, `test_otel.py`
- `test_redis_trackers.py`, `test_completeness.py`, `test_budget_tracker.py`
- And many more covering all pipeline steps

---

## 25. Deep Analysis — Best Approaches, Better Alternatives & Novel Ideas

> Research-backed analysis of every subsystem. For each area: what we do, whether it's best-in-class,
> what's better, what's novel, and concrete recommendations. Goal: lowest latency, strongest guarantees.

---

### 25.1 Language & Runtime Verdict

**Current:** Python 3.12+, Starlette, Uvicorn+uvloop, httpx (HTTP/2)

**Should we switch languages?**

| Gateway | Language | Overhead/req | Max RPS/instance | Source |
|---------|----------|-------------|------------------|--------|
| **TensorZero** | Rust | <1ms p99 at 10K QPS | 10,000+ | tensorzero.com/benchmarks |
| **Bifrost** | Go | ~11μs at 5K RPS | 5,000+ | github.com/maximhq/bifrost |
| **Envoy** | C++ | ~100μs | 35,000+ | envoyproxy.io |
| **Pingora** (Cloudflare) | Rust | N/A | 40M req/s fleet | blog.cloudflare.com |
| **LiteLLM** | Python | ~3.25ms | ~200 RPS stable | docs.litellm.ai/benchmarks |
| **Walacor Gateway** | Python | ~3-5ms est. | ~500-1000 est. | (needs benchmarking) |

**Verdict: Stay with Python.** Provider latency (100ms–10,000ms) dwarfs proxy overhead (3–5ms). A 5ms overhead on a 2,000ms Claude call is 0.25%. The governance logic (policy eval, content analysis, session chains, WAL writes) is where our value lives — rewriting to Rust gains <1% end-to-end improvement for months of work.

**When to reconsider:** If we need >1,000 RPS sustained on a single instance, or if proxying ultra-fast local models (<50ms TTFT). At that point, the LiteLLM path (Rust sidecar via PyO3 for hot paths) is pragmatic.

**Quick wins within Python (do now):**

| Optimization | Impact | Effort | Source |
|---|---|---|---|
| Convert `BaseHTTPMiddleware` to pure ASGI middleware | ~40% middleware overhead reduction | Low | LiteLLM blog: "Your Middleware Could Be a Bottleneck" |
| Try **Granian** as ASGI server (Rust HTTP parser) | 2–3x lower tail latency vs Uvicorn | Low (drop-in) | github.com/emmett-framework/granian |
| Ensure SQLite writes use `asyncio.to_thread()` | Prevents event loop blocking | Low | Python docs |
| Tune httpx connection pool (keepalive 30s, see §25.5) | Eliminates unnecessary TLS handshakes | Low | httpx docs |

**Future (12–18 months):** Python 3.14 free-threaded mode (no GIL) shows 2–3x multi-threaded speedup in benchmarks. The ecosystem (uvloop, Starlette, httpx) is not ready yet. Monitor and adopt when stable.

**References:**
- Cloudflare Pingora: blog.cloudflare.com/how-we-built-pingora
- LiteLLM benchmarks: docs.litellm.ai/docs/benchmarks
- Granian benchmarks: github.com/emmett-framework/granian/benchmarks
- Free-threaded Python: labs.quansight.org/blog/scaling-asyncio-on-free-threaded-python

---

### 25.2 Cryptography & Hashing

**Current:** ID-pointer chain (`record_id` UUIDv7 + `previous_record_id`) per session. The Walacor backend issues a tamper-evident `DH` (data hash, SHA3-512) on ingest as the cryptographic checkpoint — the gateway does not compute its own chain hash.

#### 25.2.1 Why the Walacor backend hashes, not the gateway

Centralising the hash computation in the Walacor backend keeps the cryptographic trust anchor independent of the gateway process. The gateway sends the full record (prompt, response, metadata, tool I/O); Walacor computes the `DH` and returns it. Tampering with a record on the gateway side has no effect on the Walacor-issued `DH`, which is the value an auditor will verify against.

#### 25.2.2 Signing for Non-Repudiation

| | Ed25519 Sign | Ed25519 Verify |
|---|---|---|
| Throughput | ~14,000 ops/s | ~6,000 ops/s |
| Latency | ~70μs | ~170μs |
| Output size | 64 bytes | — |
| Proves | Integrity + **Provenance** | — |

**What signing adds:** The chain pointers and Walacor `DH` prove a record was not modified. Ed25519 signatures prove **which gateway created the record**. This matters for multi-gateway deployments, third-party audits, and legal disputes. The gateway signs the canonical ID string (`record_id` + `session_id` + `timestamp`) so verifiers can reconstruct the signed payload from the WAL.

#### 25.2.3 Post-Quantum JWT Risk

SHA3-512 (used by Walacor on ingest) is quantum-safe for hashing. But **JWT signing algorithms (ECDSA/RSA) are quantum-broken by Shor's algorithm**. The real post-quantum vulnerability is in the auth path. When NIST's ML-DSA (FIPS 204) and SLH-DSA (FIPS 205) JWT implementations mature, we'll need to migrate. Our `jwt_algorithms` config already supports algorithm switching — this is crypto-agility done right.

---

### 25.3 Content Safety & PII Detection

#### 25.3.1 CRITICAL GAP: No Prompt Injection Detection

**Prompt injection is OWASP #1 LLM risk (LLM01:2025).** The gateway has zero defense.

**Best available solution:** Meta's **Prompt Guard 2 (22M parameters)** — a tiny DeBERTa-xsmall classifier that runs on CPU in **2–5ms**. Classifies inputs as benign/malicious. Fits perfectly into the existing `ContentAnalyzer` ABC. Run on input (pre-inference) and optionally on tool outputs (indirect injection).

| Model | Params | Latency | Accuracy | Notes |
|-------|--------|---------|----------|-------|
| Prompt Guard 2 (22M) | 22M | 2–5ms CPU | High | 75% less latency than 86M variant |
| Prompt Guard 2 (86M) | 86M | 5–15ms CPU | Higher (multilingual) | Better for non-English attacks |
| LlamaFirewall | Varies | Varies | AgentDojo: 17.6%→7.5% attack success | Full framework, heavier |

**Priority: P0 — implement immediately.**

#### 25.3.2 PII Detection: Regex vs NLP

**Current:** 7 compiled regex patterns. Sub-millisecond.

**Microsoft Presidio** (NLP/NER): Better at unstructured PII (names, addresses) but adds 5–10ms latency. Overkill for our hot path.

**Verdict: Keep regex for structured PII (credit cards, SSNs, keys — near-perfect accuracy). Add optional `PresidioPIIDetector` plugin using spaCy `en_core_web_sm` (7MB model) for deployments needing name/address detection. Don't replace, augment.**

**Intel Hyperscan** (SIMD regex): 400x faster than Python `re` at 500+ patterns. Irrelevant with only 7 patterns — our regex is already sub-millisecond.

#### 25.3.3 Llama Guard: Use the 1B Model

**Current:** Llama Guard 3 (8B params), 1–5 seconds on CPU.

**Better:** `llama-guard3:1b` — same 14-category coverage (S1–S14), dramatically lower latency (~0.3–1s), runs well on CPU. Drop-in replacement via config change.

**Even better:** **OpenGuardrails** (arXiv:2510.19169) — 14B model compressed to 3.3B via GPTQ, maintains 98% accuracy, supports **119 languages**, outperforms both Llama Guard and WildGuard. Apache 2.0 licensed. Has per-request configurable sensitivity thresholds.

#### 25.3.4 Streaming Content Moderation

**Current:** Regex on accumulated text in `stream_safety.py`.

**Research (arXiv:2506.09996):** Streaming Content Monitor (SCM) achieves 0.95+ F1 by seeing only the **first 18% of tokens**.

**Recommendation:** Extend `stream_safety.py` with **windowed PII/toxicity checks** — run lightweight analyzers every 50–100 tokens (~500 chars) instead of only at the end. For Llama Guard, keep it as post-stream (too slow for per-chunk).

#### 25.3.5 Watermarking — Not Applicable

The gateway cannot inject SynthID-style watermarks (requires model logit access). Detection without knowing the scheme is impractical. The ID-pointer chain plus the Walacor-issued `DH` and execution records provide better provenance than watermarking for a proxy use case.

#### 25.3.6 Audit Log Privacy (novel)

**Current:** Full plaintext prompts/responses stored in execution records.

**Recommendation:** Add **optional AES-256-GCM field-level encryption** for `prompt_text` and `response_text`. Config: `WALACOR_AUDIT_ENCRYPTION_KEY`. Metadata stays plaintext for querying. Satisfies GDPR "encryption at rest." **Do NOT use differential privacy** — wrong tool for compliance audit logs that need exact records.

---

### 25.4 Storage — Critical Performance Findings

#### 25.4.1 CRITICAL BUG: `wal_checkpoint(FULL)` After Every Write

**`writer.py` line 93 calls `PRAGMA wal_checkpoint(FULL)` after every single write.** This blocks until all readers complete, then copies all dirty pages back to the main database. It negates most of WAL mode's benefits.

**Impact:** Equivalent to a full fsync after every record. SQLite auto-checkpoints at 1,000 pages, which is dramatically faster.

**Fix:** Remove explicit `wal_checkpoint(FULL)` calls. Let SQLite auto-checkpoint. Expected: **5–10x write throughput improvement.**

#### 25.4.2 WALBackend Blocks the Event Loop

**`wal_backend.py`** calls sync SQLite methods inside `async def` — `write_durable()` performs INSERT + commit + checkpoint, all blocking the asyncio event loop.

**Fix:** Wrap in `asyncio.to_thread(self._writer.write_durable, record)`. Prevents blocking.

#### 25.4.3 StorageRouter Writes Sequentially

**`router.py`** iterates backends with `for backend in self._backends: await backend.write_execution(record)`. WAL write blocks, then Walacor HTTP POST blocks. Total = sum of both latencies.

**Fix (simple):** Use `asyncio.gather()` for parallel fan-out:
```python
results = await asyncio.gather(
    *(backend.write_execution(record) for backend in self._backends),
    return_exceptions=True
)
```
Latency drops to `max(WAL_time, Walacor_time)` instead of sum.

**Fix (better):** Write ONLY to WAL in the hot path. Let `DeliveryWorker` handle all Walacor delivery asynchronously. We already have this infrastructure (`delivered` flag, `get_undelivered()`, `mark_delivered()`). This eliminates Walacor network latency from the request path entirely.

#### 25.4.4 `synchronous=FULL` → `synchronous=NORMAL`

**Current:** `synchronous=FULL` in WALWriter. Still crash-safe in WAL mode but adds ~23% overhead vs NORMAL.

**WAL mode + NORMAL:** Prevents corruption on crash. The last 1–2 commits may be lost, but `DeliveryWorker` retry handles this. The control plane store should keep `FULL` (CRUD operations need stronger guarantees).

#### 25.4.5 Group Commit / Batch Writes (novel)

**Individual transactions: ~85 inserts/sec. Batched: ~96,000 inserts/sec.** (1000x improvement)

**Design:** Buffer records in an `asyncio.Queue`. Flush every 5–10ms or every 10–50 records in a single transaction. At 1,000 req/s with 10ms batching, you'd batch ~10 records per flush, reducing fsync calls from 1000/s to 100/s. The 5–10ms delay is negligible when provider calls take 500ms–5s.

#### 25.4.6 SQLite vs LMDB/RocksDB — Stay with SQLite

RocksDB: 20–30x write amplification (LSM compaction), key-value only (lose SQL queries for lineage dashboard). LMDB: 5–8x faster writes but also key-value only. SQLite with fixed configuration (remove checkpoint, NORMAL sync, batch writes) handles our scale (100–1,000 req/s) easily.

#### 25.4.7 Lineage Reader: Add mmap

Add `PRAGMA mmap_size = 268435456` (256MB) to `LineageReader` for faster dashboard SELECT queries. No help for writes.

**Summary of storage fixes (priority order):**

| # | Fix | Impact | Effort |
|---|-----|--------|--------|
| 1 | Remove `wal_checkpoint(FULL)` | 5–10x write throughput | 1 line |
| 2 | `asyncio.to_thread()` in WALBackend | Unblocks event loop | 3 lines |
| 3 | `asyncio.gather()` in StorageRouter | Halves write latency | 5 lines |
| 4 | `synchronous=NORMAL` for WALWriter | ~23% throughput gain | 1 line |
| 5 | Group commit via asyncio.Queue | 10–100x burst throughput | ~50 lines |
| 6 | mmap for LineageReader | Faster dashboard queries | 1 line |

---

### 25.5 Resilience & Routing

#### 25.5.1 Load Balancing: Replace Weighted Random with P2C

**Current:** `random.choices(healthy, weights=weights, k=1)` — no awareness of endpoint latency or queue depth.

**Better: Power of Two Choices (P2C).** Sample two random endpoints, pick the one with fewer outstanding requests. O(1) overhead. vLLM Router benchmarks show **25% higher throughput** than queue-aware approaches and **100% higher** than round-robin.

**Implementation:** Add `outstanding_requests: int` to `Endpoint`. Increment on forward, decrement on response/error. ~15 lines of code.

**Also add:** EWMA latency tracking per endpoint (alpha=0.2) as a P2C tiebreaker.

#### 25.5.2 Circuit Breaker Improvements

**Current issues:**
- No jitter → thundering herd when breakers close simultaneously
- No slow-call detection → a provider responding in 45s counts as "success"
- No exponential backoff on repeated opens

**Fixes:**
1. **Jitter:** `reset_timeout = base + random.uniform(-jitter, +jitter)` (30s ± 5s)
2. **Half-open probe limit:** Allow only 1–3 requests in half-open (prevent concurrent flooding)
3. **Slow-call detection:** If P50 is 2s but response takes >10s, count as failure
4. **Exponential backoff:** On Nth successive open, `timeout = min(base * 2^N, max_timeout)`

Reference: Resilience4j sliding-window model (resilience4j.readme.io)

#### 25.5.3 Adaptive Concurrency Limiting (novel — Netflix Gradient2)

**Current:** No concurrency limiting — gateway accepts unlimited concurrent requests.

**Netflix's Gradient2 algorithm:**
```
gradient = RTT_long_ewma / RTT_short_ewma
if gradient < 1.0: limit = limit * gradient  (queueing detected, decrease)
if gradient >= 1.0: limit = limit + 1        (healthy, additive increase)
```
Vector.dev reports **3x throughput improvement** over static limits.

**Implementation:** ~150 lines. Track per-provider EWMA latency (long: 600 samples, short: 20 samples). Clamp between min_limit (5) and max_limit (100). Return 503 with `Retry-After` when at limit.

#### 25.5.5 Rate Limiting: Consider GCRA

**Current:** Sliding window storing individual timestamps (O(N) per check).

**GCRA (Generic Cell Rate Algorithm):** O(1) memory per key, single value (theoretical arrival time). Used by Stripe and Cloudflare. Prevents boundary bursts that sliding window allows. Redis: 1–2 commands vs our current 4.

**Verdict:** Keep sliding window for in-memory (fine at <10K RPM). Consider GCRA for Redis at scale.

**Novel addition: Token-based rate limiting.** A 10-token request and a 100K-token request shouldn't count the same. Track tokens consumed per window alongside RPM.

#### 25.5.6 Connection Pool Tuning

**Current:** `max_connections=200, max_keepalive=50, keepalive_expiry=5s (default)`

**Fixes:**
- **Increase `keepalive_expiry` to 30s** — prevents re-handshaking between request bursts
- **Consider separate pools for streaming vs non-streaming** — streaming holds connections for seconds/minutes, can starve quick tool/health calls

#### 25.5.7 SSE Streaming

**Quick win:** Add `X-Accel-Buffering: no` header to streaming responses (prevents intermediate proxy buffering).

**Optimization:** Replace `accumulated_text +=` with a rolling 4KB window for stream safety — S4 patterns are short, no need for full response history.

---

### 25.6 AI Governance — Academic & Industry Advances

#### 25.6.1 Shadow Policy Evaluation (novel — nobody does this for LLM gateways)

**Concept:** Evaluate new policies against live traffic without enforcing them. Log "would_block" results alongside actual decisions. The lineage dashboard shows "this policy would have blocked 47 requests in the last hour" before you activate it.

**Implementation:** Add `shadow_policies` to the control plane. In `evaluate_pre_inference`, evaluate both active and shadow policy sets. Store `shadow_policy_result` in metadata. Zero enforcement, pure observation. **De-risks every policy change.**

#### 25.6.2 Policy Decision Explanations (EU AI Act compliance gap)

**Current:** 403 response returns `{"error": "Blocked by policy"}` — no explanation.

**EU AI Act Articles 13–14** mandate transparency about system limitations and decisions.

**Fix:** When `policy_evaluator.py` blocks, include:
```json
{
  "error": "Blocked by policy",
  "reason": "Rule 'require-active-attestation' failed: model status is 'revoked'",
  "policy_version": 3,
  "governance_decision": {
    "attestation": "passed",
    "pre_inference_policy": "blocked",
    "blocking_rule": "require-active-attestation",
    "field": "status",
    "expected": "active",
    "actual": "revoked"
  }
}
```

Similarly for content blocks: include category (e.g., "S4: child safety") and confidence score from `ContentAnalysisResult`.

#### 25.6.3 OPA/Rego Policy Engine Option

**Current:** Custom `walacor_core.policy_engine` with simple field-matching (equals, contains, greater_than).

**OPA/Rego:** CNCF-graduated, Datalog/Prolog derivative. A `yaml-opa-llm-guardrails` compiler already exists for LLM output guardrails. Cedar (Amazon) is 42–60x faster and formally verifiable but more restrictive.

**Recommendation:** Add `WALACOR_POLICY_ENGINE=builtin|opa` config switch. When `opa`, send attestation context to OPA's REST API. Keeps existing engine as default, unlocks enterprise-grade expressiveness. Don't replace, augment.

#### 25.6.4 Model Supply Chain Security (OpenSSF Model Signing)

**OpenSSF Model Signing v1.0** (April 2025): Production-ready specification for signing ML models using Sigstore. NVIDIA NGC and Google Kaggle adopting.

**For the gateway:** During model discovery (`control/discovery.py`), verify sigstore signatures on model artifacts before granting `active` attestation. Strongest available model provenance mechanism today.

#### 25.6.5 ISO 42001 Compliance Matrix

**ISO/IEC 42001:2023** (AI Management System): 38 controls covering governance, risk, lifecycle, third-party oversight. The gateway already covers audit/traceability. Add a compliance mapping document (like the existing `EU-AI-ACT-COMPLIANCE.md`) for ISO 42001 and NIST AI 600-1 (Generative AI Profile).

#### 25.6.6 Zero-Knowledge Proofs of Policy Compliance (frontier research)

**ZKMLOps** (arXiv:2510.26576, 2025) and **ZK Audit for Internet of Agents** (arXiv:2512.14737, 2025 — pairs zk-SNARKs with MCP!) show this is becoming practical.

**Tier 1 (implementable):** ZK proofs that policy evaluation was performed correctly — prove a policy was evaluated against input attributes and produced a result, **without revealing the prompt**. Uses simple arithmetic circuits (field comparisons, set membership). Fast to prove.

**Tier 2 (research/future):** ZK proofs of content analysis (proving a classifier ran on content). Current ZKML overhead (10–1000x) makes this impractical in the hot path.

---

### 25.7 Observability Improvements

#### 25.7.1 Multi-Span OTel Traces (replace retroactive single span)

**Current:** Single retroactive span per request in `emit_inference_span()`.

**Better:** Pipeline child spans that answer "this request was slow — was it the provider, the tool loop, or content analysis?"

| Span | What it reveals |
|------|----------------|
| `gateway.pipeline` (root) | Total request lifecycle |
| `gateway.auth` | Auth/identity resolution time |
| `gateway.policy.pre_inference` | Attestation + policy check latency |
| `gateway.forward` | Upstream provider latency (dominant cost) |
| `gateway.tool_loop` | Tool execution iterations |
| `gateway.content_analysis` | Llama Guard / PII / toxicity overhead |
| `gateway.audit_write` | WAL + Walacor write latency |

**Overhead:** ~24μs per request (8 spans × ~3μs each). Negligible.

#### 25.7.2 Cost Attribution Per User/Model (novel — high value, low effort)

**The data already flows through the pipeline** (tokens, user, model). Missing: a pricing table.

**Design:**
1. `model_pricing` table in control plane: `{model_id: {input_cost_per_token, output_cost_per_token}}`
2. Compute `estimated_cost_usd` in `_record_token_usage()`
3. New `/v1/lineage/cost` aggregation endpoint
4. Optional cost-based budgets: `max_cost_per_day_usd` (more intuitive than token limits)

**Latency overhead:** One dict lookup + multiplication. Negligible.

#### 25.7.3 Missing Metrics (RED Method Gaps)

| Gap | Metric to Add | Why |
|-----|---------------|-----|
| In-flight requests | `walacor_gateway_inflight_requests` gauge | Saturation signal for capacity planning |
| Error breakdown | `response_status_total{status_code=...}` | Distinguish upstream vs gateway vs policy errors |
| Event loop lag | `walacor_gateway_event_loop_lag_seconds` | The #1 "utilization" metric for ASGI |
| Per-model latency | `model` label on `forward_duration` | Reveal that qwen3:1.7b is fast but llama3:70b is slow |

#### 25.7.4 Anomaly Detection on Metrics

**Replace fixed thresholds with EWMA baselines.** Track per-provider `forward_duration` EWMA. Alert when `current > mean + 3*stddev`. Catches latency degradation that fixed thresholds miss. CPU cost: microseconds per update.

#### 25.7.5 Continuous Profiling (optional)

**Grafana Pyroscope** with Python SDK (wraps py-spy): 1–2% CPU overhead, always-on 100Hz sampling. Valuable for identifying whether JSON parsing or content analysis dominates under load. Add as optional `[profiling]` dependency.

---

### 25.8 Master Priority Table

All recommendations ranked by impact-to-effort ratio:

| # | Category | Action | Effort | Impact | Status |
|---|----------|--------|--------|--------|--------|
| **1** | Storage | Remove `wal_checkpoint(FULL)` | 1 line | **Critical** — 5–10x write throughput | BUG FIX |
| **2** | Storage | `asyncio.to_thread()` in WALBackend | 3 lines | **Critical** — unblocks event loop | BUG FIX |
| **3** | Storage | `asyncio.gather()` in StorageRouter | 5 lines | **High** — halves write latency | BUG FIX |
| **4** | Content | Add Prompt Guard 2 (22M) for injection detection | Medium | **High** — fills #1 OWASP gap | NEW |
| **5** | Governance | Shadow policy evaluation | Low | **High** — de-risks every policy change | NOVEL |
| **6** | Governance | Policy decision explanations | Low | **High** — EU AI Act compliance gap | NEW |
| **7** | Routing | P2C load balancing + outstanding-request tracking | Low (15 lines) | **High** — eliminates hot-spotting | UPGRADE |
| **8** | Observability | Cost attribution per user/model | Low | **High** — highest-value analytics | NEW |
| **9** | Storage | `synchronous=NORMAL` for WALWriter | 1 line | Medium — ~23% throughput | CONFIG |
| **10** | Content | Switch to `llama-guard3:1b` | Config change | Medium — 5x faster safety | CONFIG |
| **11** | Runtime | Convert to pure ASGI middleware | Low | Medium — ~40% middleware speedup | OPTIMIZE |
| **12** | Resilience | Circuit breaker: jitter + slow-call + backoff | Low (30 lines) | Medium — prevents cascades | UPGRADE |
| **13** | Observability | Multi-span OTel traces | Medium | Medium — enables debugging | UPGRADE |
| **14** | Resilience | Adaptive concurrency limiter (Gradient2) | Medium (150 lines) | Medium — prevents cascade failures | NOVEL |
| **15** | Governance | OPA/Rego policy engine option | Medium | Medium — enterprise expressiveness | NEW |
| **16** | Observability | In-flight requests gauge + event loop lag | Low | Medium — key operational signals | NEW |
| **17** | Storage | Group commit via asyncio.Queue | Medium (~50 lines) | Medium — 10–100x burst throughput | NOVEL |
| **18** | Content | OpenGuardrails/BingoGuard analyzer plugin | Medium | Medium — better accuracy, multilingual | NEW |
| **19** | Governance | OpenSSF Model Signing verification | Medium | Medium — supply chain security | NEW |
| **20** | Crypto | Optional Ed25519 record signing | Medium | Medium — non-repudiation | NOVEL |
| **21** | Content | Optional Presidio NER PII detector | Medium | Low — augments regex for names | NEW |
| **22** | Runtime | Benchmark with Granian ASGI server | Low | Low–Medium — better tail latency | TEST |
| **23** | Governance | ISO 42001 + NIST 600-1 compliance matrix | Low | Low–Medium — certification readiness | DOC |
| **24** | Governance | ZK proofs of policy compliance | High | High (long-term) — privacy-preserving | FRONTIER |
| **25** | Crypto | Keep ID-pointer chain + Walacor DH (no change needed) | None | N/A — already optimal | ✅ VALIDATED |
| **26** | Runtime | Keep Python (no language switch) | None | N/A — provider latency dominates | ✅ VALIDATED |
| **27** | Runtime | Keep httpx with HTTP/2 | None | N/A — multiplexing > raw speed | ✅ VALIDATED |

**Legend:** BUG FIX = existing issue to fix. NEW = feature to add. NOVEL = approach nobody has implemented in LLM gateways. UPGRADE = improve existing component. CONFIG = configuration change. FRONTIER = research-stage, long-term. ✅ VALIDATED = current approach is confirmed best.

---

### 25.9 Key Academic References

| Paper/System | Year | Key Finding | Relevance |
|---|---|---|---|
| Netflix, "Performance Under Load" | 2018 | Gradient2 adaptive concurrency: 3x throughput improvement | Adaptive concurrency limiting |
| AuditableLLM | MDPI 2025 | Hash-chain audit + ZK comparison; validates our approach | Confirms gateway architecture |
| Fontys "Institutional AI Sovereignty" | arXiv 2025 | 300-user gateway pilot validates proxy governance at scale | Validates our architecture |
| "Rethinking Tamper-Evident Logging" (Nitro) | ACM CCS 2025 | 10–25x improvement via eBPF co-designed crypto processing | Future WAL optimization |
| OpenGuardrails | arXiv 2025 | 3.3B model matches 14B accuracy, 119 languages | Better content safety |
| ZK Audit for Internet of Agents | arXiv 2025 | zk-SNARKs + MCP for privacy-preserving audit | ZK compliance proofs |
| BingoGuard | ICLR 2025 | Severity-tiered content moderation, SOTA accuracy | Enhanced content policies |
| Streaming Content Monitor | arXiv 2025 | 0.95+ F1 at 18% of tokens for streaming safety | Stream safety improvement |

---

### 25.10 What's Validated — Our Current Strengths

Several design decisions are **confirmed best-in-class** by the research:

1. **Walacor backend `DH` for hashing** — FIPS-compliant SHA3-512 issued by the backend on ingest; the gateway sends full records and lets the backend compute the canonical hash
2. **Python + Starlette + uvloop** — provider latency dwarfs proxy overhead; governance logic is where value lives
3. **httpx with HTTP/2** — multiplexing trumps raw client speed for a multi-provider proxy
4. **SQLite WAL mode** — right choice for embedded audit log (SQL queries for lineage, zero-config, single-file)
5. **ID-pointer session chain (UUIDv7)** — tamper-evident linkage with O(n) verification on short sessions; Walacor's `DH` provides the independent cryptographic checkpoint
6. **Completeness invariant** — every request gets an attempt record; academic literature validates this pattern
7. **Fail-open content analysis** — Llama Guard PASS on timeout matches industry practice (false positives worse than false negatives for availability)
8. **Dual-write to WAL + cloud** — the outbox pattern is the gold standard for reliable async delivery
9. **Auto-attestation** — "trust first, verify later" is the right UX for developer experience; control plane tightens later
10. **8-step pipeline architecture** — independently validated by Fontys 300-user institutional gateway pilot
