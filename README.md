# Walacor AI Security Gateway

**The governance enforcement and cryptographic audit layer for enterprise AI infrastructure.**

A production-grade, drop-in ASGI proxy that integrates with any LLM provider without changing application code. The gateway enforces five security guarantees on every inference request — model attestation, full-fidelity audit recording, pre-inference policy, post-inference content analysis, and session chain integrity — while feeding a cryptographic audit trail to the Walacor control plane. Providers stay providers; the governance layer is Walacor's.

---

## What it guarantees

| Guarantee | Description |
|---|---|
| **G1 — Model Attestation** | Every request is matched to a cryptographically attested model record. Unknown or unattested models are fail-closed. |
| **G2 — Full-fidelity audit** | Prompt text, response content, provider request ID, and model hash are persisted directly to Walacor's backend (or SQLite WAL when offline). Walacor's backend hashes on ingest — the gateway sends the full record. |
| **G3 — Pre-inference Policy** | Requests are evaluated against the active policy before being forwarded. Stale policies fail closed. |
| **G4 — Post-inference Content Gate** | Model responses are evaluated by pluggable content analyzers (PII, toxicity, custom) before being returned to the caller. |
| **G5 — Session Chain Integrity** | Conversation turns are linked via a Merkle chain (SHA3-512 over canonical fields), enabling tamper detection by the control plane. |

**Completeness Invariant:** `GEN_ATTEMPT = GEN + GEN_DENY + GEN_ERROR`
Every request — regardless of failure point — produces exactly one row in `gateway_attempts`. Auth failures, parse errors, and provider timeouts are all counted.

---

## Architecture

```
Client
  │  POST /v1/chat/completions (or /v1/messages, /v1/completions, /v1/custom, /generate)
  ▼
┌─────────────────────────────────────────────────────┐
│  completeness_middleware  ← outermost, always runs  │
│  ┌────────────────────────────────────────────────┐ │
│  │  api_key_middleware    ← inner, auth check     │ │
│  │  ┌──────────────────────────────────────────┐ │ │
│  │  │  orchestrator (8-step pipeline)          │ │ │
│  │  │                                          │ │ │
│  │  │  1. G1  Attestation lookup / refresh     │ │ │
│  │  │  2. G3  Pre-inference policy eval        │ │ │
│  │  │  2.5    WAL backpressure gate            │ │ │
│  │  │  2.6    Token budget check               │ │ │
│  │  │  3.     Forward to provider              │ │ │
│  │  │  4. G4  Post-inference content gate      │ │ │
│  │  │  5.     Token usage recording            │ │ │
│  │  │  6. G2  Build execution record            │ │ │
│  │  │  7. G5  Session Merkle chain             │ │ │
│  │  │  8. G2  Audit write (Walacor + WAL)       │ │ │
│  │  └──────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
  │
  ▼
Provider (OpenAI / Anthropic / HuggingFace / Ollama / Generic)
```

Streaming responses use a tee-buffer: chunks are forwarded to the caller in real time while being accumulated for post-stream audit recording via a Starlette `BackgroundTask`.

When `WALACOR_SERVER`, `WALACOR_USERNAME`, and `WALACOR_PASSWORD` are all set, the gateway writes to **both** Walacor's backend (`POST /envelopes/submit` via `WalacorClient`) **and** the local SQLite WAL. This dual-write ensures the lineage dashboard always has local data for browsing and chain verification, while Walacor's backend provides durable long-term storage. When Walacor credentials are unset, the gateway uses the SQLite WAL only.

### Data plane ↔ Control plane boundary

```
┌──────────────────────────────────────────────────────────────────┐
│                         DATA PLANE                               │
│                                                                  │
│   Client ──► Gateway ──► Provider                                │
│                │                                                 │
│                │  sync (attestations + policies, pull every 60s) │
│                │◄────────────────────────────────┐               │
│                │                                 │               │
│                │  direct write (WalacorClient)   │               │
│                │  POST /envelopes/submit          │               │
│                │  ── executions  (ETId 9000001)  │               │
│                │  ── attempts    (ETId 9000002)  │               │
│                │  ── tool events (ETId 9000003)  │               │
│                └────────────────────────────────►│               │
│                                                  │               │
└──────────────────────────────────────────────────┼───────────────┘
                                                   │
                                    ┌──────────────▼──────────────┐
                                    │       WALACOR BACKEND        │
                                    │                              │
                                    │  • Attestation registry      │
                                    │  • Policy store              │
                                    │  • walacor_gw_executions     │
                                    │  • walacor_gw_attempts       │
                                    │  • Chain integrity verifier  │
                                    └──────────────────────────────┘
```

The gateway writes full execution records directly to Walacor's backend via an authenticated HTTP client (`WalacorClient`). Walacor's backend handles hashing and long-term storage. In offline/fallback mode (no Walacor credentials), the gateway buffers records in a local SQLite WAL and delivers them when connectivity is restored. If the policy cache expires while the control plane is unreachable, the gateway fails closed.

---

## Execution record fields (G2)

Every allowed request produces one `ExecutionRecord` written to Walacor's backend (or the SQLite WAL in fallback mode):

| Field | Description |
|---|---|
| `execution_id` | UUID generated by the gateway for this turn |
| `prompt_text` | Actual prompt text sent to the model |
| `response_content` | Actual response content returned by the model |
| `provider_request_id` | ID assigned by the provider (e.g. `chatcmpl-xxx`, `msg_xxx`) — the interaction-level identifier from the model/user exchange |
| `model_hash` | Hash of the model weights/binary from the MEE (e.g. Ollama digest from `/api/show`). `null` for cloud providers. |
| `model_attestation_id` | Attestation record ID from the control plane |
| `model_id` | Actual model name (e.g. `qwen3:4b`, `gpt-4`). Used by the lineage dashboard for display. |
| `provider` | Provider that served this request (`ollama`, `openai`, `anthropic`, etc.) |
| `policy_version` | Policy version applied to this request |
| `policy_result` | `pass`, `blocked`, or `flagged` |
| `tenant_id` | Tenant this request belongs to |
| `gateway_id` | Gateway instance that processed the request |
| `timestamp` | ISO 8601 UTC timestamp |
| `session_id` | Session identifier (if provided by caller) |
| `sequence_number` | Turn index within the session (G5) |
| `previous_record_hash` | Hash of the preceding turn in the session chain (G5) |
| `record_hash` | SHA3-512 Merkle hash of this record's canonical fields (G5) |
| `latency_ms` | End-to-end request latency in milliseconds |
| `prompt_tokens` | Number of prompt tokens consumed |
| `completion_tokens` | Number of completion tokens generated |
| `total_tokens` | Total tokens (`prompt_tokens + completion_tokens`) |
| `thinking_content` | Model reasoning content (if thinking strip is enabled and the model produced `<think>` blocks) |
| `response_policy_result` | Post-inference content analysis outcome: `pass`, `blocked`, `flagged`, or `skipped` |
| `analyzer_decisions` | Array of `{analyzer_id, verdict, confidence, category, reason}` from all content analyzers |

---

## Supported providers

| Route | Provider | Adapter | Notes |
|---|---|---|---|
| `/v1/chat/completions`, `/v1/completions` | OpenAI and compatibles | `OpenAIAdapter` | Extracts `chatcmpl-xxx` provider request ID |
| `/v1/chat/completions`, `/v1/completions` | Ollama (local MEE) | `OllamaAdapter` | Fetches model digest from `/api/show`; select with `WALACOR_GATEWAY_PROVIDER=ollama` |
| `/v1/messages` | Anthropic Claude | `AnthropicAdapter` | Extracts `msg_xxx` provider request ID |
| `/generate` | HuggingFace Inference Endpoints | `HuggingFaceAdapter` | |
| `/v1/custom` | Any REST API | `GenericAdapter` | JSONPath config |

### Multi-model routing (single instance, multiple providers)

