# Provn — AI Security Gateway

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) ![License](https://img.shields.io/badge/license-Apache%202.0-green)

**Governance enforcement and cryptographic audit layer for enterprise AI infrastructure.**

A drop-in proxy that sits between your application and any LLM provider. No code changes required. Every inference request passes through an 8-step governance pipeline that enforces five security guarantees before the response reaches the caller.

> **Shadow mode for safe rollout:** Set `WALACOR_ENFORCEMENT_MODE=audit_only` to record and analyze traffic without blocking anything. Use this to validate policies and tune content analyzers before enabling enforcement.

| Guarantee | What it does |
|---|---|
| **G1 — Model Attestation** | Only cryptographically attested models can serve requests. Unknown models are blocked. |
| **G2 — Full-fidelity Audit** | Prompt, response, thinking content, provider request ID, and model hash are persisted to Walacor backend + local SQLite WAL. |
| **G3 — Pre-inference Policy** | Requests are evaluated against active policy rules before forwarding. Stale policies fail closed. |
| **G4 — Content Gate** | Responses pass through pluggable analyzers (PII, toxicity, Llama Guard, DLP, Prompt Guard) before reaching the caller. |
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
|  |  security_headers + body_size + IP rate limit  ||
|  |  +-------------------------------------------+||
|  |  |  api_key_middleware  (auth check)          |||
|  |  |  +---------------------------------------+|||
|  |  |  |  orchestrator (8-step pipeline)        ||||
|  |  |  |                                        ||||
|  |  |  |  1. Attestation lookup           (G1)  ||||
|  |  |  |  2. Pre-inference policy eval    (G3)  ||||
|  |  |  |  3. Budget + rate limit + WAL          ||||
|  |  |  |  4. Forward to provider                ||||
|  |  |  |  5. Tool loop (MCP, web search)        ||||
|  |  |  |  6. Post-inference content gate  (G4)  ||||
|  |  |  |  7. Build execution record + chain(G5) ||||
|  |  |  |  8. Audit write (Walacor + WAL)  (G2)  ||||
|  |  |  +---------------------------------------+|||
|  |  +-------------------------------------------+||
|  +-----------------------------------------------+|
+---------------------------------------------------+
  |
  v
Provider (OpenAI / Anthropic / Ollama / HuggingFace / Generic)
```

Streaming responses use a tee-buffer: chunks flow to the caller in real time while being accumulated for post-stream audit recording.

---

## Quick Start

### Docker Compose (recommended)

```bash
git clone https://github.com/dharm0809/LLM-Gateway.git && cd LLM-Gateway
docker compose up -d
# Wait for Ollama to be ready, then pull a model:
docker exec gateway-ollama-1 ollama pull qwen3:8b
```

Gateway: `http://localhost:8002` | Dashboard: `http://localhost:8002/lineage/` | OpenWebUI: `http://localhost:3000`

### Manual

```bash
pip install -e ".[dev]"

# Full governance with Ollama
export WALACOR_GATEWAY_TENANT_ID=dev-tenant
export WALACOR_GATEWAY_API_KEYS=my-secret-key
export WALACOR_GATEWAY_PROVIDER=ollama
export WALACOR_PROVIDER_OLLAMA_URL=http://localhost:11434
walacor-gateway
```

Point any OpenAI-compatible client at `http://localhost:8000`. All config uses the `WALACOR_` prefix (see `.env.example`).

> **API key required.** If no keys are configured, the gateway auto-generates one at startup (check logs). See [Getting Started](docs/GETTING-STARTED.md) for full setup guide.

---

## Recommended Models

| Model | Best for | Size | Thinking | Tools | Notes |
|-------|---------|------|----------|-------|-------|
| **qwen3:8b** | Primary — reasoning + tool use | 5GB | Yes (stored in audit) | Yes | Both thinking and tools verified end-to-end |
| **qwen3:30b** | Best quality (needs 32GB RAM) | 20GB | Yes | Yes | Most reliable tool calling in qwen3 family |
| **llama3.1:8b** | Fast tool workloads | 4.9GB | No | Yes | Deterministic function calling, no thinking |

Thinking models (qwen3) produce `<think>` blocks which the gateway strips from the response and stores separately as `thinking_content` in the audit record — client gets clean output, auditors get full reasoning chain.

---

## Supported Providers

| Route | Provider | Notes |
|---|---|---|
| `/v1/chat/completions` | **OpenAI** and compatibles | Extracts `chatcmpl-xxx` provider request ID |
| `/v1/chat/completions` | **Ollama** (local) | Fetches model SHA256 digest; thinking strip for reasoning models |
| `/v1/messages` | **Anthropic Claude** | Extracts `msg_xxx` provider request ID |
| `/generate` | **HuggingFace** | Inference Endpoints |
| `/v1/custom` | **Any REST API** | JSONPath-configurable generic adapter |

**Multi-model routing:** Serve multiple models from one gateway using `WALACOR_MODEL_ROUTING_JSON` with fnmatch patterns.

---

## Features

**Governance & Security**
- Pre-inference policy engine with deny/allow rule semantics and fail-closed staleness protection
- Six content analyzers: regex PII, Presidio NER, toxicity, Llama Guard 3, DLP classifier, Prompt Guard 2
- PII severity tiers: high-risk PII (credit cards, SSNs) blocked, low-risk (IPs, emails) warned
- Token budget enforcement (daily/monthly, per-tenant)
- JWT/SSO authentication (HS256/RS256/ES256, JWKS auto-refresh) alongside API key auth
- Caller identity tracking in audit trail (headers, JWT claims, OpenWebUI metadata)
- SSRF protection on outbound tool URLs (blocks private IP ranges)
- MCP command allowlist + subprocess env sanitization
- Request body size limits, per-IP rate limiting, CORS origin restriction
- Security headers (CSP, X-Frame-Options, X-Content-Type-Options) on all responses

**Audit & Compliance**
- Dual-write to Walacor backend + local SQLite WAL (encrypted, 0600 permissions)
- SHA3-512 Merkle session chains with client-side verification
- Thinking content stored separately from response in execution records
- Tool event audit trail with SHA3-512 hashes on input/output data
- Content analysis results stored per-request for compliance replay
- Audit content classifier separates user question from conversation noise
- Compliance export API — EU AI Act, NIST AI RMF, SOC 2 (JSON/CSV)

**Tool Execution**
- Active tool loop: gateway executes tools on behalf of the model (web search, MCP, custom)
- Built-in web search (DuckDuckGo, Brave, SerpAPI) with source attribution
- MCP server support (stdio + HTTP/SSE transport) with command allowlist
- Tool output content analysis (PII, toxicity, indirect prompt injection scanning)
- Tool output size limits and total loop wall-clock timeout
- Adaptive per-model timeouts based on observed P95 latency

**Performance**
- Parallelized pre-checks — policy, budget, and rate-limit evaluated concurrently
- LRU content analysis cache (configurable entries)
- Adaptive timeouts — gateway learns each model's speed and auto-scales timeouts
- Streaming tee-buffer for real-time response delivery + post-stream audit

**Operational**
- Embedded SQLite control plane — manage attestations, policies, and budgets via API or dashboard
- Model capability auto-discovery (tool-aware models get tool loop; others work normally)
- Resilience layer: weighted load balancing, circuit breakers, retry with backoff
- Alert bus: webhooks, PagerDuty for budget threshold crossings
- Horizontal scaling via Redis (session chain + budget state sharing)
- OpenTelemetry trace export, Prometheus metrics at `/metrics`

---

## Endpoints

| Path | Method | Auth? | Description |
|---|---|---|---|
| `/v1/chat/completions` | POST | Yes | OpenAI / Ollama chat proxy |
| `/v1/messages` | POST | Yes | Anthropic Messages proxy |
| `/v1/completions` | POST | Yes | OpenAI text completions proxy |
| `/generate` | POST | Yes | HuggingFace proxy |
| `/v1/models` | GET | No | OpenAI-compatible model list |
| `/health` | GET | No | JSON health status |
| `/metrics` | GET | No | Prometheus metrics |
| `/lineage/` | GET | No | Audit lineage dashboard (SPA) |
| `/v1/lineage/*` | GET | Yes | Lineage API (sessions, timeline, execution, verify, attachments) |
| `/v1/control/*` | CRUD | Yes | Embedded control plane (attestations, policies, budgets) |
| `/v1/compliance/export` | GET | Yes | Compliance report export |

---

## Dashboard

The gateway serves a built-in React dashboard at `/lineage/` with:

- **Overview** — stat cards, live throughput chart, token usage and latency charts
- **Sessions** — browse sessions with user identity, question preview, numbered pagination
- **Chain Verification** — client-side SHA3-512 recomputation (no server trust required)
- **Pipeline Trace** — canvas waterfall showing time spent in each governance step
- **Control** — manage models, policies, budgets; discover available models from providers
- **Playground** — interactive prompt testing with governance readout and model comparison
- **Compliance** — preview and download compliance reports
- **Attempts** — completeness invariant tracking with disposition statistics

---

## Production Test Suite

7-tier gate structure for pre-launch validation:

| Tier | File | What it tests |
|------|------|---------------|
| 1 | `tier1_live.py` | Health, completeness, session chain, lineage, WAL, metrics |
| 2 | `tier2_security.py` | Auth, control plane auth, no stack traces, method enforcement |
| 3 | `tier3_performance.py` | Baseline latency, ramp, sustained load, SLA card |
| 4 | `tier4_resilience.py` | Ollama down, gateway restart, streaming safety |
| 5 | `tier5_compliance.py` | Chain audit (50 sessions), EU AI Act, health, metrics |
| 6 | `tier6_advanced.py` + `tier6_mcp.py` | Web search, tool audit, MCP fetch/time, attachments |
| 7 | `tier7_gauntlet.py` | 89 checks: CRUD, identity, PII, streaming, multi-model, WAL burst |
| 8 | `tier8_security_deep.py` | 44 security checks: auth, CORS, headers, body limits, error sanitization |

```bash
export GATEWAY_API_KEY=your-key
export GATEWAY_MODEL=qwen3:8b
python3.12 tests/production/tier7_gauntlet.py
```

---

## CI/CD

```
git push to main → GitHub Actions → test → build → push to GHCR → deploy
```

Image: `ghcr.io/dharm0809/walacor-gateway:latest`

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/unit/                         # run unit tests
cp .env.example .env                       # fill in credentials
python3 -m uvicorn gateway.main:app --reload --port 8000 --app-dir src
```

Requirements: Python 3.12+. Optional extras: `[redis]`, `[telemetry]`, `[auth]`, `[presidio]`, `[guard]`.

---

## Documentation

| Document | Description |
|---|---|
| **[Getting Started](docs/GETTING-STARTED.md)** | API keys, endpoints, models, testing — for developers and testers |
| **[How It Works](docs/HOW-IT-WORKS.md)** | Pipeline walkthrough, tool execution, content analysis, audit trail |
| **[Configuration](docs/CONFIGURATION.md)** | All `WALACOR_*` environment variables |
| **[EU AI Act Compliance](docs/EU-AI-ACT-COMPLIANCE.md)** | EU AI Act, NIST AI RMF, SOC 2 mapping |
| **[Security Hardening](docs/plans/2026-03-19-security-hardening.md)** | 32-task security plan covering all OWASP LLM Top 10 risks |
| **[Flow & Soundness](docs/FLOW-AND-SOUNDNESS.md)** | Pipeline flowcharts and soundness analysis |
| **[Executive Briefing](docs/WIKI-EXECUTIVE.md)** | What we built and why (non-technical) |

---

## License

Apache 2.0
