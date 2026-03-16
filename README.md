# Walacor AI Security Gateway

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) ![License](https://img.shields.io/badge/license-Apache%202.0-green)

**Governance enforcement and cryptographic audit layer for enterprise AI infrastructure.**

A drop-in proxy that sits between your application and any LLM provider. No code changes required. Every inference request passes through an 8-step governance pipeline that enforces five security guarantees before the response reaches the caller.

Available in two deployment modes: **Python-only** (ASGI, single process) or **Hybrid Go/Python** (Go HTTP proxy + Python gRPC governance sidecar) for maximum throughput.

> **Shadow mode for safe rollout:** Set `WALACOR_ENFORCEMENT_MODE=audit_only` to record and analyze traffic without blocking anything. Use this to validate policies and tune content analyzers before enabling enforcement.

| Guarantee | What it does |
|---|---|
| **G1 — Model Attestation** | Only cryptographically attested models can serve requests. Unknown models are blocked. |
| **G2 — Full-fidelity Audit** | Prompt, response, provider request ID, and model hash are persisted to Walacor backend (or local SQLite WAL). |
| **G3 — Pre-inference Policy** | Requests are evaluated against active policy rules before forwarding. Stale policies fail closed. |
| **G4 — Content Gate** | Responses pass through pluggable analyzers (PII, toxicity, Llama Guard) before reaching the caller. |
| **G5 — Session Chain** | Conversation turns are linked via SHA3-512 Merkle chain for tamper detection. |

**Completeness invariant:** Every request produces exactly one audit row — allowed, denied, or errored.

---

## Architecture

```
Client
  |  POST /v1/chat/completions (or /v1/messages, /v1/completions, /generate)
  v
+---------------------------------------------------+
|  completeness_middleware  (outermost, always runs) |
|  +-----------------------------------------------+|
|  |  api_key_middleware  (auth check)              ||
|  |  +-------------------------------------------+||
|  |  |  orchestrator (8-step pipeline)            |||
|  |  |                                            |||
|  |  |  1. Attestation lookup              (G1)  |||
|  |  |  2. Pre-inference policy eval       (G3)  |||
|  |  |  3. Budget check + WAL backpressure       |||
|  |  |  4. Forward to provider                   |||
|  |  |  5. Post-inference content gate     (G4)  |||
|  |  |  6. Token usage recording                 |||
|  |  |  7. Build execution record + chain  (G5)  |||
|  |  |  8. Audit write (Walacor + WAL)     (G2)  |||
|  |  +-------------------------------------------+||
|  +-----------------------------------------------+|
+---------------------------------------------------+
  |
  v
Provider (OpenAI / Anthropic / Ollama / HuggingFace / Generic)
```

Streaming responses use a tee-buffer: chunks flow to the caller in real time while being accumulated for post-stream audit recording.

### Hybrid Go/Python Mode

```
Client
  |  POST /v1/chat/completions
  v
+---------------------------------------------------+
|  Go Proxy (port 8080)                             |
|  - HTTP ingress, auth, rate limiting              |
|  - SSE streaming with batched flushes             |
|  - Async post-inference for lowest latency        |
|  +-----------------------------------------------+|
|  |  gRPC  ←→  Python Sidecar (port 50051)        ||
|  |  - Full governance pipeline                   ||
|  |  - Content analysis, policy engine            ||
|  |  - Audit trail, session chains                ||
|  +-----------------------------------------------+|
+---------------------------------------------------+
  |
  v
Provider (OpenAI / Anthropic / Ollama / HuggingFace)
```

Deploy with `docker compose -f deploy/docker-compose.hybrid.yml up`. See [Hybrid Architecture Plan](docs/plans/2026-03-14-hybrid-architecture-and-competitive-features.md) for design details and benchmarks.

---

## Quick Start

```bash
# Install (single package — no external dependencies needed)
pip install -e ".[dev]"

# Minimal — transparent proxy (no governance, records to WAL)
export WALACOR_SKIP_GOVERNANCE=true
export WALACOR_PROVIDER_OPENAI_KEY=sk-...
walacor-gateway

# Full governance — Ollama, no control plane needed
# Prerequisites: install Ollama (https://ollama.com), then:
#   ollama serve
#   ollama pull qwen3:4b   (or any model)
export WALACOR_SKIP_GOVERNANCE=false
export WALACOR_GATEWAY_TENANT_ID=tenant-abc
export WALACOR_GATEWAY_API_KEYS=my-secret-key
export WALACOR_GATEWAY_PROVIDER=ollama
export WALACOR_PROVIDER_OLLAMA_URL=http://localhost:11434
export WALACOR_WAL_PATH=/tmp/walacor-wal
walacor-gateway
```

Point any OpenAI-compatible client at `http://localhost:8000`. All config uses the `WALACOR_` prefix and can go in `.env.gateway` (see `.env.gateway.example`).

Open `http://localhost:8000/lineage/` for the audit dashboard.

---

## Supported Providers

| Route | Provider | Notes |
|---|---|---|
| `/v1/chat/completions` | **OpenAI** and compatibles | Extracts `chatcmpl-xxx` provider request ID |
| `/v1/chat/completions` | **Ollama** (local) | Fetches model SHA256 digest; set `WALACOR_GATEWAY_PROVIDER=ollama` |
| `/v1/messages` | **Anthropic Claude** | Extracts `msg_xxx` provider request ID |
| `/generate` | **HuggingFace** | Inference Endpoints |
| `/v1/custom` | **Any REST API** | JSONPath-configurable generic adapter |

**Multi-model routing:** Serve GPT-4, Llama, and Claude from one gateway instance using `WALACOR_MODEL_ROUTING_JSON` with fnmatch patterns. See [Configuration](docs/CONFIGURATION.md).

---

## Features

**Governance & Security**
- Pre-inference policy engine with fail-closed staleness protection
- Three built-in content analyzers: regex PII detection, toxicity deny-list, and Llama Guard 3 (model-based safety) — all run concurrently under enforced timeouts
- Mid-stream S4 (child safety) abort during SSE streaming
- Token budget enforcement (daily/monthly, per-tenant)
- JWT/SSO authentication (HS256/RS256/ES256, JWKS auto-refresh) alongside API key auth
- Audit-only shadow mode for safe rollout (`WALACOR_ENFORCEMENT_MODE=audit_only`)

**Audit & Compliance**
- Dual-write to Walacor backend + local SQLite WAL
- SHA3-512 Merkle session chains with client-side verification
- Compliance export API — EU AI Act, NIST AI RMF, SOC 2, ISO 42001 (JSON/CSV/PDF)
- Per-step pipeline timing in every execution record

**Performance**
- Hybrid Go/Python architecture — Go HTTP proxy with gRPC governance sidecar for maximum throughput
- Parallelized pre-checks — policy, budget, and rate-limit evaluated concurrently via `asyncio.gather`
- LRU content analysis cache (5000 entries) — identical content skips re-analysis
- Batched SSE flushes (50ms intervals) in Go proxy streaming
- Async post-inference evaluation — response sent before governance completes (configurable)
- Singleton pattern analyzers — regex patterns compiled once, reused across requests

**Operational**
- Embedded SQLite control plane — manage attestations, policies, and budgets via API or dashboard
- Model capability auto-discovery (tool-aware models get tool loop; others work normally)
- Built-in web search tool (DuckDuckGo, Brave, SerpAPI) with tool event auditing
- Resilience layer: weighted load balancing, circuit breakers, retry with backoff
- Rate limiting with `X-RateLimit-*` headers (in-memory or Redis-backed)
- Alert bus: webhooks, Slack, PagerDuty for budget threshold crossings
- Horizontal scaling via Redis (session chain + budget state sharing)
- Prompt caching auto-injection for Anthropic; cache hit detection for OpenAI
- OpenTelemetry trace export, Prometheus metrics at `/metrics`

---

## Endpoints

| Path | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI / Ollama chat proxy |
| `/v1/messages` | POST | Anthropic Messages proxy |
| `/v1/completions` | POST | OpenAI text completions proxy |
| `/generate` | POST | HuggingFace proxy |
| `/v1/custom` | POST | Generic adapter proxy |
| `/v1/models` | GET | OpenAI-compatible model list (from attested models) |
| `/health` | GET | JSON health status |
| `/metrics` | GET | Prometheus metrics |
| `/lineage/` | GET | Audit lineage dashboard (SPA) |
| `/v1/lineage/*` | GET | Lineage API (sessions, timeline, execution, verify, trace) |
| `/v1/control/*` | GET/POST/PUT/DELETE | Embedded control plane (attestations, policies, budgets) |
| `/v1/compliance/export` | GET | Compliance report export (JSON, CSV, PDF) |

---

## Dashboard

The gateway serves a built-in React dashboard at `/lineage/` with:

- **Overview** — stat cards, live throughput chart, token usage and latency charts
- **Sessions** — browse sessions, timeline with chain links, execution detail with full audit record
- **Chain Verification** — client-side SHA3-512 recomputation (no server trust required)
- **Pipeline Trace** — canvas waterfall showing time spent in each governance step
- **Control** — manage models, policies, budgets; discover available models from providers
- **Playground** — interactive prompt testing with side-by-side model comparison
- **Compliance** — preview and download compliance reports for four regulatory frameworks
- **Attempts** — completeness invariant tracking with disposition statistics

---

## Development

```bash
pip install -e ".[dev]"
pytest                                        # run tests
cp .env.gateway.example .env.gateway          # fill in credentials
python3 -m uvicorn src.gateway.main:app --reload --port 8000
```

Requirements: Python 3.12+. Optional extras: `[redis]`, `[telemetry]`, `[auth]`.

**Go proxy** (optional, for hybrid mode):
```bash
cd proxy && go build -o proxy . && ./proxy
```
Requires Go 1.21+. Configure via `PROXY_*` env vars (see `proxy/config/config.go`).

---

## Documentation

| Document | Description |
|---|---|
| **[How It Works](docs/HOW-IT-WORKS.md)** | Pipeline walkthrough, tool execution, content analysis, audit trail |
| **[Configuration](docs/CONFIGURATION.md)** | All `WALACOR_*` environment variables |
| **[Quick Start Guide](docs/QUICKSTART.md)** | Step-by-step setup for new users |
| **[EU AI Act Compliance](docs/EU-AI-ACT-COMPLIANCE.md)** | EU AI Act, NIST AI RMF, SOC 2, ISO 42001 mapping |
| **[Adapters](docs/ADAPTERS.md)** | Provider adapter details and custom adapter guide |
| **[Flow & Soundness](docs/FLOW-AND-SOUNDNESS.md)** | Pipeline flowcharts and soundness analysis |
| **[Executive Briefing](docs/WIKI-EXECUTIVE.md)** | What we built and why (non-technical) |
| **[Gateway Reference](docs/GATEWAY-REFERENCE.md)** | Complete API and configuration reference |
| **[Visual Workflow](docs/gateway-workflow.html)** | Interactive HTML architecture diagram |

---

## License

Apache 2.0
