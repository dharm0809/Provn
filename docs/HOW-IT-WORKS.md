# Walacor Gateway — How It Works

**Audience:** Engineers, technical managers, anyone who wants to understand what the gateway does and how it does it.

---

## What It Is

Walacor Gateway is an **AI security and governance proxy**. It sits between your application and any LLM provider (Ollama, OpenAI, Anthropic, HuggingFace, etc.) and does six things:

1. **Authenticates** callers (API key or JWT/SSO)
2. **Enforces policies** (model approval, access rules, token budgets)
3. **Scans content** for safety (PII, toxicity, Llama Guard)
4. **Manages tool execution** for local models (MCP + built-in tools)
5. **Records everything** in a tamper-proof cryptographic audit trail
6. **Provides a dashboard** to see all AI activity in real time

```
Your App  ──>  Walacor Gateway (:8000)  ──>  Ollama / OpenAI / Anthropic
                       │
                       ├── Authenticates the caller
                       ├── Checks if the model is approved
                       ├── Enforces policies and token budgets
                       ├── Manages tool calls (web search, MCP)
                       ├── Scans responses for PII, toxicity, safety
                       ├── Records everything in a tamper-proof chain
                       └── Dashboard at /lineage/
```

Your app doesn't change its code — it just points the base URL from the LLM provider to `http://localhost:8000`. The gateway is invisible to the app.

---

## What It Is NOT

- **Not a model wrapper** — we don't host, modify, or fine-tune any models
- **Not an LLM framework** — we're not LangChain, LlamaIndex, or CrewAI
- **Not a chatbot** — we don't build AI applications
- **Not a load balancer** — routing is a feature, not the purpose
- **Not a model training platform**

The best analogy: **a WAF (Web Application Firewall) for AI.** A WAF sits between users and web apps, inspecting traffic for SQL injection and XSS. The gateway sits between apps and LLMs, inspecting prompts/responses for PII leaks, safety violations, and policy breaches.

| Traditional IT Security | Walacor Gateway |
|---|---|
| WAF inspects HTTP for SQL injection, XSS | Gateway inspects prompts/responses for PII, toxicity, safety |
| WAF logs every request for compliance | Gateway creates cryptographically chained audit records |
| Firewall enforces allow/deny rules | Gateway enforces model attestation + policy rules |
| Rate limiter controls request volume | Gateway enforces token budgets (daily/monthly) |
| SIEM dashboard shows security events | Lineage dashboard shows all AI interactions + chain verification |

---

## The Request Pipeline (8 Steps)

Every request goes through this pipeline:

```
Request arrives at :8000
  │
  ├─ Middleware: CORS → Completeness → Auth
  │
  ▼
Step 1: Authenticate Caller
  │  Validate API key (X-API-Key) or JWT (Bearer token)
  │  Resolve caller identity (user, team, roles, email)
  │  ✗ Invalid → 401 Unauthorized
  │
Step 2: Model Attestation (G1)
  │  Is this model registered and approved?
  │  ✓ Active → proceed
  │  ✗ Revoked → 403 Forbidden
  │  ? Unknown + no control plane → auto-attest on first use
  │
Step 3: Pre-Inference Policy (G3)
  │  Evaluate policy rules (who can use which models)
  │  ✗ Policy deny → 403 Forbidden
  │
Step 4: Token Budget Check
  │  Has this tenant/user exceeded their limit?
  │  ✗ Exhausted → 429 Too Many Requests
  │
Step 5: Forward to LLM Provider
  │  Send request to Ollama/OpenAI/Anthropic
  │  (with tool definitions injected if active strategy)
  │
Step 6: Tool Execution Loop (if model requests tools)
  │  Gateway executes tools, scans output, feeds results back
  │  Repeats until model gives a final answer
  │
Step 7: Post-Inference Content Analysis (G4)
  │  Scan response through PII, toxicity, Llama Guard
  │  ✗ Credit card/SSN/child safety → 403 BLOCK
  │  ⚠ Email/phone/violence → WARN (logged, not blocked)
  │  NOTE: For streaming responses, S4 child-safety patterns
  │  are also monitored mid-stream via compiled regex. If
  │  detected, the SSE stream is immediately terminated —
  │  providing defense-in-depth beyond post-inference analysis.
  │
Step 8: Audit Record + Session Chain (G5)
  │  Build execution record with all metadata
  │  Compute SHA3-512 chain hash (Merkle chain)
  │  Dual-write to Walacor backend + local WAL
  │
  ▼
Response returned to app
```