By default, all `/v1/chat/completions` requests route to the same provider configured via `WALACOR_GATEWAY_PROVIDER`. For deployments that need to serve GPT-4, Llama 3, and Claude from a single gateway instance, use the model routing table.

Set `WALACOR_MODEL_ROUTING_JSON` to a JSON array of routing rules. Each rule uses an fnmatch pattern matched against the request's `model` field (case-insensitive). The first matching rule wins; unmatched models fall through to path-based routing.

```bash
export WALACOR_MODEL_ROUTING_JSON='[
  {"pattern": "gpt-*",    "provider": "openai",    "url": "https://api.openai.com",     "key": "sk-..."},
  {"pattern": "claude-*", "provider": "anthropic", "url": "https://api.anthropic.com",  "key": "sk-ant-..."},
  {"pattern": "llama*",   "provider": "ollama",    "url": "http://localhost:11434",      "key": ""}
]'
```

With this config, `POST /v1/chat/completions` with `"model": "gpt-4"` routes to OpenAI; `"model": "llama3.2"` routes to Ollama — all through the same gateway instance and the same audit trail.

The value can also be a file path to a JSON file (same convention as `WALACOR_MCP_SERVERS_JSON`):

```bash
export WALACOR_MODEL_ROUTING_JSON=/etc/gateway/routes.json
```

### Connecting to a local MEE (Ollama / LM Studio)

The gateway is designed to act as the governance layer in front of Model Execution Environments (MEEs) like Ollama or LM Studio running on-device or on-prem.

**Ollama:**
```bash
# Select the Ollama adapter; point at Ollama's OpenAI-compat endpoint
export WALACOR_GATEWAY_PROVIDER=ollama
export WALACOR_PROVIDER_OLLAMA_URL=http://localhost:11434
walacor-gateway
```
The `OllamaAdapter` calls `POST /api/show` to retrieve the model's SHA256 digest from the Ollama registry and stores it as `model_hash` in the execution record. Digests are cached per model name with a configurable TTL (default 1800s, set via `WALACOR_OLLAMA_DIGEST_CACHE_TTL`).

For reasoning models (e.g., Qwen3), the adapter reads Ollama's native `reasoning` field from both non-streaming responses (`message.reasoning`) and streaming deltas (`delta.reasoning`). This content is stored as `thinking_content` in the execution record, separate from the visible response. For older Ollama versions that embed `<think>…</think>` tags in the content, the adapter falls back to tag stripping.

**LM Studio (and other OpenAI-compat servers):**
```bash
# LM Studio exposes the OpenAI-compat API — use the standard OpenAI adapter
export WALACOR_PROVIDER_OPENAI_URL=http://localhost:1234
walacor-gateway
```
LM Studio has no `/api/show` endpoint, so `model_hash` will be `null` in the execution record. The provider request ID (`chatcmpl-xxx`) is still captured.

---

## Quick start

```bash
# Install
pip install -e ./walacor-core
pip install -e "./Gateway[dev]"

# --- Walacor backend storage (recommended) ---
# Copy the example env file and fill in your Walacor credentials
cp .env.gateway.example .env.gateway
# edit .env.gateway: set WALACOR_SERVER, WALACOR_USERNAME, WALACOR_PASSWORD

# Transparent proxy mode + Walacor storage (skip governance, records go to Walacor)
export WALACOR_SKIP_GOVERNANCE=true
export WALACOR_SERVER=https://sandbox.walacor.com/api
export WALACOR_USERNAME=your-username
export WALACOR_PASSWORD=your-password
export WALACOR_GATEWAY_TENANT_ID=your-tenant
export WALACOR_PROVIDER_OPENAI_KEY=sk-...
walacor-gateway

# Full governance mode — Ollama (no control plane needed, auto-attestation)
export WALACOR_SKIP_GOVERNANCE=false
export WALACOR_ENFORCEMENT_MODE=enforced
export WALACOR_GATEWAY_TENANT_ID=tenant-abc
export WALACOR_GATEWAY_API_KEYS=my-secret-key
export WALACOR_GATEWAY_PROVIDER=ollama
export WALACOR_PROVIDER_OLLAMA_URL=http://localhost:11434
export WALACOR_WAL_PATH=/tmp/walacor-wal
walacor-gateway

# Full governance mode + Walacor storage — OpenAI
export WALACOR_SKIP_GOVERNANCE=false
export WALACOR_GATEWAY_TENANT_ID=tenant-abc
export WALACOR_SERVER=https://your-walacor-server/api
export WALACOR_USERNAME=your-username
export WALACOR_PASSWORD=your-password
export WALACOR_PROVIDER_OPENAI_KEY=sk-...
walacor-gateway

# Full governance mode + control plane (when available)
export WALACOR_GATEWAY_TENANT_ID=tenant-abc
export WALACOR_CONTROL_PLANE_URL=https://control.example.com
export WALACOR_SERVER=https://your-walacor-server/api
export WALACOR_USERNAME=your-username
export WALACOR_PASSWORD=your-password
export WALACOR_PROVIDER_OPENAI_KEY=sk-...
walacor-gateway
```

Point any OpenAI-compatible client at `http://localhost:8000`.

---

## Configuration

All variables use the `WALACOR_` prefix. Can also be placed in `.env` or `.env.gateway` (see `.env.gateway.example`). The gateway validates required fields at startup and fails fast if anything is missing.

### Walacor backend storage

Set all three to activate direct writes to Walacor. When any one is missing the gateway falls back to the SQLite WAL.

| Variable | Default | Description |
|---|---|---|
| `WALACOR_SERVER` | `""` | Walacor backend URL (e.g. `https://sandbox.walacor.com/api`) |
| `WALACOR_USERNAME` | `""` | Walacor backend username |
| `WALACOR_PASSWORD` | `""` | Walacor backend password |
| `WALACOR_EXECUTIONS_ETID` | `9000001` | Schema ETId for execution records (`walacor_gw_executions`) |
| `WALACOR_ATTEMPTS_ETID` | `9000002` | Schema ETId for attempt records (`walacor_gw_attempts`) |

### Core

| Variable | Default | Description |
|---|---|---|
| `WALACOR_GATEWAY_TENANT_ID` | *(required)* | Tenant identifier |
| `WALACOR_CONTROL_PLANE_URL` | `""` | Control plane base URL. When empty, governance runs with auto-attestation and pass-all policies (no sync). |
| `WALACOR_GATEWAY_API_KEYS` | `""` | Comma-separated API keys for caller auth. Empty = no auth required. |
| `WALACOR_CONTROL_PLANE_API_KEY` | `""` | Key the gateway sends when calling the control plane |
| `WALACOR_GATEWAY_ID` | `gw-<random>` | Stable instance identifier |
| `WALACOR_SKIP_GOVERNANCE` | `false` | `true` = transparent proxy (no attestation or policy); storage still active. A shared HTTP client with connection pooling is initialised in both modes. |
| `WALACOR_ENFORCEMENT_MODE` | `enforced` | `enforced` blocks on violations; `audit_only` forwards and logs shadow blocks |

### Caches & sync

| Variable | Default | Description |
|---|---|---|
| `WALACOR_ATTESTATION_CACHE_TTL` | `300` | Attestation cache TTL (seconds) |
| `WALACOR_POLICY_STALENESS_THRESHOLD` | `900` | Max policy age before fail-closed (seconds) |
| `WALACOR_SYNC_INTERVAL` | `60` | Pull-sync interval (seconds) |
| `WALACOR_GATEWAY_PROVIDER` | `openai` | Active provider (`openai`, `ollama`, `anthropic`, etc.) — used for attestation sync and adapter selection |

### WAL

