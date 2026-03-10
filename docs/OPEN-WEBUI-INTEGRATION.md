# Open WebUI + Walacor Gateway — Integration Analysis & Strategic Plan

> **Date:** 2026-03-09
> **Status:** Planning — Pre-implementation review complete

---

## Table of Contents

1. [What is Open WebUI](#1-what-is-open-webui)
2. [How the Integration Works](#2-how-the-integration-works)
3. [Chat UI Alternatives Compared](#3-chat-ui-alternatives-compared)
4. [Open WebUI Proxy Behavior — Critical Findings](#4-open-webui-proxy-behavior--critical-findings)
5. [Architecture: Gateway in the Middle](#5-architecture-gateway-in-the-middle)
6. [Decision-by-Decision Validation](#6-decision-by-decision-validation)
7. [Gateway's Competitive Moat](#7-gateways-competitive-moat)
8. [Implementation Plan — Phased](#8-implementation-plan--phased)
9. [What NOT to Do](#9-what-not-to-do)
10. [Docker Compose Reference](#10-docker-compose-reference)

---

## 1. What is Open WebUI

Open WebUI is a self-hosted ChatGPT-like interface (126k GitHub stars) that sits in front of LLM backends.

**Tech stack:** Svelte + FastAPI + SQLite/PostgreSQL

**Key capabilities:**
- Multi-model chat (switch models mid-conversation)
- RAG with 9 vector DB backends (ChromaDB, PGVector, Qdrant, Milvus, etc.)
- User management with RBAC, OAuth/OIDC SSO, LDAP
- Image generation (DALL-E, ComfyUI)
- Voice/video (Whisper STT, ElevenLabs TTS)
- Code execution via Jupyter kernels
- Pipelines plugin system (external middleware)
- Horizontal scaling with Redis
- OpenTelemetry observability

**Deployment:** Docker on port 8080 (typically mapped to 3000). Single command:
```bash
docker run -d -p 3000:8080 \
  -v open-webui:/app/backend/data \
  -e OPENAI_API_BASE_URL=http://gateway:8000/v1 \
  -e OPENAI_API_KEY=your-key \
  ghcr.io/open-webui/open-webui:main
```

**Key architectural fact:** Open WebUI is itself an OpenAI-compatible proxy. It exposes `/api/chat/completions` and can unify Ollama + OpenAI backends behind a single API. It also supports multiple backends via semicolon-separated URLs in `OPENAI_API_BASE_URLS`.

---

## 2. How the Integration Works

```
┌─────────────┐     OpenAI-compat      ┌──────────────────┐     Ollama API        ┌──────────┐
│  Chat UI     │ ───────────────────→  │  Walacor Gateway  │ ──────────────────→   │  Ollama   │
│  (any)       │   /v1/chat/completions │  (Governance)     │                       │  Models   │
│  port 3000   │ ←─────────────────── │  port 8000         │ ←────────────────── │  port 11434│
└─────────────┘     Standard response   └──────────────────┘                       └──────────┘
```

**Configuration in Open WebUI:**
```bash
OPENAI_API_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=test-key-alpha
ENABLE_OLLAMA_API=false
ENABLE_FORWARD_USER_INFO_HEADERS=true
```

**Configuration in LibreChat (librechat.yaml):**
```yaml
endpoints:
  custom:
    - name: "Walacor Gateway"
      apiKey: "test-key-alpha"
      baseURL: "http://localhost:8000/v1"
      models:
        fetch: true
```

**Configuration in LobeChat:**
```bash
OPENAI_PROXY_URL=http://localhost:8000/v1
OPENAI_API_KEY=test-key-alpha
```

**Zero code changes needed on either side.** Gateway already speaks OpenAI-compatible `/v1/chat/completions`. Any UI that supports custom OpenAI base URLs works immediately.

---

## 3. Chat UI Alternatives Compared

### Comparison Table

| Feature | Open WebUI | LibreChat | LobeChat | AnythingLLM | Jan | text-gen-webui |
|---------|-----------|-----------|----------|-------------|-----|----------------|
| **GitHub Stars** | 126k | 34.5k | 73.3k | 56k | 40.9k | 46.2k |
| **Active** | Yes | Yes | Yes | Yes | Yes | Yes |
| **License** | MIT | MIT | Apache 2.0 | MIT | Apache 2.0 | AGPL-3.0 |
| **Stack** | Svelte+FastAPI | React+Node.js | Next.js | React+Express | Tauri (desktop) | Gradio (Python) |
| **Custom proxy URL** | Yes | Yes (YAML) | Yes (env) | Yes ("Generic OpenAI") | Limited | N/A |
| **Proxy transparency** | Poor | Good | Good | Good | N/A | N/A |
| **Multi-user auth** | OAuth, LDAP, SCIM | OAuth, LDAP | SSO providers | Permissions (Docker) | None | Basic |
| **RAG** | Yes (9 vector DBs) | Yes (separate service) | Yes | Yes (best-in-class) | No | No |
| **Plugin system** | Pipelines + Functions | MCP + Marketplace | MCP (10k+ tools) | Browser ext + MCP | Extensions | Extensions |
| **Enterprise (SSO/RBAC)** | Yes | Yes | Partial | Partial | No | No |
| **Docker** | Yes | Yes | Yes | Yes | No (desktop) | Yes |

### Proxy Transparency Rating (Critical for Gateway)

| UI | Transparency | Details |
|-----|-------------|---------|
| **LibreChat** | Best | `addParams`/`dropParams`/`headers` config gives full control. No hidden request mutation. |
| **LobeChat** | Good | Thin Next.js backend. `OPENAI_PROXY_URL` is clean passthrough. |
| **AnythingLLM** | Good | "Generic OpenAI" provider is straightforward. |
| **Open WebUI** | Poor | Heavy request modification (see Section 4). |

### Recommendation

**Don't lock into one UI.** Gateway should work with ALL OpenAI-compatible UIs. The deliverable is a properly compatible Gateway, not a UI-specific integration.

**If picking one for demos/docs:** LibreChat offers the best proxy transparency and allows injecting custom headers (like `X-Session-ID`) via YAML config without code changes.

### Eliminated Options

| UI | Reason |
|----|--------|
| **Chatbot UI** | Abandoned — last commit June 2024 |
| **Jan** | Desktop-only, single-user, no server deployment |
| **text-generation-webui** | Local inference tool, not multi-provider chat UI |

---

## 4. Open WebUI Proxy Behavior — Critical Findings

Open WebUI is **NOT a transparent proxy.** It runs a multi-stage middleware pipeline that transforms requests before forwarding.

### Request Modifications

| Modification | What happens | Impact on Gateway |
|---|---|---|
| **System prompt injection** | `apply_system_prompt_to_body()` injects admin-configured system prompts into message list | Gateway audits a prompt the user didn't type. Token counts inflated. |
| **RAG context injection** | `apply_source_context_to_messages()` injects document citations into messages | Prompt text seen by Gateway includes RAG context. Further token inflation. |
| **User metadata injection** | Adds `payload["user"] = {name, id, email, role}` to request body | Non-standard field — may break strict OpenAI schema validation. |
| **Model name rewriting** | Strips `prefix_id` from model names before forwarding | Gateway may see different model ID than user selected. |
| **Reasoning model conversion** | For o1/o3 models: `max_tokens` → `max_completion_tokens`, `system` role → `user`/`developer` | Parameter transformation invisible to Gateway. |
| **Azure payload transformation** | Restructures entire payload against a parameter whitelist | Only affects Azure, but shows willingness to deeply mutate requests. |

### Header Behavior

| Header | Behavior |
|---|---|
| `Authorization` | **Replaced entirely** — Open WebUI sets its own auth header based on internal config. Original client auth is never forwarded. |
| `Content-Type` | Always set to `application/json` |
| `X-OpenWebUI-User-Name/Id/Email/Role` | Injected when `ENABLE_FORWARD_USER_INFO_HEADERS=true` (configurable header names) |
| `X-OpenWebUI-Chat-Id` | Forwarded via `FORWARD_SESSION_INFO_HEADER_CHAT_ID` |
| `HTTP-Referer: https://openwebui.com/` | Injected for OpenRouter endpoints (cannot be disabled) |
| `X-Title: Open WebUI` | Injected for OpenRouter endpoints |

### Streaming Behavior

Open WebUI wraps streaming responses in a `stream_wrapper()` function via `stream_chunks_handler`. SSE chunks are intercepted, potentially transformed, and re-emitted. This means Gateway's SSE output is re-chunked by Open WebUI before reaching the browser.

### Bypass Risk: Direct Connections

Open WebUI has a "Direct Connections" feature where the **browser talks directly to the LLM API, bypassing the Open WebUI backend entirely.** If enabled, this also bypasses any Gateway positioned between Open WebUI's backend and the LLM provider, creating a **complete governance gap.**

### Filter Functions (inlet/outlet)

Open WebUI supports plugin filters:
- `inlet()` — modifies request body before LLM call
- `outlet()` — modifies response after LLM returns
- `outlet()` does NOT run for direct API calls (only `/api/chat/completed`)

### Implications for Gateway

1. Gateway should validate but not reject requests with unknown fields (like `user`)
2. Gateway should log the actual model ID received, not assume it matches what the user saw
3. System prompts injected by Open WebUI will appear in Gateway's audit trail — this is actually useful for compliance (captures the full prompt context)
4. "Direct Connections" must be disabled in any governed deployment
5. Token counts in Gateway will include RAG context — may differ from what Open WebUI shows the user

---

## 5. Architecture: Gateway in the Middle

### SSE Streaming Through Double Proxy

Chaining SSE streaming (`Browser → Open WebUI → Gateway → Ollama`) has documented issues:

**Buffering:** Nginx (if used as reverse proxy) buffers upstream responses by default, completely breaking SSE streaming. Tokens arrive in bursts. Required nginx config:
```nginx
proxy_buffering off;
proxy_cache off;
proxy_http_version 1.1;
proxy_set_header Connection '';
proxy_read_timeout 300s;
chunked_transfer_encoding off;
```

**The "silent period" problem:** If Gateway runs Llama Guard analysis before forwarding the first token, the silence triggers proxy idle timeouts. Cloudflare enforces 100 seconds. Open WebUI issue #16747 documents this exact scenario.

**Connection exhaustion:** Each SSE stream ties up one worker connection for the entire generation duration. Open WebUI issue #13524 documents nginx `worker_connections` exhaustion under moderate concurrent load.

### The Streaming Governance Dilemma

For streaming requests, Gateway must choose:

| Option | Behavior | UX | Governance |
|--------|----------|-----|-----------|
| **A: Full block** | Wait for Llama Guard to complete before forwarding first token | 1-10 second delay before anything appears | Strong — content blocked before user sees it |
| **B: Stream immediately** | Forward tokens as they arrive, analyze response in background | No delay, smooth streaming | Weak — user sees content before analysis completes |
| **C: Pre-only** | Analyze PROMPT pre-inference (fast ~50ms for PII regex). Stream response immediately. Analyze response post-stream and LOG. | No delay | Moderate — prompt governance is strong, response governance is audit-only |

**Recommendation:** Option C is the pragmatic choice. Add a config:
```
WALACOR_STREAMING_GOVERNANCE_MODE=pre_only|full_block|audit_post
```

### SSE Keepalive Requirement

Gateway must send SSE comment frames during any processing pause to prevent timeout disconnections:
```
: heartbeat\n\n
```
Every 10-15 seconds during Llama Guard analysis or other processing. Plus the response header `X-Accel-Buffering: no` to disable nginx buffering.

### WebSocket Handling

**Not an issue for our architecture.** Open WebUI uses WebSocket (Socket.IO) between browser ↔ Open WebUI server. Between Open WebUI server ↔ Gateway, it's pure HTTP/SSE. Gateway doesn't need WebSocket support.

### Latency Overhead

| Scenario | Overhead | Perception |
|----------|----------|-----------|
| Proxy pass-through only | <1ms | Imperceptible |
| + PII regex analysis | ~5-50ms | Imperceptible |
| + Llama Guard analysis | 200-2000ms | Noticeable on first token |
| + Full response buffer | Adds full response time | Very noticeable |

At low traffic (<100 RPS), the proxy overhead itself is negligible regardless of language. The governance analysis is what costs time.

---

## 6. Decision-by-Decision Validation

### Decision 1: GET /v1/models Endpoint — VALIDATED, Priority #1

**Current state:** Gateway does NOT have `/v1/models`. It has `/v1/control/discover` which returns a different format.

**What UIs expect:** `GET {base_url}/models` returning:
```json
{
  "object": "list",
  "data": [
    {"id": "qwen3:4b", "object": "model", "created": 1686935002, "owned_by": "walacor-gateway"}
  ]
}
```

**Open WebUI behavior:**
- Calls `/models` with 1-second TTL cache (nearly every request)
- Reads `model.get("id") or model.get("name")` — tolerant of either
- On failure: logs error, shows no models, but doesn't crash
- Users can manually type model IDs as fallback

**Implementation:**
1. Reuse existing `discovery.py` logic
2. Filter to only attested models (governance value-add — unattested models don't appear)
3. Cache result in-memory (refresh every 60s or on attestation change)
4. Serve from cache (handles 1-second polling without hammering upstream)
5. Return OpenAI-compatible format

**Verdict:** Must-do. Without this, no UI can auto-discover models through Gateway. Highest impact, lowest effort.

---

### Decision 2: Session Header Mapping — REVISED

**Original plan:** Map `X-OpenWebUI-Chat-Id` → `X-Session-ID`.

**Problem:** This only works for Open WebUI. LibreChat, LobeChat, AnythingLLM each have different session concepts or no session header at all.

**Revised approach:** Make Gateway accept configurable session headers:
```
WALACOR_SESSION_HEADER_NAMES=X-Session-ID,X-OpenWebUI-Chat-Id,X-Chat-Id
```

Gateway checks each header in order, uses the first one found. If none present, auto-generates a session ID (already does this).

**Verdict:** Keep the feature but make it generic, not Open WebUI-specific.

---

### Decision 3: Docker Compose — VALIDATED

No concerns. Table stakes for any deployment. Ship multiple compose files:

| File | Stack |
|------|-------|
| `docker-compose.yml` | Gateway + Ollama (minimal) |
| `docker-compose.openwebui.yml` | Gateway + Ollama + Open WebUI |
| `docker-compose.librechat.yml` | Gateway + Ollama + LibreChat |

Reinforces that Gateway is UI-agnostic.

---

### Decision 4: Governance Metadata in Responses — REVISED

**Original plan:** Inject `walacor` object into the response JSON body.

**Problem:** Some strict OpenAI client SDKs may fail on unexpected fields. Ties the approach to a specific response format.

**Revised approach:** Return governance data via response headers:
```
X-Walacor-Execution-Id: uuid
X-Walacor-Policy-Result: allowed
X-Walacor-PII-Detected: false
X-Walacor-Chain-Sequence: 4
X-Walacor-Budget-Remaining: 45000
```

**Why headers are better:**
- Ignored by all clients that don't look for them
- Visible in browser DevTools for debugging
- Won't break any OpenAI-compatible client
- Can be read by UI middleware (Open WebUI Pipelines, LibreChat headers)
- Standard HTTP pattern for metadata

**Verdict:** Use response headers instead of body injection. Lower priority — nice-to-have.

---

### Decision 5: Per-User Token Budgets — VALIDATED, Lower Priority

**Finding:** LiteLLM already does per-key spend tracking. This is expected table stakes, not a differentiator.

**Still valuable** because Gateway's per-user budgets integrate with the Merkle-chained audit trail — you get not just "user X spent Y tokens" but a cryptographically verifiable record of every token spent.

**Verdict:** Keep, but move to Phase 2. Not the moat.

---

### Decision 6: Compliance Export API — ELEVATED, Phase 2 Priority #1

**Finding:** No competing product offers this. Not LiteLLM, not Open WebUI Pipelines, not AnythingLLM.

**This is Gateway's primary differentiator for enterprise sales.** Regulated industries (healthcare, finance, government) need provable, tamper-evident audit trails.

**Endpoint:** `GET /v1/compliance/export?from=2026-01-01&to=2026-03-09`

**Returns:**
```json
{
  "export_id": "uuid",
  "period": {"from": "...", "to": "..."},
  "summary": {
    "total_requests": 14500,
    "unique_users": 23,
    "models_used": ["qwen3:4b", "llama3.1:8b"],
    "pii_incidents": 3,
    "blocked_requests": 47,
    "chain_integrity": "all_valid"
  },
  "merkle_root": "sha3-512-hash",
  "records": [...]
}
```

**Verdict:** Elevated to Phase 2 Priority #1. This is the moat.

---

### Decision 7: Open WebUI Pipeline Plugin — KILLED

**Reasons:**
1. Pipelines run arbitrary Python code — security risk in enterprise environments, often disabled
2. Pipelines are Open WebUI-specific — investment ties us to one UI
3. Creates a triple-proxy: `Browser → Open WebUI → Pipeline → Gateway → LLM` — compounds SSE streaming issues
4. Governance data via response headers (Decision 4 revised) achieves the same goal without a plugin

**If someone wants governance badges in their chat UI**, they build their own Pipeline/plugin that reads Gateway's `X-Walacor-*` response headers. We provide documentation, not the plugin itself.

**Verdict:** Removed from the plan entirely.

---

### Decision 8: Ollama Model Management Features — ADDRESSED

**Problem:** When Open WebUI connects via OpenAI-compatible API, users lose:
- Model pulling/downloading from the UI
- Model deletion
- Model unloading (memory management)
- Modelfile parsing

**Option A — Dual connection (recommended for now):**
```bash
# Open WebUI config
OPENAI_API_BASE_URL=http://gateway:8000/v1    # Chat traffic → governed
OLLAMA_BASE_URL=http://ollama:11434            # Model management → direct
```

Risk: Users can select Ollama models directly for chat, bypassing Gateway. Mitigated by documentation and model access controls.

**Option B — Gateway proxies Ollama management (future):**
Add Ollama management endpoints to Gateway:
- `GET/POST /api/tags` → proxy to Ollama
- `POST /api/pull` → proxy to Ollama
- `DELETE /api/delete` → proxy to Ollama

Keeps Gateway as the single connection point. More engineering effort.

**Verdict:** Option A for now. Option B as a future enhancement if bypass becomes a real concern.

---

### Decision 9: SSE Keepalive Heartbeats — VALIDATED, Phase 1

**Problem:** Gateway's Llama Guard analysis (up to 10 seconds) creates a "silent period" during streaming requests. Intermediate proxies (nginx, Cloudflare) drop connections after 30-100 seconds of silence.

**Solution:** During any processing pause, send SSE comment frames:
```
: heartbeat\n\n
```

Plus response header:
```
X-Accel-Buffering: no
```

**Verdict:** Must-do for production reliability. Phase 1.

---

### Decision 10: Streaming Governance Mode Config — VALIDATED, Phase 1

**New decision** from architecture review. Add:
```
WALACOR_STREAMING_GOVERNANCE_MODE=pre_only
```

| Mode | Prompt analysis | Response analysis | Best for |
|------|----------------|-------------------|----------|
| `pre_only` | Before inference | Post-stream audit log only | Best UX, good for most deployments |
| `full_block` | Before inference | Before first token | Strict compliance (non-streaming feel) |
| `audit_post` | Before inference | Post-stream audit + alert | Balance of UX and visibility |

**Verdict:** Important for production. Phase 1.

---

## 7. Gateway's Competitive Moat

### What Gateway Has That Nobody Else Does

| Feature | LiteLLM | Open WebUI Pipelines | AnythingLLM | **Walacor Gateway** |
|---------|---------|---------------------|-------------|---------------------|
| Merkle-chain audit trail | No | No | No | **Yes** |
| Model attestation (crypto) | No | No | No | **Yes** |
| CEL policy engine | No | No | No | **Yes** |
| Compliance export | No | No | No | **Yes (planned)** |
| PII detection + blocking | Paid/3rd party | DIY | No | **Built-in** |
| Llama Guard integration | SageMaker only | DIY | No | **Built-in** |
| Per-user budgets | Yes (paid) | No | No | **Yes** |
| Tamper-proof session chains | No | No | No | **Yes** |
| Tool-aware auditing (MCP) | No | No | No | **Yes** |
| Thinking content stripping | No | No | No | **Yes** |

### What Gateway Should NOT Compete On

| Area | Why not | Who does it better |
|------|---------|-------------------|
| Chat UI | Massive effort, can't keep up with UI dev pace | Open WebUI, LibreChat, LobeChat |
| RAG pipeline | Complex, well-solved by existing tools | AnythingLLM, Open WebUI |
| Model management UI | Ollama already provides this | Ollama, Open WebUI |
| Plugin marketplace | Network effects — can't build community overnight | LobeChat (10k+ MCP tools) |
| User registration/SSO | Solved by every web framework | Open WebUI, LibreChat |

### The Positioning

> **Any chat UI gives your team an AI interface. Walacor Gateway makes it enterprise-ready.**
>
> - Every conversation is attested, audited, and Merkle-chain verified
> - PII never leaks — detected and blocked before it reaches users
> - Per-user token budgets prevent runaway costs
> - Compliance exports for SOC2, HIPAA, and regulatory audits
> - Works with any model provider (Ollama, OpenAI, Anthropic, HuggingFace)
> - Works with any chat UI (Open WebUI, LibreChat, LobeChat, custom)
> - Deploy in 60 seconds with Docker Compose

---

## 8. Implementation Plan — Phased

### Phase 1: API Compatibility (Week 1)

*Make Gateway work seamlessly with any OpenAI-compatible UI.*

| # | Task | Effort | Impact |
|---|------|--------|--------|
| 1.1 | `GET /v1/models` — return attested models in OpenAI format | Small | Critical |
| 1.2 | Generic session header mapping (`WALACOR_SESSION_HEADER_NAMES`) | Small | High |
| 1.3 | SSE keepalive heartbeats during processing pauses | Small | High |
| 1.4 | Streaming governance mode config (`WALACOR_STREAMING_GOVERNANCE_MODE`) | Medium | High |
| 1.5 | Docker Compose files (Gateway+Ollama, +OpenWebUI, +LibreChat) | Small | High |
| 1.6 | Handle non-standard request fields gracefully (don't reject `user` field) | Small | Medium |

### Phase 2: Enterprise Governance (Week 2-4)

*Build the features that justify Gateway's existence.*

| # | Task | Effort | Impact |
|---|------|--------|--------|
| 2.1 | Compliance export API (`GET /v1/compliance/export`) | Medium | Very High |
| 2.2 | Per-user token budgets (using forwarded identity headers) | Medium | High |
| 2.3 | Governance data in response headers (`X-Walacor-*`) | Small | Medium |
| 2.4 | Cross-session analytics in lineage dashboard | Medium | High |

### Phase 3: Operational Excellence (Week 4-6)

*Polish for production deployments.*

| # | Task | Effort | Impact |
|---|------|--------|--------|
| 3.1 | Cost projection analytics (tokens x provider pricing) | Medium | High |
| 3.2 | Anomaly detection (PII spikes, unusual usage patterns) | Medium | Medium |
| 3.3 | Ollama management proxy (optional, if bypass is a concern) | Medium | Medium |
| 3.4 | Multi-UI integration documentation + quickstart guides | Small | High |

---

## 9. What NOT to Do

| Temptation | Why it's wrong |
|-----------|----------------|
| Rebuild Open WebUI's chat features in Gateway | Massive effort, can't keep up with their dev pace |
| Make Gateway depend on any specific UI | Gateway should work with any OpenAI-compatible client |
| Fork Open WebUI | Maintenance nightmare, lose upstream updates |
| Build a Gateway UI for chatting | Open WebUI / LibreChat already solved this |
| Build an Open WebUI Pipeline plugin | UI-specific, triple-proxy risk, Pipelines often disabled in enterprise |
| Disable Open WebUI's auth and use only Gateway's | Let them coexist — UI handles sessions, Gateway handles API auth |
| Inject governance data into response body | Use response headers — safer, more compatible |
| Compete on RAG, model management, or plugin ecosystem | Others do this better — focus on governance moat |

---

## 10. Docker Compose Reference

### Minimal: Gateway + Ollama

```yaml
# docker-compose.yml
services:
  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama-data:/root/.ollama

  gateway:
    build: .
    ports:
      - "8000:8000"
    environment:
      WALACOR_GATEWAY_PROVIDER: ollama
      WALACOR_PROVIDER_OLLAMA_URL: http://ollama:11434
      WALACOR_GATEWAY_API_KEYS: ${GATEWAY_API_KEY:-default-dev-key}
      WALACOR_GATEWAY_TENANT_ID: ${TENANT_ID:-default-tenant}
      WALACOR_SESSION_CHAIN_ENABLED: "true"
      WALACOR_PII_DETECTION_ENABLED: "true"
      WALACOR_LINEAGE_ENABLED: "true"
    depends_on:
      - ollama

volumes:
  ollama-data:
```

### Full Stack: Gateway + Ollama + Open WebUI

```yaml
# docker-compose.openwebui.yml
services:
  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama-data:/root/.ollama

  gateway:
    build: .
    ports:
      - "8000:8000"
    environment:
      WALACOR_GATEWAY_PROVIDER: ollama
      WALACOR_PROVIDER_OLLAMA_URL: http://ollama:11434
      WALACOR_GATEWAY_API_KEYS: ${GATEWAY_API_KEY:-default-dev-key}
      WALACOR_GATEWAY_TENANT_ID: ${TENANT_ID:-default-tenant}
      WALACOR_SESSION_CHAIN_ENABLED: "true"
      WALACOR_PII_DETECTION_ENABLED: "true"
      WALACOR_TOXICITY_DETECTION_ENABLED: "true"
      WALACOR_LINEAGE_ENABLED: "true"
      WALACOR_METRICS_ENABLED: "true"
      # Accept Open WebUI's session header
      WALACOR_SESSION_HEADER_NAMES: "X-Session-ID,X-OpenWebUI-Chat-Id"
    depends_on:
      - ollama

  webui:
    image: ghcr.io/open-webui/open-webui:main
    ports:
      - "3000:8080"
    volumes:
      - webui-data:/app/backend/data
    environment:
      # Chat traffic goes through Gateway (governed)
      OPENAI_API_BASE_URL: http://gateway:8000/v1
      OPENAI_API_KEY: ${GATEWAY_API_KEY:-default-dev-key}
      ENABLE_OLLAMA_API: "false"
      # Forward user identity to Gateway for audit
      ENABLE_FORWARD_USER_INFO_HEADERS: "true"
      # Disable direct connections to prevent governance bypass
      # (verify this env var name in Open WebUI docs)
    depends_on:
      - gateway
    extra_hosts:
      - host.docker.internal:host-gateway

volumes:
  ollama-data:
  webui-data:
```

### Full Stack: Gateway + Ollama + LibreChat

```yaml
# docker-compose.librechat.yml
services:
  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama-data:/root/.ollama

  gateway:
    build: .
    ports:
      - "8000:8000"
    environment:
      WALACOR_GATEWAY_PROVIDER: ollama
      WALACOR_PROVIDER_OLLAMA_URL: http://ollama:11434
      WALACOR_GATEWAY_API_KEYS: ${GATEWAY_API_KEY:-default-dev-key}
      WALACOR_GATEWAY_TENANT_ID: ${TENANT_ID:-default-tenant}
      WALACOR_SESSION_CHAIN_ENABLED: "true"
      WALACOR_PII_DETECTION_ENABLED: "true"
      WALACOR_LINEAGE_ENABLED: "true"
    depends_on:
      - ollama

  mongodb:
    image: mongo:7
    volumes:
      - mongo-data:/data/db

  librechat:
    image: ghcr.io/danny-avila/librechat:latest
    ports:
      - "3000:3080"
    volumes:
      - ./librechat.yaml:/app/librechat.yaml
    environment:
      MONGO_URI: mongodb://mongodb:27017/librechat
    depends_on:
      - gateway
      - mongodb

volumes:
  ollama-data:
  mongo-data:
```

With `librechat.yaml`:
```yaml
version: 1.2.1
cache: true
endpoints:
  custom:
    - name: "Walacor Gateway"
      apiKey: "${GATEWAY_API_KEY}"
      baseURL: "http://gateway:8000/v1"
      models:
        fetch: true
      headers:
        X-Session-ID: "{{conversationId}}"
```

---

## Appendix: LiteLLM Comparison

LiteLLM is the most direct competitor to Walacor Gateway. Understanding the overlap helps position Gateway correctly.

### What LiteLLM Does Well
- Unified OpenAI-compatible API across 100+ LLM providers
- Virtual API keys with per-key rate limits and spend tracking
- Load balancing and failover across providers
- Guardrails framework with 5 hook points (pre-call, during-call, post-call, etc.)
- Traffic mirroring (shadow testing)
- Caching (exact match and semantic)
- Logging integrations (Langfuse, Datadog)
- Admin dashboard UI

### What LiteLLM Does NOT Do
- No Merkle-chain audit trail
- No model attestation
- No CEL policy engine
- No compliance export
- No tamper-proof session chains
- No built-in PII/toxicity detection (relies on 3rd party integrations)
- Llama Guard only via SageMaker endpoints
- Advanced features (SSO, JWT auth, audit logging) are enterprise/paid-only

### Performance Comparison

| Gateway | Language | P99 Latency (500 RPS) | Throughput |
|---------|----------|----------------------|------------|
| Bifrost | Go | 1.68s | 424 req/s |
| LiteLLM | Python | 90.72s | 44.84 req/s |
| Walacor Gateway | Python | Not benchmarked yet | Not benchmarked yet |

LiteLLM degrades significantly at scale due to Python's GIL. Walacor Gateway (also Python/FastAPI) likely has similar scaling characteristics. This is acceptable for most deployments (<100 RPS) but worth benchmarking.

### Strategic Position

Gateway and LiteLLM solve different problems:
- **LiteLLM:** "I need one API for all my LLM providers" (operational convenience)
- **Walacor Gateway:** "I need provable, auditable, compliant LLM usage" (governance + compliance)

They could theoretically coexist (Gateway → LiteLLM → providers) but the double-proxy adds complexity. Better to position Gateway as the all-in-one: provider routing + governance.