**Source files:**
- Pipeline orchestrator: `src/gateway/pipeline/orchestrator.py`
- App startup & middleware: `src/gateway/main.py`
- Adapters: `src/gateway/adapters/` (ollama, openai, anthropic, huggingface, generic)

---

## How Tool Execution Works (MCP & Web Search)

This is the most commonly asked question: **how does the gateway manage tool calls?**

### The key insight: the model never touches the outside world

The LLM only sends text that says *"I want to call web_search with query=X."* It has **no network access**, no API keys, no database credentials. The gateway is the only thing that actually executes tools.

### Three phases

#### Phase 1: Startup — Tool Discovery

When the gateway starts, it connects to all configured tool sources and builds a unified catalog:

**Source A: MCP Servers (external tools)**

Configured via `WALACOR_MCP_SERVERS_JSON`:
```json
[
  {"name": "internal-db", "command": "npx", "args": ["mcp-server-postgres", "postgres://..."]},
  {"name": "jira-tool",   "command": "npx", "args": ["mcp-server-jira"]}
]
```
Gateway starts each as a subprocess, connects via the MCP protocol, calls `list_tools()`, and discovers what tools are available.

**Source B: Built-in Tools (e.g. web search)**

Enabled via `WALACOR_WEB_SEARCH_ENABLED=true`. The `WebSearchTool` class implements the same interface as an MCP client (`get_tools()` + `call_tool()`), but runs as plain Python — no subprocess needed.

Both sources register into a single **ToolRegistry**. The orchestrator doesn't know or care if a tool is MCP or built-in — it just calls `registry.execute_tool(name, args)`.

```
MCP Server (subprocess)  ─┐
                           ├──> ToolRegistry ──> execute_tool(name, args)
Built-in WebSearchTool   ─┘
```

**Source files:**
- MCP client: `src/gateway/mcp/client.py`
- Tool registry: `src/gateway/mcp/registry.py`
- Web search: `src/gateway/tools/web_search.py`

#### Phase 2: Request Time — The Tool Loop

When a request comes in and the model is local (Ollama), the gateway manages the full tool lifecycle:

```
Step A: Gateway injects tool definitions into the request body
        Reads all tools from ToolRegistry, converts to OpenAI function-calling format,
        adds "tools": [...] to the request JSON before sending to Ollama.

Step B: Model responds with a tool call (not a final answer)
        Ollama returns: {"finish_reason": "tool_calls",
                         "tool_calls": [{"function": {"name": "web_search",
                                                       "arguments": "{\"query\":\"python\"}"}}]}
        The model CANNOT execute this — it's just text output.

Step C: Gateway intercepts and executes the tool
        - Validates arguments against the tool's schema
        - Calls registry.execute_tool("web_search", {"query": "python"})
        - Registry dispatches to WebSearchTool → ddgs library (full web search results),
          falling back to DDG Instant Answers API if the library is unavailable → results

Step D: Gateway scans tool output for safety
        - Runs PII, toxicity, and Llama Guard on the tool output
        - Catches indirect prompt injection (malicious content in search results)
        - If unsafe: replaces output with "[blocked by content policy]"

Step E: Gateway hashes and records the tool event
        - SHA3-512 hash of tool input and output
        - Records sources (URLs), duration, content analysis verdicts
        - Written as a first-class audit event (ETId 9000003)

Step F: Gateway feeds results back to the model
        - Builds a new request with tool results as a "tool" role message
        - Sends back to Ollama
        - If model requests more tools → loop repeats (B-F)
        - If model gives final answer → return to app
```

**The app never sees any of this.** It sent one request and got one answer. All the multi-turn tool conversation happened inside the gateway.

#### Phase 3: Smart Model Detection

Not all models support tool calling. The gateway detects this automatically:

| Model | 1st Request | 2nd+ Request |
|---|---|---|
| **qwen3:4b** (supports tools) | Gateway injects tools → model accepts → cache `supports_tools=True` | Tools injected, full tool loop available |
| **gemma3:1b** (no tool support) | Gateway injects tools → model returns 400 → strip tools, retry, cache `supports_tools=False` | Tools skipped entirely, no wasted round-trip |

