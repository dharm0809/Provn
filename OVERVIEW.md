# TruzenAI — Overview

## What it is

A security and audit proxy that sits between your application and any AI model. Your app talks to the gateway exactly like it talks to OpenAI. The gateway handles attestation, policy enforcement, content analysis, and audit logging — then forwards the request to the actual model.

```
Your App  →  TruzenAI Gateway  →  LLM (OpenAI / Anthropic / Ollama / any provider)
```

No code changes required in your application.

---

## What it records

Every request produces one audit record containing:

- The **prompt** and **response** (full text, sent to Walacor backend which hashes on ingest)
- **Thinking content** — reasoning chain from thinking models (qwen3), stored separately from the clean response
- The **provider's own request ID** — the ID the model assigned to that specific exchange
- The **model hash** — a cryptographic fingerprint of the model weights (available for local models like Ollama)
- **Tool events** — every tool call (web search, MCP, fetch) with full input/output content (Walacor hashes on ingest)
- **Content analysis** — PII detection, toxicity scoring, Llama Guard verdicts per request
- **Caller identity** — who made the request (JWT claims, headers, or client IP)
- Policy result, timestamp, tenant, session chain values, latency, token counts

Records are dual-written to a local SQLite WAL (crash-safe, encrypted, 0600 permissions) and the Walacor backend.

---

## What it enforces

| Check | What happens on failure |
|---|---|
| **Model attestation** | Request blocked — model must be registered or auto-attested |
| **Pre-request policy** | Request blocked — policy rules evaluated (deny/allow semantics) |
| **Response content** | Response blocked — PII, toxicity, Llama Guard, DLP, Prompt Guard checks |
| **Token budget** | Request blocked — per-tenant spend limit enforced |
| **Rate limiting** | Request throttled — per-key and per-IP limits (default 120 RPM) |
| **WAL backpressure** | Request blocked — protects against unbounded disk growth during outages |
| **Request body size** | Request rejected (413) — default 50MB limit |

Set `WALACOR_ENFORCEMENT_MODE=audit_only` to log violations without blocking (useful for baseline measurement before going live).

---

## Recommended models

| Model | Best for | Thinking | Tools |
|-------|---------|----------|-------|
| **qwen3:8b** | Primary — reasoning + tool use | Yes | Yes |
| **qwen3:30b** | Best quality (needs 32GB RAM) | Yes | Yes |
| **llama3.1:8b** | Fast, reliable tool workloads | No | Yes |

---

## Quick start

```bash
# Docker Compose (includes Ollama + OpenWebUI)
git clone https://github.com/dharm0809/LLM-Gateway.git && cd LLM-Gateway
docker compose up -d
docker exec gateway-ollama-1 ollama pull qwen3:8b
```

Gateway: `http://localhost:8002` | Dashboard: `http://localhost:8002/lineage/`

> **API key required.** Check logs for auto-generated key: `docker compose logs gateway | grep "Auto-generated key"`

See [Getting Started](docs/GETTING-STARTED.md) for the full setup guide including API key configuration, user identity headers, and test suite commands.

---

## Key endpoints

| Path | Auth? | Description |
|---|---|---|
| `POST /v1/chat/completions` | Yes | OpenAI / Ollama chat proxy |
| `GET /health` | No | Status, cache freshness, WAL backlog |
| `GET /metrics` | No | Prometheus metrics |
| `GET /v1/models` | No | Available models |
| `GET /lineage/` | No | Audit lineage dashboard |
| `GET /v1/lineage/sessions` | Yes | Session list with question preview |
| `GET /v1/lineage/verify/{id}` | Yes | Verify session chain integrity |
| `GET /v1/control/status` | Yes | Gateway governance status |
| `GET /v1/control/discover` | Yes | Scan providers for available models |
| `GET /v1/readiness` | No | 31-check rollup (security, integrity, persistence, hygiene) |
| `GET /v1/connections` | Yes | 10-tile subsystem health cockpit |
| `POST /v1/openwebui/events` | Yes | OpenWebUI plugin event governance |
| `GET /api/tags` · `/ps` · `/version` · `/show` | No | Ollama-shape proxy for OpenWebUI native registration |

---

## How the audit trail works

```
Request comes in
    │
    ├─ Blocked (attestation / policy / budget / rate limit)?
    │       → one row in gateway_attempts (disposition = denied_*)
    │
    └─ Allowed?
           │
           ├─ Forward to model, get response
           ├─ Execute tools if model requests them (web search, MCP)
           ├─ Strip thinking content, store separately
           ├─ Run content checks (PII, toxicity, Llama Guard, DLP)
           ├─ Link to session chain (UUIDv7 ID-pointer: record_id + previous_record_id)
           ├─ Write to local WAL (SQLite, fsync, 0600 permissions)
           └─ Deliver to Walacor backend (async, with retry)
                    ↓
           one row in gateway_attempts (disposition = allowed)
           one row in wal_records (full execution record)
           N rows in tool_events (if tools were called)
```

Every request — whether allowed, blocked, or errored — always produces exactly one row in `gateway_attempts`. This is the completeness invariant.

---

## Session chains (G5)

Pass `X-Session-Id` header in your request. The gateway links turns within a session via a UUIDv7 ID-pointer chain:

```
turn 1  →  record_id_1 (UUIDv7),  previous_record_id = null
turn 2  →  record_id_2,            previous_record_id = record_id_1
turn 3  →  record_id_3,            previous_record_id = record_id_2
```

Each record is Ed25519-signed over its canonical ID string, and Walacor hashes the full record on ingest (returning `DH` as a tamper-evident checkpoint). The lineage dashboard verifies chains server-side via `/v1/lineage/verify/{id}` — any deleted, reordered, or modified turn breaks the pointer walk.

---

## Security

The gateway ships with 30+ security hardening measures:

- API key required for all data endpoints (auto-generated if not configured)
- CSP + security headers on all responses
- CORS restricted to configured origins (default: same-origin only)
- SSRF protection on outbound tool URLs (blocks private IP ranges)
- MCP subprocess command allowlist + env sanitization
- Request body size limits (default 50MB)
- Per-IP pre-auth rate limiting + per-key rate limiting
- Constant-time API key comparison
- Generic error responses (no stack traces or internal paths leaked)
- WAL file permissions 0600 + PRAGMA secure_delete
- Indirect prompt injection scanning on tool output

See [Security Hardening Plan](docs/plans/2026-03-19-security-hardening.md) for the full 32-task audit.

---

## Further reading

- [Getting Started](docs/GETTING-STARTED.md) — API keys, models, testing, team onboarding
- [How It Works](docs/HOW-IT-WORKS.md) — complete walkthrough of the pipeline, tool execution, MCP, and audit trail
- [EU AI Act Compliance](docs/EU-AI-ACT-COMPLIANCE.md) — regulatory mapping
- [Executive Briefing](docs/WIKI-EXECUTIVE.md) — CEO/leadership narrative