| Variable | Default | Description |
|---|---|---|
| `WALACOR_WAL_PATH` | `/var/walacor/wal` | WAL database directory |
| `WALACOR_WAL_MAX_SIZE_GB` | `10.0` | Max WAL disk usage before fail-closed |
| `WALACOR_WAL_MAX_AGE_HOURS` | `72.0` | Max WAL record age |
| `WALACOR_WAL_HIGH_WATER_MARK` | `10000` | Undelivered record limit before rejecting new requests |
| `WALACOR_MAX_STREAM_BUFFER_BYTES` | `10485760` | Streaming tee buffer cap (10 MB) |

### Completeness tracking

| Variable | Default | Description |
|---|---|---|
| `WALACOR_COMPLETENESS_ENABLED` | `true` | Write one `gateway_attempts` row per request |
| `WALACOR_ATTEMPTS_RETENTION_HOURS` | `168` | Retention for attempt records (7 days) |

### Response policy / content analysis

| Variable | Default | Description |
|---|---|---|
| `WALACOR_RESPONSE_POLICY_ENABLED` | `true` | Enable post-inference content gate (G4) |
| `WALACOR_PII_DETECTION_ENABLED` | `true` | Enable built-in PII detector (`walacor.pii.v1`) |
| `WALACOR_TOXICITY_DETECTION_ENABLED` | `false` | Enable built-in toxicity detector (`walacor.toxicity.v1`) |
| `WALACOR_TOXICITY_DENY_TERMS` | `""` | Comma-separated extra deny terms added to toxicity detector |

### Token budget

| Variable | Default | Description |
|---|---|---|
| `WALACOR_TOKEN_BUDGET_ENABLED` | `false` | Enable token budget enforcement |
| `WALACOR_TOKEN_BUDGET_PERIOD` | `monthly` | `daily` or `monthly` |
| `WALACOR_TOKEN_BUDGET_MAX_TOKENS` | `0` | Max tokens per period per tenant (`0` = unlimited) |

### Session chain

| Variable | Default | Description |
|---|---|---|
| `WALACOR_SESSION_CHAIN_ENABLED` | `true` | Enable Merkle chain for session records (G5) |
| `WALACOR_SESSION_CHAIN_MAX_SESSIONS` | `10000` | Max concurrent sessions in memory (ignored when Redis is configured) |
| `WALACOR_SESSION_CHAIN_TTL` | `3600` | Session state TTL — inactive sessions evicted (seconds) |

### Redis (multi-replica state sharing)