Source: `orchestrator.py:55-68` (`_model_capabilities` registry)

### Two strategies: Active vs Passive

| Strategy | When | Who Executes Tools | Gateway Role |
|---|---|---|---|
| **Active** | Local models (Ollama) | The gateway | Executes tools, scans output, hashes everything, feeds results back |
| **Passive** | Cloud providers (OpenAI, Anthropic) | The provider | Records what happened — extracts tool interactions from the response |

The strategy is auto-detected based on the provider. Set `WALACOR_TOOL_STRATEGY=auto` (default).

### What about private/in-house MCP servers?

Three scenarios:

| Approach | Audit Coverage | How |
|---|---|---|
| **Register MCP servers in the gateway** | Full — hashed, safety-scanned, recorded | Add to `WALACOR_MCP_SERVERS_JSON` |
| **App sends tool messages through gateway** | Partial — passive strategy extracts tool interactions | No config needed |
| **App runs MCP tools outside gateway** | None — gateway can't audit what it never sees | Not recommended |

**The recommended architecture** is to configure all MCP servers in the gateway:

```
┌─────────┐                    ┌──────────────────────────────────┐          ┌──────────┐
│         │                    │        WALACOR GATEWAY           │          │          │
│  App    │─── request ──────> │                                  │────────> │  Ollama  │
│         │                    │  ┌──────────────────────────┐    │          │          │
│  (no    │                    │  │  Private MCP Servers     │    │          │          │
│  MCP    │                    │  │  ├── jira-tool           │    │          │          │
│  client │                    │  │  ├── internal-db         │    │          │          │
│  needed)│                    │  │  └── company-search      │    │          │          │
│         │<── final answer ── │  └──────────────────────────┘    │ <─────── │          │
└─────────┘                    └──────────────────────────────────┘          └──────────┘
```

The app doesn't need its own MCP client. It sends a question; the gateway handles the rest. This is the same principle as a firewall — if traffic doesn't go through the gateway, it can't be audited.

---

## Content Analysis (Safety Scanning)

Three analyzers run concurrently on every response before it's returned to the caller:

### PII Detector (`walacor.pii.v1`)
Regex-based, deterministic, zero external dependencies.

| Pattern | Action |
|---|---|
| Credit card numbers (Visa, MC, Amex) | **BLOCK** |
| US Social Security Numbers | **BLOCK** |
| AWS access key IDs | **BLOCK** |
| API keys / tokens / secrets | **BLOCK** |
| Email addresses | WARN (logged, not blocked) |
| US phone numbers | WARN |
| IPv4 addresses | WARN |

Source: `src/gateway/content/pii_detector.py`

### Toxicity Detector (`walacor.toxicity.v1`)
Keyword matching with configurable deny terms.

| Category | Action |
|---|---|
| Child safety | **BLOCK** |
| Self-harm indicators | WARN |
| Violence instructions | WARN |
| Custom deny terms | WARN |

Source: `src/gateway/content/toxicity_detector.py`

### Llama Guard 3 (`walacor.llama_guard.v3`)
Model-based safety using Meta's Llama Guard 3, running locally via Ollama. Covers 14 safety categories (S1-S14).

| Category | Action |
|---|---|
| S4 — Child safety | **BLOCK** (403 returned to caller) |
| S1 — Violent crimes | WARN |
| S9 — Indiscriminate weapons | WARN |
| S11 — Self-harm | WARN |
| All other unsafe categories | WARN |
| Safe | PASS |

Fail-open: if Ollama is unavailable, returns PASS with confidence=0.0.

Source: `src/gateway/content/llama_guard.py`

---

## Cryptographic Audit Trail

### What gets recorded

Every LLM interaction produces an execution record:

```json
{
  "execution_id": "550e8400-e29b-...",
  "model_id": "qwen3:4b",
  "provider": "ollama",
  "prompt_text": "What is Python?",
  "response_text": "Python is a programming...",
  "thinking_content": "<think>The user asked about...</think>",
  "attestation_id": "self-attested:qwen3:4b",
  "policy_version": 2,
  "policy_result": "pass",
  "content_analysis": [
    {"analyzer": "pii", "verdict": "pass"},
    {"analyzer": "toxicity", "verdict": "pass"},
    {"analyzer": "llama_guard", "verdict": "pass", "category": "safe"}
  ],
  "token_usage": {"prompt_tokens": 186, "completion_tokens": 120, "total_tokens": 306},
  "cache_hit": false,
  "cached_tokens": 0,
  "variant_id": null,
  "latency_ms": 8500,
  "user": "dharmpratap",
  "team": "engineering",
  "sequence_number": 5,
  "previous_record_hash": "a3f8c2d1...",
  "record_hash": "7b1d4e9f..."
}
```

### Session chain (Merkle chain)

Each record links to the previous one via SHA3-512:

```
record_hash = SHA3-512(
    execution_id | policy_version | policy_result |
    previous_record_hash | sequence_number | timestamp
)
```

If anyone tampers with a record, all subsequent hashes break — the chain is verifiable end-to-end.

```
Turn 1 → record_hash_1
Turn 2 → SHA3-512(turn_2_fields + record_hash_1) → record_hash_2
Turn 3 → SHA3-512(turn_3_fields + record_hash_2) → record_hash_3
```

### Dual-write storage

Records always go to two places:
- **Walacor backend** (immutable ledger, if configured) — long-term storage
- **Local SQLite WAL** (always) — powers the lineage dashboard

Source files:
- Record builder: `src/gateway/pipeline/hasher.py`
- Session chain: `src/gateway/pipeline/session_chain.py`
- WAL writer: `src/gateway/wal/writer.py`
- Walacor client: `src/gateway/walacor/client.py`

---

## Embedded Control Plane

The gateway includes a built-in control plane backed by SQLite. No external service required.

### What it manages

| Resource | API | Dashboard Tab |
|---|---|---|
| **Model Attestations** | `POST /v1/control/attestations` | Control > Models |
| **Policies** | `POST /v1/control/policies` | Control > Policies |
| **Token Budgets** | `POST /v1/control/budgets` | Control > Budgets |
| **Model Discovery** | `GET /v1/control/discover` | Control > Models > Discover |

- Mutations immediately refresh in-memory caches
- A local sync loop prevents policy staleness (fail-closed)
- When a remote control plane is configured, it takes precedence

Source: `src/gateway/control/` (store.py, api.py, loader.py, discovery.py)

---

## Lineage Dashboard

A full web dashboard at `/lineage/` for real-time visibility into all AI activity.

### Views

| View | What it shows |
|---|---|
| **Overview** | Live throughput chart, token usage, latency, stat cards |
| **Sessions** | All sessions with model, status, chain status |
| **Timeline** | Execution timeline within a session, chain links, tool badges |
| **Execution Detail** | Full prompt, response, thinking content, tool events, hashes |
| **Chain Verification** | Client-side SHA3-512 recomputation (no server trust needed) |
| **Control** | Manage models, policies, budgets (requires API key) |
| **Attempts** | Completeness invariant — every request tracked |
| **Playground** | Interactive prompt testing with governance readout, compare mode for side-by-side model testing |
| **Compliance** | Export compliance reports (JSON/CSV/PDF) for EU AI Act, NIST AI RMF, SOC 2, ISO 42001 |
| **Pipeline Trace** | Visual waterfall chart showing time in each pipeline step (attestation, policy, budget, LLM forward, content analysis, chain, audit write) with hover descriptions |

Source: `src/gateway/lineage/` (reader.py, api.py, static/)

---

## Authentication

Three modes, configured via `WALACOR_AUTH_MODE`:

| Mode | How it works |
|---|---|
| `api_key` (default) | Validates `X-API-Key` header against `WALACOR_GATEWAY_API_KEYS` |
| `jwt` | Validates Bearer JWT (HS256, RS256, ES256; JWKS endpoint support) |
| `both` | Tries JWT first, falls back to API key |

JWT works with any OIDC provider (Okta, Azure AD, Auth0). Caller identity (user, email, roles, team) is extracted from JWT claims and attached to every audit record.

Source: `src/gateway/auth/` (api_key.py, jwt_auth.py, identity.py)

---

## Project Structure