| Variable | Default | Description |
|---|---|---|
| `WALACOR_REDIS_URL` | `""` | Redis connection URL (e.g. `redis://redis-svc:6379/0`). When set, session chain and budget state are shared across all replicas via Redis. When empty, in-memory trackers are used (single-replica only). |
| `WALACOR_MODEL_ROUTING_JSON` | `""` | JSON array of model routing rules (see [Multi-model routing](#multi-model-routing-single-instance-multiple-providers)), or a path to a JSON file. |
| `WALACOR_UVICORN_WORKERS` | `1` | Uvicorn worker count. Can be set to `>1` **only when `WALACOR_REDIS_URL` is configured** — without Redis, each worker has independent in-memory state and session chains / budget counters will diverge across workers. |

Install the Redis client library via the optional extra:

```bash
pip install "walacor-gateway[redis]"
```

### Provider URLs and keys

| Variable | Default | Description |
|---|---|---|
| `WALACOR_PROVIDER_OPENAI_URL` | `https://api.openai.com` | OpenAI base URL (also used for LM Studio, vLLM, etc.) |
| `WALACOR_PROVIDER_OPENAI_KEY` | `""` | OpenAI API key |
| `WALACOR_PROVIDER_OLLAMA_URL` | `http://localhost:11434` | Ollama base URL |
| `WALACOR_PROVIDER_OLLAMA_KEY` | `""` | Ollama API key (usually empty for local deployments) |
| `WALACOR_PROVIDER_ANTHROPIC_URL` | `https://api.anthropic.com` | Anthropic base URL |
| `WALACOR_PROVIDER_ANTHROPIC_KEY` | `""` | Anthropic API key |
| `WALACOR_PROVIDER_HUGGINGFACE_URL` | `""` | HuggingFace Inference Endpoint URL |
| `WALACOR_PROVIDER_HUGGINGFACE_KEY` | `""` | HuggingFace API key |
| `WALACOR_GENERIC_UPSTREAM_URL` | `""` | Generic adapter upstream URL |
| `WALACOR_GENERIC_MODEL_PATH` | `$.model` | JSONPath to model ID in request |
| `WALACOR_GENERIC_PROMPT_PATH` | `$.messages[*].content` | JSONPath to prompt in request |
| `WALACOR_GENERIC_RESPONSE_PATH` | `$.choices[0].message.content` | JSONPath to content in response |

### Llama Guard (G4 — model-based content safety)

| Variable | Default | Description |
|---|---|---|
| `WALACOR_LLAMA_GUARD_ENABLED` | `false` | Enable Llama Guard 3 content analyzer (`walacor.llama_guard.v3`) |
| `WALACOR_LLAMA_GUARD_MODEL` | `llama-guard3` | Ollama model name for Llama Guard |
| `WALACOR_LLAMA_GUARD_TIMEOUT_MS` | `10000` | Per-analysis timeout (milliseconds) |

Requires Ollama running locally with the `llama-guard3` model pulled (`ollama pull llama-guard3:1b`). Runs alongside the regex-based PII and toxicity analyzers — all three execute concurrently under enforced timeouts.

### Thinking content strip

| Variable | Default | Description |
|---|---|---|
| `WALACOR_THINKING_STRIP_ENABLED` | `true` | Strip `<think>…</think>` tokens from model responses before returning to caller. Stripped content is preserved as `thinking_content` in the execution record. |

For Ollama models that natively separate reasoning (e.g., Qwen3), the adapter reads the native `reasoning` field directly instead of parsing `<think>` tags. Both paths produce the same `thinking_content` field in the audit record.

### Tool-aware mode and built-in web search

| Variable | Default | Description |
|---|---|---|
| `WALACOR_TOOL_AWARE_ENABLED` | `false` | Enable MCP/tool interception in the pipeline |
| `WALACOR_MCP_SERVERS_JSON` | `""` | JSON array of MCP server definitions, or a file path |
| `WALACOR_WEB_SEARCH_ENABLED` | `false` | Register built-in web search tool (requires `tool_aware_enabled=true`) |
| `WALACOR_WEB_SEARCH_PROVIDER` | `duckduckgo` | Web search backend: `duckduckgo`, `brave`, or `serpapi` |
| `WALACOR_WEB_SEARCH_API_KEY` | `""` | API key for Brave or SerpAPI (not needed for DuckDuckGo) |
| `WALACOR_WEB_SEARCH_MAX_RESULTS` | `5` | Max results per search |

When tool-aware mode is active and the model supports tool calling, the gateway auto-detects whether to use the **passive** strategy (cloud providers report tool calls in their response) or the **active** strategy (gateway runs the tool loop itself for local models like Ollama). Active strategy forces streaming requests to non-streaming internally to intercept tool calls. The tool loop returns the model's final answer (after all tool executions) to the caller — intermediate `finish_reason: tool_calls` responses are consumed internally.

**Model capability auto-discovery:** Not all models support function calling. When the gateway sends tool definitions to a model and receives a 400/422 "does not support tools" error, it automatically strips the tool definitions and retries — the caller never sees the failure. The result is cached in a **model capability registry** so subsequent requests to the same model skip tool injection entirely. This means:

- Models that support tools (e.g., Qwen3, GPT-4) get the full tool loop
- Models that don't (e.g., Gemma3, Phi3) work normally without tools — no wasted retries after the first request
- Real SSE streaming is preserved for tool-unsupported models (no stream→non-stream override needed)
- The registry is visible in the `/health` endpoint under `model_capabilities`

The gateway handles concurrent requests to multiple models safely — session chains stay contiguous, budget tracking is consistent, and there are no race conditions even when different models share a session.

**Tool event auditing:** Every tool call produces a separate tool event record in both Walacor backend (ETId 9000003) and the local WAL. Each record contains the tool name, input data (actual function arguments), input/output SHA3-512 hashes, sources (URLs with titles for web search), content analysis results on tool output, duration, and iteration number. Content analyzers (PII, toxicity, Llama Guard) run on every tool output to detect indirect prompt injection via tool results.

### Lineage dashboard

| Variable | Default | Description |
|---|---|---|
| `WALACOR_LINEAGE_ENABLED` | `true` | Enable the lineage dashboard at `/lineage/` and API at `/v1/lineage/*` |

The lineage dashboard provides a read-only view into the local WAL database — session explorer, execution timeline, chain verification, and attempt completeness. See [Lineage dashboard](#lineage-dashboard) for details.

### Embedded control plane

| Variable | Default | Description |
|---|---|---|
| `WALACOR_CONTROL_PLANE_ENABLED` | `true` | Enable the embedded SQLite control plane for local attestation, policy, and budget management |
| `WALACOR_CONTROL_PLANE_DB_PATH` | `""` | SQLite database path. When empty, defaults to `{wal_path}/control.db` |

When enabled, the gateway embeds a CRUD control plane backed by SQLite. This provides local management of model attestations, policies, and token budgets via REST API and the dashboard's **Control** tab — no external control plane service required. Mutations immediately refresh in-memory caches (attestation, policy, budget). A local sync loop refreshes caches every `sync_interval` seconds, preventing the `fail_closed` state that occurs when policy caches go stale. See [Embedded control plane](#embedded-control-plane) for details.

### OpenTelemetry (OTel)

| Variable | Default | Description |
|---|---|---|
| `WALACOR_OTEL_ENABLED` | `false` | Enable OpenTelemetry trace export |
| `WALACOR_OTEL_ENDPOINT` | `http://localhost:4317` | OTLP gRPC collector endpoint |
| `WALACOR_OTEL_SERVICE_NAME` | `walacor-gateway` | OTel service name |

Requires the `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, and `opentelemetry-instrumentation-httpx` packages.

### Observability

| Variable | Default | Description |
|---|---|---|
| `WALACOR_METRICS_ENABLED` | `true` | Expose Prometheus metrics at `/metrics` |
| `WALACOR_LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Endpoints

| Path | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI / Ollama / LM Studio chat completions proxy |
| `/v1/completions` | POST | OpenAI text completions proxy |
| `/v1/messages` | POST | Anthropic Messages proxy |
| `/v1/custom` | POST | Generic adapter proxy |
| `/generate` | POST | HuggingFace inference proxy |
| `/v1/models` | GET | OpenAI-compatible model listing (from attested models) |
| `/health` | GET | JSON health status |
| `/metrics` | GET | Prometheus text format metrics |
| `/lineage/` | GET | Lineage dashboard (HTML SPA) |
| `/v1/lineage/sessions` | GET | List sessions with record counts |
| `/v1/lineage/sessions/{session_id}` | GET | Session execution timeline |
| `/v1/lineage/executions/{execution_id}` | GET | Full execution record + tool events |
| `/v1/lineage/attempts` | GET | Recent attempts with disposition stats |
| `/v1/lineage/trace/{execution_id}` | GET | Execution trace with pipeline timings for waterfall view |
| `/v1/lineage/verify/{session_id}` | GET | Server-side chain verification |
| `/v1/control/attestations` | GET | List model attestations |
| `/v1/control/attestations` | POST | Create/update attestation |
| `/v1/control/attestations/{id}` | DELETE | Remove attestation |
| `/v1/control/policies` | GET | List policies |
| `/v1/control/policies` | POST | Create policy |
| `/v1/control/policies/{id}` | PUT | Update policy |
| `/v1/control/policies/{id}` | DELETE | Remove policy |
| `/v1/control/budgets` | GET | List budgets |
| `/v1/control/budgets` | POST | Create/update budget |
| `/v1/control/budgets/{id}` | DELETE | Remove budget |
| `/v1/control/status` | GET | Comprehensive gateway control status |
| `/v1/control/discover` | GET | Scan configured providers for available models |
| `/v1/compliance/export` | GET | Compliance export (JSON, CSV, PDF) with framework mappings |
| `/v1/attestation-proofs` | GET | Attestation proofs (SyncClient format) |
| `/v1/policies` | GET | Active policies (SyncClient format) |

### `/health` response

**Walacor backend storage mode:**

```json
{
  "status": "healthy",
  "gateway_id": "gw-a1b2c3d4e5f6",
  "tenant_id": "tenant-abc",
  "enforcement_mode": "enforced",
  "uptime_seconds": 3600,
  "storage": {
    "backend": "walacor",
    "server": "https://sandbox.walacor.com/api",
    "executions_etid": 9000001,
    "attempts_etid": 9000002
  },
  "attestation_cache": { "entries": 12, "last_sync": "2026-02-18T10:00:00Z", "stale": false },
  "policy_cache":      { "version": 7,  "last_sync": "2026-02-18T10:00:00Z", "stale": false },
  "token_budget": {
    "period": "monthly",
    "period_start": "2026-02-01T00:00:00Z",
    "tokens_used": 142000,
    "max_tokens": 1000000,
    "percent_used": 14.2
  },
  "session_chain": { "active_sessions": 3 },
  "model_capabilities": {
    "gpt-4": { "supports_tools": true },
    "llama3.2": { "supports_tools": false }
  }
}
```

When the **Redis tracker** is active, `active_sessions` is reported as `"unavailable"` (counting all keys by prefix is too expensive in Redis). The Prometheus gauge `walacor_gateway_session_chain_active` is only updated when the in-memory tracker is in use.

```json
  "session_chain": { "active_sessions": "unavailable" }
}
```

**SQLite WAL fallback mode:**

```json
{
  "status": "healthy",
  ...
  "wal": {
    "pending_records": 0,
    "oldest_pending_seconds": null,
    "disk_usage_bytes": 4096,
    "disk_usage_percent": 0.0
  }
}
```

`status` values: `healthy` → `degraded` → `fail_closed`

---

## Audit-only mode

Set `WALACOR_ENFORCEMENT_MODE=audit_only` to run in shadow mode:

- Every request is **forwarded** regardless of attestation, policy, budget, or content violations
- Violations are logged as warnings and recorded in the execution record as `would_have_blocked: true` with a `would_have_blocked_reason` field
- The control plane receives complete audit trails, enabling safe baseline measurement before switching to `enforced`

---

## Content analyzers (G4)

**This is the extensibility point.** The `ContentAnalyzer` plugin interface is how the gateway addresses the "semantic blindness" gap in traditional proxy-based AI security tools — proxies that hash and record traffic without understanding what the model actually said. Every analyzer runs concurrently under an enforced per-analyzer timeout, returns a typed `Decision` (verdict, confidence, category, reason), and never stores or logs the text it analyzed.

The gateway ships three built-in analyzers. Any number of custom analyzers can be added without touching the pipeline.

### Built-in: `walacor.pii.v1`

Regex-based, deterministic, zero external dependencies. Detects:

| Pattern | Example |
|---|---|
| Credit card numbers | Visa, Mastercard, Amex, Discover (Luhn-plausible) |
| US Social Security Numbers | `123-45-6789` |
| Email addresses | `user@example.com` |
| US phone numbers | All common formats |
| IPv4 addresses | `192.168.1.1` |
| AWS access key IDs | `AKIAIOSFODNN7EXAMPLE` |
| API keys / tokens / secrets | `api_key: abc123...` patterns |

Uses severity tiers: high-risk PII (credit cards, SSNs, AWS access keys, API keys/tokens) returns `BLOCK` with `confidence=0.99`; low-risk PII (email addresses, phone numbers, IP addresses) returns `WARN` with `confidence=0.99`. This prevents false-positive blocks when models include example IPs or emails in educational responses. No detected content is logged or stored.

### Built-in: `walacor.toxicity.v1`

Deny-list pattern matching. Default categories:

| Category | Verdict |
|---|---|
| `self_harm_indicator` | WARN |
| `violence_instruction` | WARN |
| `child_safety` | BLOCK |
| `custom_deny_list` | WARN |

Extra terms added at startup via `WALACOR_TOXICITY_DENY_TERMS=term1,term2`.

### Built-in: `walacor.llama_guard.v3`

Model-based content safety using Meta's Llama Guard 3. Runs the response through a local Llama Guard model (via Ollama) to detect unsafe content categories including violence, sexual content, criminal planning, and more. Requires `WALACOR_LLAMA_GUARD_ENABLED=true` and the Llama Guard model pulled in Ollama.

| Configuration | Value |
|---|---|
| Backend | Ollama (local inference) |
| Default model | `llama-guard3` (1B parameter variant) |
| Timeout | 10 seconds (configurable) |
| Verdict | `BLOCK` for S4 (child safety), `WARN` for all other unsafe categories (S1–S3, S5–S14), `PASS` on `safe` |

Llama Guard runs concurrently with PII and toxicity analyzers — all three execute in parallel under enforced timeouts. No response content is stored by the analyzer.

> **Thinking model support:** For models that use `<think>` reasoning (e.g. qwen3), the thinking strip moves all content to `thinking_content`. Content analyzers automatically fall back to analyzing `thinking_content` when `content` is empty, ensuring safety classifiers always fire regardless of thinking mode.

### Writing a custom analyzer

Implement the `ContentAnalyzer` ABC from `gateway.content`:

```python
from gateway.content import ContentAnalyzer, Decision, Verdict

class MyAnalyzer(ContentAnalyzer):
    @property
    def analyzer_id(self) -> str:
        return "acme.my_analyzer.v1"

    @property
    def timeout_ms(self) -> int:
        return 100  # gateway enforces this via asyncio.wait_for

    async def analyze(self, text: str) -> Decision:
        if "forbidden" in text.lower():
            return Decision(
                verdict=Verdict.BLOCK,
                confidence=0.95,
                analyzer_id=self.analyzer_id,
                category="custom",
                reason="forbidden_term",
            )
        return Decision(
            verdict=Verdict.PASS,
            confidence=1.0,
            analyzer_id=self.analyzer_id,
            category="custom",
            reason="clean",
        )
```

Register it by appending to `ctx.content_analyzers` in `main.py`'s `_init_content_analyzers()`.

Verdict semantics:

| Verdict | Enforced mode | Audit-only mode |
|---|---|---|
| `PASS` | Response returned | Response returned |
| `WARN` | Response returned, `flagged` in WAL record | Same |
| `BLOCK` | 403 returned to caller | Forwarded, `would_have_blocked=true` in WAL |

---

## Horizontal scaling

The gateway supports horizontal scaling when Redis is configured. Without Redis, all replica-level state (session chains, budget counters) is in-process memory and diverges across pods.

```
                  ┌──────────────┐
  ──► replica 1 ──┤              ├── provider
                  │  Redis 7     │
  ──► replica 2 ──┤  (shared     ├── provider
                  │   state)     │
  ──► replica 3 ──┤              ├── provider
                  └──────────────┘
```

**With Redis (`WALACOR_REDIS_URL` set):**
- Session chain state (`gateway:session:{id}`) — `next_chain_values` is a read-only HGET (no pre-increment); `update()` atomically writes both `seq` and `hash` in one pipeline. This eliminates false chain gaps on transient write failures and ensures seq=0 for the first record in every session, matching in-memory behaviour.
- Budget counters (`gateway:budget:{tenant}:{user}:{period}`) — Lua atomic check-and-reserve with `estimated` tokens; after each LLM response, `record_usage` applies the `actual − estimated` delta via `INCRBY`/`DECRBY` so the counter tracks real token consumption.
- `WALACOR_UVICORN_WORKERS` can be set to `>1`

**Without Redis:**
- In-memory trackers are used (original behavior, no dependency change)
- Keep `WALACOR_UVICORN_WORKERS=1`

**Docker Compose with Redis:**
```bash
docker-compose --profile redis up
```

**Kubernetes (Helm):** Set `WALACOR_REDIS_URL: "redis://redis-svc:6379/0"` in `values.yaml` and configure a Redis deployment alongside the gateway.

---

## Session chain (G5)

When a request includes a `session_id`, the gateway links consecutive turns via a Merkle chain:

```
record_hash = SHA3-512(
    execution_id | policy_version | policy_result |
    previous_record_hash | sequence_number | timestamp
)
```

- First record uses `previous_record_hash = "000...000"` (128 zeros — genesis) and `sequence_number = 0` in both in-memory and Redis trackers
- Each subsequent record chains off the previous `record_hash`; `sequence_number` increments per turn within the session
- Sessions are evicted from memory after `WALACOR_SESSION_CHAIN_TTL` seconds of inactivity
- The control plane can detect tampering via broken hash chains and missing sequence numbers
- With Redis: `next_chain_values` is read-only — the seq counter is only advanced inside `update()` after a successful write, so transient write failures never create phantom sequence gaps

**Current scope:** chain integrity is enforced within the Walacor system. External anchoring (RFC 3161 timestamp authority or distributed ledger) — which would make the chain independently verifiable outside of Walacor infrastructure — is on the roadmap for V2 (see [Roadmap](#roadmap)).

---

## Prometheus metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `walacor_gateway_requests_total` | Counter | `provider`, `model`, `outcome` | Request outcomes |
| `walacor_gateway_attempts_total` | Counter | `disposition` | Completeness invariant — every attempt |
| `walacor_gateway_pipeline_duration_seconds` | Histogram | `step` | Pipeline step timing |
| `walacor_gateway_forward_duration_seconds` | Histogram | `provider` | Upstream latency |
| `walacor_gateway_response_policy_total` | Counter | `result` | G4 outcomes (`pass`, `blocked`, `flagged`, `skipped`) |
| `walacor_gateway_token_usage_total` | Counter | `tenant_id`, `provider`, `token_type` | Token consumption |
| `walacor_gateway_budget_exceeded_total` | Counter | `tenant_id` | Budget exhaustion events |
| `walacor_gateway_session_chain_active` | Gauge | — | Active session chain count |
| `walacor_gateway_wal_pending` | Gauge | — | Undelivered WAL records |
| `walacor_gateway_wal_disk_bytes` | Gauge | — | WAL disk usage |
| `walacor_gateway_wal_oldest_pending_seconds` | Gauge | — | Age of oldest undelivered record |
| `walacor_gateway_cache_entries` | Gauge | `cache_type` | Cache entry counts |
| `walacor_gateway_sync_last_success_seconds` | Gauge | `cache_type` | Seconds since last successful sync |
| `walacor_gateway_delivery_total` | Counter | `result` | WAL delivery outcomes |

`disposition` label values for `walacor_gateway_attempts_total`:

| Value | Meaning |
|---|---|
| `allowed` | Request completed and recorded |
| `denied_auth` | API key missing or invalid |
| `denied_attestation` | Model not attested |
| `denied_policy` | Pre-inference policy block |
| `denied_response_policy` | G4 content block |
| `denied_budget` | Token budget exhausted |
| `denied_wal_full` | WAL backpressure limit hit |
| `error_gateway` | Internal gateway error |
| `error_parse` | Request body could not be parsed |
| `error_provider` | Provider returned 5xx |
| `error_no_adapter` | No adapter for requested path |

---

## Storage backends

### Walacor backend (default when credentials are set)

When `WALACOR_SERVER`, `WALACOR_USERNAME`, and `WALACOR_PASSWORD` are all configured, the gateway writes directly to Walacor using `WalacorClient`:

- **`walacor_gw_executions`** (ETId=9000001) — full execution records: prompt text, response content, provider request ID, model hash, session chain fields
- **`walacor_gw_attempts`** (ETId=9000002) — one row per request (completeness invariant), all dispositions
- **`walacor_gw_tool_events`** (ETId=9000003) — one row per tool call: tool name, input data, input/output hashes, sources, content analysis, duration

`WalacorClient` authenticates with username/password (`POST /auth/login`), receives a JWT Bearer token, and includes it in every `POST /envelopes/submit` call. Token management features:

- **Proactive refresh** — background task wakes before JWT expiry (5 min lead time) and re-authenticates silently, avoiding 401 latency spikes under load
- **Reactive re-auth** — on any 401 the client re-authenticates immediately and retries the write once
- **Concurrency safety** — `asyncio.Lock` ensures the proactive refresh loop and reactive retry never call `/auth/login` concurrently

### SQLite WAL (fallback)

When Walacor credentials are not set, the gateway uses SQLite in WAL mode (`synchronous=FULL`) as a crash-safe append-only log:

- **`wal_records`** — execution records and tool event records; delivered to the control plane by the background delivery worker
- **`gateway_attempts`** — one row per request for all dispositions (local telemetry only)

Tool event records are stored in `wal_records` with `event_type='tool_call'` and linked to their parent execution via the `execution_id` field in the record JSON.

A background delivery worker retries undelivered records with exponential backoff (1 s initial, 60 s cap). The gateway is **fail-closed** when:

- Policy cache is stale beyond `WALACOR_POLICY_STALENESS_THRESHOLD`
- WAL pending count ≥ `WALACOR_WAL_HIGH_WATER_MARK`
- WAL disk usage ≥ `WALACOR_WAL_MAX_SIZE_GB`

### Dual-write mode (Walacor + WAL)

When both Walacor credentials and a WAL path are configured, step 8 writes to **both** backends. The Walacor backend provides durable long-term storage and the control plane integration path. The local WAL provides the data source for the lineage dashboard and serves as a fallback if the Walacor write fails. If the Walacor write fails, the WAL write still succeeds and the execution ID is set — no audit record is lost. Tool events follow the same dual-write pattern — each tool call is written to both Walacor (ETId 9000003) and the local WAL.

### Completeness invariant implementation note

`gateway_attempts` is written by `completeness_middleware`, which wraps every request as the outermost layer. Because Starlette's `BaseHTTPMiddleware` runs `call_next` in a separate anyio task, Python `ContextVar` mutations made inside the handler are not visible in the middleware's `finally` block. Disposition, provider, model ID, and execution ID are therefore propagated via `request.state` (which crosses task boundaries) in addition to `ContextVar` — the middleware reads `request.state` first with a `ContextVar` fallback. This invariant holds in both Walacor backend mode and SQLite WAL mode.

---

## Lineage dashboard

The gateway includes a built-in audit lineage dashboard served at `/lineage/`. It provides a read-only view into the local WAL database with no external dependencies (vanilla HTML/CSS/JS, CDN-loaded js-sha3 for client-side chain verification).

### Views

| View | Description |
|---|---|
| **Overview** | Stat cards (sessions, requests, enforcement), live throughput chart, recent sessions, and recent activity feed |
| **Live Throughput Chart** | Real-time canvas-based telemetry graph on the Overview page. Polls `/metrics` every 3 seconds, parses Prometheus counters, and plots requests/sec over a 3-minute window (60 data points). Gold line for total req/s, green fill for allowed, red fill for blocked. Animated pulse dot on latest point. Live counters: req/s, tokens/s, % allowed, total. |
| **Session Explorer** | Browse all sessions with record counts, model names, and last activity timestamps |
| **Session Timeline** | Ordered list of executions within a session, showing sequence numbers, model, policy result, chain hashes, visual chain links, and tool call badges for tool-augmented requests |
| **Execution Detail** | Full execution record including all fields, tool events with input data and sources, thinking content, and highlighted chain fields |
| **Tool Events** | Rich tool call display: tool name, type and source badges, terminal-style input data, clickable source links, SHA3-512 hashes, content analysis verdicts on tool output, duration, and iteration count |
| **Chain Verification** | Server-side and client-side SHA3-512 chain recomputation with per-record pass/fail status |
| **Token Usage & Latency Charts** | Dual canvas charts on the Overview page. Token chart shows stacked area for prompt vs completion tokens over time. Latency chart shows average inference latency. Both support live mode (polling `/metrics` every 3s) and historical mode (1h, 24h, 7d, 30d ranges via `/v1/lineage/token-latency`). Shared range selector with the throughput chart. |
| **Attempts / Completeness** | Recent attempt records with disposition statistics |

### API endpoints

All `/v1/lineage/*` endpoints return JSON. They bypass API key authentication (same as `/health` and `/metrics`).

| Endpoint | Query params | Returns |
|---|---|---|
| `GET /v1/lineage/sessions` | `limit`, `offset` | `[{session_id, record_count, last_activity}]` |
| `GET /v1/lineage/sessions/{session_id}` | — | `[{execution_id, record_json, sequence_number, ...}]` |
| `GET /v1/lineage/executions/{execution_id}` | — | `{record, tool_events}` |
| `GET /v1/lineage/attempts` | `limit`, `offset` | `{items, stats}` |
| `GET /v1/lineage/verify/{session_id}` | — | `{valid, records, errors}` |
| `GET /v1/lineage/metrics` | `range` (`1h`,`24h`,`7d`,`30d`) | Time-bucketed attempt metrics (throughput, allowed/blocked counts) |
| `GET /v1/lineage/token-latency` | `range` (`1h`,`24h`,`7d`,`30d`) | Time-bucketed token usage and latency aggregation for charts |

### Chain verification

The dashboard supports both server-side and client-side chain verification. The canonical hash input is:

```
SHA3-512(execution_id | policy_version | policy_result | previous_record_hash | sequence_number | timestamp)
```

Client-side verification uses js-sha3 to independently recompute every `record_hash` in the browser and verify `previous_record_hash` linkage — no trust in the server required.

### Configuration

Set `WALACOR_LINEAGE_ENABLED=false` to disable the dashboard and API endpoints. The lineage reader opens a **separate read-only** SQLite connection (`?mode=ro` + `PRAGMA query_only=ON`) to avoid interfering with the WAL writer.

---

## Embedded control plane

The gateway includes a built-in control plane backed by SQLite (`PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL`). It manages model attestations, policies, and token budgets locally — no external control plane service required.

### What it solves

| Problem | Solution |
|---|---|
| `fail_closed` after 15 minutes | Local sync loop refreshes policy cache every `sync_interval` seconds, keeping `fetched_at` fresh |
| No way to approve/revoke models | CRUD API + dashboard Control tab for attestation management |
| No way to manage policies | Policy CRUD with rules builder, enforcement level, active/disabled status |
| No way to set budgets without restart | Budget CRUD with immediate cache refresh — changes take effect on the next request |
| No fleet management | One gateway is the "primary"; others pull via SyncClient from its `/v1/attestation-proofs` and `/v1/policies` endpoints |

### API

All `/v1/control/*` endpoints require API key authentication (sent via `X-API-Key` header). The sync-contract endpoints (`/v1/attestation-proofs`, `/v1/policies`) also require API key auth so remote gateways can pull securely.

| Endpoint | Method | Description |
|---|---|---|
| `/v1/control/attestations` | GET | List attestations. Optional `?tenant_id=` filter. |
| `/v1/control/attestations` | POST | Create or update attestation. Body: `{model_id, provider, status, notes}` |
| `/v1/control/attestations/{id}` | DELETE | Remove attestation by ID |
| `/v1/control/policies` | GET | List policies. Optional `?tenant_id=` filter. |
| `/v1/control/policies` | POST | Create policy. Body: `{policy_name, enforcement_level, description, rules: [{field, operator, value}]}` |
| `/v1/control/policies/{id}` | PUT | Update policy fields |
| `/v1/control/policies/{id}` | DELETE | Remove policy |
| `/v1/control/budgets` | GET | List budgets. Optional `?tenant_id=` filter. |
| `/v1/control/budgets` | POST | Create or update budget. Body: `{tenant_id, user, period, max_tokens}` |
| `/v1/control/budgets/{id}` | DELETE | Remove budget |
| `/v1/control/status` | GET | Comprehensive gateway status (caches, WAL, sync mode, model capabilities, auth, providers, budgets) |
| `/v1/control/discover` | GET | Scan Ollama and OpenAI for available models. Returns `{models: [{model_id, provider, source, registered}]}` |

**Side effects:** Every mutation (POST/PUT/DELETE) immediately refreshes the corresponding in-memory cache. Attestation changes clear and repopulate `attestation_cache`. Policy changes call `policy_cache.set_policies()` with a new version and fresh `fetched_at`. Budget changes call `budget_tracker.configure()` for each DB budget and `budget_tracker.remove()` for deleted ones.

### Fleet sync

For multi-gateway deployments, designate one gateway as the primary. Other gateways set `WALACOR_CONTROL_PLANE_URL` pointing to the primary's base URL and `WALACOR_CONTROL_PLANE_API_KEY` to a shared key. The existing `SyncClient` pulls attestations and policies from the primary every `sync_interval` seconds.

```
Gateway A (primary)  ←── manages attestations/policies via Control tab
    │
    ├── /v1/attestation-proofs  ──► Gateway B (SyncClient pulls every 60s)
    └── /v1/policies            ──► Gateway C (SyncClient pulls every 60s)
```

When `WALACOR_CONTROL_PLANE_URL` points to the gateway itself (localhost + same port), SyncClient creation is skipped and the gateway loads directly from its local DB.

### Dashboard — Control tab

The lineage dashboard at `/lineage/` includes a **Control** tab with four sub-views:

| Sub-view | Description |
|---|---|
| **Models** | Table of attested models with status badges, verification levels, and approve/revoke/remove actions. "Discover Models" button scans configured providers (Ollama, OpenAI) and shows available models with one-click Register or Register All. Inline "Add Model" form. |
| **Policies** | Table of policies with enforcement level badges, rule counts, and edit/delete actions. Policy editor with rules builder (field/operator/value rows). |
| **Budgets** | Table of budgets with usage progress bars (green→amber→red gradient). Inline "Add Budget" form. |
| **Status** | Read-only card grid showing gateway info, cache status, WAL status, model capabilities, sync mode, auth & security (auth mode, JWT, content analyzers), configured providers, and runtime state (active sessions, token budget, model routes). |

The Control tab requires API key authentication — entering the key in the auth card stores it in `sessionStorage` for the session.

---

## Auto-attestation (governance without control plane)

The gateway supports full governance mode without a control plane service. When `WALACOR_CONTROL_PLANE_URL` is not set:

| Feature | Behavior |
|---|---|
| **Attestation (G1)** | Models are auto-attested on first use. A `CachedAttestation` with `verification_level=self_attested` and `attestation_id=self-attested:{model}` is created and cached. |
| **Policy (G3)** | An empty pass-all policy set is seeded at startup. All requests pass pre-inference policy. |
| **Sync loop** | Not started — no background polling. |
| **Session chain (G5)** | Fully operational (local or Redis). |
| **Content analysis (G4)** | Fully operational (PII, toxicity, Llama Guard). |
| **Token budget** | Fully operational. |
| **Audit (G2)** | Fully operational (WAL and/or Walacor backend). |
| **Completeness** | Fully operational. |

This mode is designed for deployments where all models are trusted (e.g., on-premise Ollama) and the organization wants governance enforcement (content analysis, budgets, session chains, audit trail) without deploying a separate control plane service. When a control plane becomes available, set `WALACOR_CONTROL_PLANE_URL` to enable centrally managed attestations and policies.

---

## OpenAI compatibility layer (Phase 23)

### GET /v1/models

Returns an OpenAI-compatible model list from the embedded control plane's attested models. Only models with `status: active` are included; revoked models are filtered out. The endpoint requires no authentication and is excluded from completeness tracking.

```json
{"object": "list", "data": [{"id": "qwen3:4b", "object": "model", "created": 1741600000, "owned_by": "ollama"}]}
```

### Governance response headers

Non-streaming responses include `X-Walacor-*` headers for client-side audit correlation:

| Header | Description |
|---|---|
| `X-Walacor-Execution-Id` | Unique execution record identifier |
| `X-Walacor-Attestation-Id` | Model attestation used for this request |
| `X-Walacor-Chain-Seq` | Session chain sequence number |
| `X-Walacor-Policy-Result` | Pre-inference policy result (`allowed`, `denied`, etc.) |

For streaming responses, an `event: governance` SSE event is emitted after `data: [DONE]` with the same fields in a JSON payload.

### SSE keepalives

The streaming forwarder includes an `sse_keepalive_generator` that yields `: keepalive\n\n` SSE comment lines at 15-second intervals. This prevents proxy/load-balancer idle timeouts during long-running model inferences.

---

## Compliance export API (Phase 24)

The gateway provides regulatory compliance export capabilities via `GET /v1/compliance/export`, supporting four major frameworks and three output formats.

### Supported frameworks

| Framework | ID | Coverage |
|---|---|---|
| **EU AI Act** | `eu_ai_act` | Articles 12 (Record-Keeping), 14 (Human Oversight) |
| **NIST AI RMF** | `nist` | Govern, Map, Measure, Manage functions |
| **SOC 2 Type II** | `soc2` | CC7.2, CC7.3, CC8.1 trust criteria |
| **ISO 42001** | `iso42001` | Clauses 6.1, 8.4, 9.1, 10.1 |

### Query parameters

| Parameter | Required | Description |
|---|---|---|
| `start` | Yes | Start date (YYYY-MM-DD) |
| `end` | Yes | End date (YYYY-MM-DD) |
| `framework` | No | Framework ID (default: `eu_ai_act`) |
| `format` | No | Output format: `json` (default), `csv`, `pdf` |

### Example

```bash
# JSON compliance report for EU AI Act
curl "http://localhost:8080/v1/compliance/export?start=2026-03-01&end=2026-03-10&framework=eu_ai_act"

# CSV export for SOC 2
curl "http://localhost:8080/v1/compliance/export?start=2026-03-01&end=2026-03-10&framework=soc2&format=csv"

# PDF report (requires WeasyPrint)
curl -o report.pdf "http://localhost:8080/v1/compliance/export?start=2026-03-01&end=2026-03-10&format=pdf"
```

### Report contents

Each export includes:
- **Executive summary** — total requests, allowed/denied counts, models used
- **Model attestation inventory** — per-model request counts and token usage
- **Chain verification results** — session integrity status via Merkle chain verification
- **Framework compliance mapping** — requirement-level status (compliant/partial/non_compliant) with evidence references
- **Sample execution records** — up to 10 recent execution records (full data in JSON/CSV)

### Dashboard

The Lineage dashboard includes a **Compliance** tab with date range pickers, framework selector, preview panel with summary statistics, and download buttons for JSON/CSV/PDF exports.

---

## Resilience layer (Phase 25)

The gateway includes a resilience layer for fault tolerance across multiple provider endpoints.

### Model groups

Configure multiple endpoints per model with weighted load balancing via `WALACOR_MODEL_GROUPS_JSON`:

```bash
export WALACOR_MODEL_GROUPS_JSON='{
  "gpt-*": [
    {"url": "https://api1.openai.com", "key": "sk-1", "weight": 7},
    {"url": "https://api2.openai.com", "key": "sk-2", "weight": 3}
  ],
  "claude-*": [
    {"url": "https://api.anthropic.com", "key": "sk-ant-1", "weight": 1}
  ]
}'
```

Endpoints are selected via weighted random — weight 7 vs 3 gives ~70/30 traffic split. Unhealthy endpoints enter a cooldown period and are automatically re-enabled when it expires.

### Circuit breakers

Each model ID has an independent circuit breaker. After 5 consecutive failures, the circuit opens and requests are routed to fallback endpoints. After a configurable reset timeout, the circuit enters half-open state — a single success closes it.

### Retry with backoff

Transient errors (503, 429, 500, 502, 504, network errors) are retried with exponential backoff via tenacity. Non-retryable errors (400, 401, 403) fail immediately. Default: 2 attempts.

### Error-specific fallback

Provider errors are classified and routed accordingly:

| Error class | Action |
|---|---|
| `rate_limited` (429) | Retry with backoff, then fallback to next endpoint |
| `server_error` (5xx) | Retry with backoff, then fallback to next endpoint |
| `context_overflow` | Fallback to next endpoint (potentially larger context model) |
| `content_policy` | No retry — return error to client |

### Execution record tracking

Retry attempts are linked via the `retry_of` field in execution records, enabling audit trail reconstruction across retry chains.

## Rate limiting and alerting (Phase 26)

### Request rate limiting

The gateway enforces per-user request rate limits using a sliding window algorithm. Configure via environment variables:

| Variable | Default | Description |
|---|---|---|
| `WALACOR_RATE_LIMIT_ENABLED` | `false` | Enable request rate limiting |
| `WALACOR_RATE_LIMIT_RPM` | `60` | Requests per minute limit |
| `WALACOR_RATE_LIMIT_PER_MODEL` | `true` | Rate limit per user+model (vs per user only) |

When a request exceeds the rate limit, the gateway returns HTTP 429 with standard headers:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 12
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1741640000
```

Successful responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers. The rate limiter supports both in-memory (single-node) and Redis-backed (multi-replica) modes.

### Alert event bus

The gateway emits alerts for budget threshold crossings and dispatches them to external systems:

| Variable | Default | Description |
|---|---|---|
| `WALACOR_WEBHOOK_URLS` | `""` | Comma-separated webhook URLs for alerts |
| `WALACOR_PAGERDUTY_ROUTING_KEY` | `""` | PagerDuty Events API v2 routing key |
| `WALACOR_ALERT_BUDGET_THRESHOLDS` | `70,90,100` | Budget usage % thresholds for alerts |

**Dispatchers:**
- **Webhook** — generic JSON POST to any URL
- **Slack** — Block Kit formatted messages (auto-detected from `hooks.slack.com` URLs)
- **PagerDuty** — Events API v2 with severity mapping (info/warning/critical)

Budget threshold alerts fire once per threshold per budget period. The alert bus uses an async queue with fail-open semantics — alert delivery failures never block request processing.

### Prometheus metrics (Phase 26)

New metrics for monitoring rate limiting and content analysis:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `walacor_gateway_rate_limit_hits_total` | Counter | `model` | Rate limit 429 responses |
| `walacor_gateway_content_blocks_total` | Counter | `analyzer` | Content analysis blocks |
| `walacor_gateway_budget_utilization_ratio` | Gauge | `tenant_id` | Budget utilization 0-1 |

## Governance waterfall trace view (Phase 27)

Every non-streaming request captures per-step timing data in the execution record:

| Field | Description |
|---|---|
| `attestation_ms` | Model attestation lookup time |
| `policy_ms` | Pre-inference policy evaluation |
| `budget_ms` | Token budget check time |
| `pre_checks_ms` | Total pre-check overhead (all above combined) |
| `forward_ms` | Provider forward + response parse |
| `content_analysis_ms` | Post-inference content analyzers (PII, toxicity, Llama Guard) |
| `chain_ms` | Merkle session chain update |
| `write_ms` | Audit record write (Walacor + WAL) |
| `total_ms` | Total pipeline duration |

**Trace API:** `GET /v1/lineage/trace/{execution_id}` returns the execution record, tool events, and timings in a single response for the dashboard's waterfall view.

**Dashboard:** The Execution detail view renders a canvas-based waterfall chart showing each pipeline step as a color-coded horizontal bar proportional to its duration. Tool calls appear as nested gold bars under the forward step.

---

## Roadmap

The following capabilities are planned for V2. None of these change the core guarantees — they extend them.

### External Merkle anchoring
Periodically publish the WAL root hash to an RFC 3161 timestamp authority or a public distributed ledger. This makes the audit chain independently verifiable by auditors and regulators without access to Walacor infrastructure. The on-gateway chain structure (G5) is already designed to support this; the anchor publication step is the remaining piece.

### Pre-attestation webhook
Before forwarding a request, call a configurable webhook that receives the prompt text, model ID, and policy context. The webhook returns allow/deny. Enables integration with external decisioning systems (DLP, CASB, custom classifiers) without building them into the gateway binary.

### Trusted Execution Environment (TEE) deployment mode
Run the gateway inside an AWS Nitro Enclave or Azure Confidential Container, with remote attestation of the gateway process itself. Combined with G1 model attestation, this closes the remaining trust gap: the gateway's integrity is verifiable by the control plane, not just assumed.

### Per-user quota API
The embedded control plane provides budget CRUD (`/v1/control/budgets`) with per-user granularity and dynamic updates. Rate limiting (Phase 26) adds `X-RateLimit-*` headers on every response. The remaining piece is a public-facing quota API endpoint with usage reporting for product-level metering.

---

## Development

```bash
# Install
pip install -e ./walacor-core
pip install -e "./Gateway[dev]"

# Run tests
pytest

# Set up credentials (copy template, fill in values — never committed)
cp .env.gateway.example .env.gateway

# Run locally with hot reload — audit-only mode (no governance enforcement)
source .env.gateway
WALACOR_SKIP_GOVERNANCE=true \
uvicorn gateway.main:app --reload --port 8000

# Run with full governance — no control plane needed (auto-attestation + pass-all policies)
source .env.gateway
WALACOR_SKIP_GOVERNANCE=false \
WALACOR_ENFORCEMENT_MODE=enforced \
WALACOR_GATEWAY_TENANT_ID=dev-tenant \
WALACOR_GATEWAY_PROVIDER=ollama \
WALACOR_PROVIDER_OLLAMA_URL=http://localhost:11434 \
WALACOR_WAL_PATH=/tmp/walacor_wal \
uvicorn gateway.main:app --port 8000

# Run with full governance + control plane (when available)
source .env.gateway
WALACOR_CONTROL_PLANE_URL=http://127.0.0.1:9000 \
WALACOR_GATEWAY_TENANT_ID=dev-tenant \
WALACOR_PROVIDER_OPENAI_KEY=sk-... \
uvicorn gateway.main:app --port 8000
```

Requirements: Python 3.12+, `walacor-core` package.

---

## Documentation

| Document | Audience | Description |
|---|---|---|
| [How It Works](docs/HOW-IT-WORKS.md) | Engineers, technical managers | Complete walkthrough: pipeline, tool execution, MCP, content analysis, audit trail |
| [Visual Workflow](docs/gateway-workflow.html) | Presentations, demos | Interactive HTML diagram of the full architecture and pipeline |
| [Executive Briefing](docs/WIKI-EXECUTIVE.md) | CEO, leadership | Narrative explanation of what we built and why |
| [Overview](OVERVIEW.md) | Quick reference | One-page summary |
| [EU AI Act Compliance](docs/EU-AI-ACT-COMPLIANCE.md) | Compliance, legal | EU AI Act, NIST AI RMF, SOC 2 mapping |
| [Flow & Soundness](docs/FLOW-AND-SOUNDNESS.md) | Security reviewers | Pipeline flowcharts + soundness analysis |
| [Configuration](docs/CONFIGURATION.md) | DevOps, operators | All config variables |
| [Adapters](docs/ADAPTERS.md) | Engineers | Provider adapter details |
| [Quick Start](docs/QUICKSTART.md) | New users | Getting started guide |

---

## License

Apache 2.0