```
src/gateway/
├── main.py                      # App startup, middleware, routes
├── config.py                    # All WALACOR_ env vars (pydantic-settings)
├── auth/                        # Authentication (API key, JWT, identity)
├── adapters/                    # Provider adapters (Ollama, OpenAI, Anthropic, etc.)
│   └── thinking.py              # Strip <think> blocks from reasoning models
├── pipeline/                    # Core 8-step governance pipeline
│   ├── orchestrator.py          # Main pipeline (1,350+ lines)
│   ├── context.py               # Shared pipeline state
│   ├── forwarder.py             # HTTP forward + SSE streaming
│   ├── hasher.py                # Build execution records
│   ├── session_chain.py         # SHA3-512 Merkle chain
│   └── budget_tracker.py        # Token budgets (in-memory / Redis)
├── content/                     # Content safety analyzers
│   ├── pii_detector.py          # PII detection (regex)
│   ├── toxicity_detector.py     # Toxicity (keyword)
│   └── llama_guard.py           # Llama Guard 3 (model-based)
├── tools/
│   └── web_search.py            # Built-in web search (DDG/Brave/SerpAPI)
├── mcp/
│   ├── client.py                # MCP subprocess client
│   └── registry.py              # Unified tool registry
├── control/                     # Embedded control plane (SQLite CRUD)
├── lineage/                     # Dashboard (reader + API + static SPA)
├── wal/                         # SQLite WAL writer + delivery worker
├── walacor/                     # Walacor backend HTTP client
├── metrics/                     # Prometheus counters/histograms
└── telemetry/                   # OpenTelemetry spans (optional)
```

---

## Compliance Coverage

| Framework | How the Gateway Satisfies It |
|---|---|
| **EU AI Act Art. 12** — Record-keeping | Every interaction recorded with full prompt, response, model, user, timestamp |
| **EU AI Act Art. 14** — Human oversight | Lineage dashboard, content analysis, chain verification |
| **NIST AI RMF** — Govern | Policy rules, model attestation, token budgets, role-based access |
| **SOC 2** — Processing Integrity | SHA3-512 Merkle chain — tamper-evident, independently verifiable |
| **SOC 2** — Confidentiality | PII detection, API key/credential scanning |

Detailed mapping: `docs/EU-AI-ACT-COMPLIANCE.md`

---

## Prompt Caching

The gateway supports automatic prompt caching for Anthropic and OpenAI providers.

**Anthropic**: System messages automatically get `cache_control: {"type": "ephemeral"}` injected into their content blocks. This enables Anthropic's prompt caching — subsequent requests with the same system prompt use cached tokens at reduced cost.

**OpenAI**: The gateway detects OpenAI's automatic prefix caching from `prompt_tokens_details.cached_tokens` in the usage response.

Cache metadata (`cache_hit`, `cached_tokens`, `cache_creation_tokens`) is included in every execution record and visible in the lineage dashboard. Controlled by `WALACOR_PROMPT_CACHING_ENABLED` (default: true).

Source: `src/gateway/adapters/caching.py`

---

## Compliance Export

The gateway can generate compliance reports covering any date range, mapped to regulatory frameworks.

| Format | Endpoint | Description |
|---|---|---|
| JSON | `GET /v1/compliance/export?format=json` | Machine-readable full report |
| CSV | `GET /v1/compliance/export?format=csv` | Spreadsheet-compatible execution log |
| PDF | `GET /v1/compliance/export?format=pdf` | Print-ready report with executive summary |

Parameters: `framework` (eu_ai_act, nist, soc2, iso42001), `start` (YYYY-MM-DD), `end` (YYYY-MM-DD).

Reports include: executive summary, model attestation inventory, execution log, session chain integrity verification, and framework-specific compliance mapping.

Source: `src/gateway/compliance/` (api.py, pdf_report.py)

---

## Quick Reference

| What | Where |
|---|---|
| Gateway endpoint | `http://localhost:8000` |
| Dashboard | `http://localhost:8000/lineage/` |
| Health check | `GET /health` |
| Prometheus metrics | `GET /metrics` |
| All config vars | `.env.example` |
| Visual workflow diagram | `docs/gateway-workflow.html` |
| Executive briefing | `docs/WIKI-EXECUTIVE.md` |
| Full config reference | `README.md` |
| Compliance mapping | `docs/EU-AI-ACT-COMPLIANCE.md` |
| Pipeline flowcharts | `docs/FLOW-AND-SOUNDNESS.md` |
