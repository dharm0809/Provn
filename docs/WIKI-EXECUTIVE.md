# Walacor Gateway — Executive Briefing

**Audience:** CEO, Engineering leadership, Product leadership
**Purpose:** What we built, why we built it this way, what it captures, and how we think about enterprise AI governance.

---

## 1. The Problem — and How We Think About It

Enterprise AI adoption is running years ahead of enterprise AI governance. Every time a developer makes a call to GPT-4, Claude, or a local Llama model, three things are absent:

1. **No proof of what was asked or answered.** Application logs are mutable, rotated, or never collected. There is no way to demonstrate that what was recorded is what actually happened.
2. **No enforcement before or after inference.** Nothing stands between an application and a model. Nothing prevents a model from returning a Social Security Number, an API key, or harmful content.
3. **No way to audit a conversation as a unit.** AI interactions are conversations, not individual API calls. Traditional tooling treats each request atomically. There is no cryptographic guarantee that a series of turns hasn't been edited, reordered, or silently dropped.

Most existing approaches address one of these at a time — and incompletely. Metadata logging tells you a call was made, not what was said. Input filters check the prompt, not the response. Session logging captures turns, but doesn't prove their integrity.

**Our position is that none of these partial solutions are sufficient for regulated industries or high-stakes AI deployments.** Governance requires proof, not trust. The Walacor Gateway is built around that belief end to end.

---

## 2. Our Approach

The gateway is a **security and audit proxy**. Applications point at it instead of the LLM provider. It intercepts every request, enforces policies, and records a cryptographic audit trail — then forwards the call to the actual model. From the application's perspective, nothing changes.

```
Your App  →  Walacor Gateway  →  LLM (OpenAI / Anthropic / Ollama / …)
                   │
                   └── Cryptographic audit record → Walacor backend
```

Three design principles shape everything we built:

**1. Record everything, not metadata about everything.**
We capture the full prompt text and the full response content — not just a timestamp and a model name. The record is sent to Walacor's backend, which hashes it on ingest. This means the backend can prove the content it stored is exactly what the gateway sent. No summarization, no sampling, no truncation.

**2. Fail closed, always.**
If the governance layer cannot verify something, it does not allow it. Attestation cache stale? Block. Policy cache expired? Block. WAL disk full? Block. We believe a governance system that degrades silently into a pass-through is not a governance system. Operators get a `fail_closed` health signal before requests are impacted, giving them time to react.

**3. The audit trail must be provably complete.**
Every request that enters the gateway — allowed, denied, auth-failed, errored, or timed out — produces exactly one attempt record. This is enforced by the outermost middleware layer, which runs before all other processing. It cannot be bypassed. Regulators and auditors can verify completeness without trusting internal logs:

```
Total attempts = Allowed + Denied + Errors    (no gaps permitted)
```

---

## 3. What We Capture

### On every request

| What | How | Why it matters |
|------|-----|---------------|
| Full prompt text | Stored in execution record | Enables retrospective review of what was actually asked |
| Full response content | Stored in execution record | Response is where PII leakage and harmful content appear |
| Provider request ID | Extracted from provider response headers | Ties the gateway record to the provider's own logs |
| Model name and provider | Stored in execution record | The actual model name (e.g. `qwen3:1.7b`) and provider (`ollama`, `openai`) for human-readable display and filtering |
| Model attestation ID | Looked up from control plane | Proves which registered model was requested |
| Model content digest | Fetched from Ollama `/api/show` for local models | For on-device models, proves exactly which weights were used |
| Policy version and outcome | From policy evaluation step | Proves which rules were applied and what they decided |
| Tenant and gateway instance | From configuration | Ties every record to an accountable entity |
| Timestamp | UTC, ISO 8601 | Required for any regulatory chain of custody |

### On every conversation turn (session chain)

When a caller provides a `session_id`, each turn is cryptographically linked to the previous one. Every record contains a fingerprint of the turn before it, creating an unbroken chain across the entire conversation. This means:

- Any deleted turn breaks the chain — the sequence gap is detectable
- Any edited turn breaks the chain — the fingerprint no longer matches
- Any reordered turns break the chain — the link to the prior turn no longer matches
- The first turn in every session always starts at position 0

**Why conversations, not just calls?** Because AI risk doesn't live in individual requests — it lives in conversations. A model might reveal sensitive information only when a conversation reaches a certain point. Compliance reviewers need to reconstruct the full interaction, not spot-check isolated API calls. The session chain makes that reconstruction cryptographically sound.

The chain is verifiable end-to-end without decrypting or reading any content.

### On every failure

Even requests that never reach a model produce a `GatewayAttempt` record: auth failures, parse errors, policy blocks, provider timeouts. The audit trail is not limited to successful inferences.

---

## 4. How We Enforce

Enforcement runs in a defined sequence. Each step is a gate that the request must pass before proceeding.

```
Incoming request
  │
  ├── [Always] Completeness record reserved — GatewayAttempt written on every exit
  │
  ├── 1. Auth check — validate caller API key (if configured)
  ├── 2. Model routing — match model field to provider via fnmatch table
  ├── 3. G1  Model attestation — is this model registered and not revoked?
  ├── 4. G3  Pre-inference policy — does this request pass the active policy?
  ├── 5.     WAL backpressure — is there room to record this request?
  ├── 6.     Token budget — does this tenant have budget remaining?
  ├── 7.     Forward to LLM provider
  ├── 8. G4  Post-inference content gate — does the response pass the analyzers?
  ├── 9. G5  Append to session Merkle chain
  └── 10. G2 Write ExecutionRecord to Walacor or WAL
```

**The order is deliberate.** We check attestation before policy because a request to an unattested model shouldn't even reach the policy evaluation layer. We check policy before forwarding because we never want inference to happen on a request that would have been blocked. We check the response before returning it because that's where PII and harmful content appear.

**The two modes reflect our thinking on rollout, not a compromise on integrity:**

| Mode | Governance | Audit | When to use |
|------|-----------|-------|-------------|
| **Audit-only** (`WALACOR_SKIP_GOVERNANCE=true`) | Off | Full | Pilots, initial rollout — record everything, enforce nothing |
| **Shadow** (`WALACOR_ENFORCEMENT_MODE=audit_only`) | Simulated | Full + `would_have_blocked` | Baseline before going live — see what would have been blocked |
| **Enforced** | Full | Full | Production |

The progression is: observe → baseline → enforce. Each stage produces a complete audit trail.

---

## 5. The Five Guarantees

### G1 — Model Attestation

We don't trust that the model being called is the model the application thinks it's calling. Every request is matched against the attestation registry in the Walacor control plane. Unregistered, revoked, or unrecognized models are blocked before inference occurs.

For local model execution environments (Ollama), the gateway goes further: it fetches the model's SHA256 content digest from the Ollama registry and records it alongside the inference. This means the record contains proof of which exact model weights produced the response — not just the model name.

When a control plane is configured and the attestation cache becomes stale (control plane unreachable), the gateway blocks. We chose this over a fail-open default because a stale attestation list could silently allow a revoked model.

When no control plane is configured, the gateway operates in **auto-attestation mode**: models are automatically registered on first use with a self-attested status. This gives teams full governance capabilities — content analysis, budgets, session chains, audit trails — without needing to deploy a control plane service first. The attestation record clearly marks these models as self-attested, so an auditor can distinguish between centrally verified and locally trusted models. When a control plane becomes available, centrally managed attestations replace auto-attestation automatically.

### G2 — Full-Fidelity Audit

The execution record is the core artifact. It contains everything listed in Section 3 above. It is written to Walacor's backend (or the SQLite WAL in offline mode) after every successful inference.

The gateway sends the full record — including the complete prompt text and response content — to Walacor's backend, which hashes it on ingest. The backend can prove that what it stored is exactly what was received.

For streaming responses, we buffer the full response content alongside the live stream — chunks are forwarded to the caller in real time while being accumulated for the audit record. The caller experiences no added latency from the audit step. The upstream HTTP status code is captured before the first byte is returned, so the caller always gets the actual provider status.

### G3 — Pre-Inference Policy

Requests are evaluated against a versioned policy set pulled from the control plane before any inference occurs. The policy version applied and the outcome (pass, blocked, flagged) are recorded in the execution record.

If the policy cache expires while the control plane is unreachable, the gateway fails closed. We will not enforce an unknown policy on live traffic.

**Shadow mode** (`WALACOR_ENFORCEMENT_MODE=audit_only`) is the recommended way to introduce policy enforcement. Every request is forwarded regardless of policy outcome; violations are recorded as `would_have_blocked=true`. Teams can review the shadow audit trail and tune policies before switching to enforced mode — without impacting production traffic during the transition.

### G4 — Post-Inference Content Gate

**Input filtering is not enough.** Models can be prompted correctly and still return harmful content. The risk is in the output. After the model responds and before the response is returned to the caller, we run every response through pluggable content analyzers.

Three are built in:

| Analyzer | Approach | What it catches |
|----------|----------|----------------|
| `walacor.pii.v1` | Regex, deterministic, severity-tiered | High-risk (credit cards, SSNs, AWS keys, API tokens) → **blocks**; low-risk (emails, phone numbers, IPs) → **warns** |
| `walacor.toxicity.v1` | Keyword deny-list | Self-harm indicators, violence instructions, child safety violations, custom terms |
| `walacor.llama_guard.v3` | Model-based (Meta Llama Guard 3) | Child safety violations → **blocks**; violence, sexual content, criminal planning, and other unsafe categories → **warns** (logged for audit, not blocked) |

The third analyzer — Llama Guard — is a significant step beyond rule-based detection. It uses a purpose-built safety model (Meta's Llama Guard 3, running locally via Ollama) to evaluate response content against a broad taxonomy of unsafe categories. Unlike regex or keyword matching, it understands context and intent. It runs concurrently with the other two analyzers under the same enforced timeout, and like them, stores no content — only a verdict.

All three analyzers run in parallel. No analyzed content is logged or stored by any analyzer — analysis happens in memory and the result is a verdict only. Verdicts are `PASS`, `WARN` (forwarded and flagged in the audit record), or `BLOCK` (403 returned to caller).

The PII detector uses severity tiers because low-risk PII appears routinely in legitimate AI responses — a model explaining DNS will naturally mention an IP address, and a model explaining email protocols will mention an example address. Blocking these would create false positives. High-risk PII (financial data, identity documents, credentials) is always blocked.

For models that use internal reasoning (e.g. qwen3's `<think>` blocks), the gateway ensures content analysis runs on the reasoning content when the visible response is empty. Without this, a thinking model that places its entire answer inside reasoning tokens would bypass all safety classifiers.

Custom analyzers can be added by implementing a single interface without touching the pipeline. This is intentional: we expect content analysis requirements to evolve, and we built for extensibility.

### G5 — Session Chain Integrity

Every conversation turn with a `session_id` is cryptographically linked to the previous turn. The chain construction:

- First turn: `sequence_number = 0`, `previous_record_hash = "000...000"` (genesis)
- Every subsequent turn: `previous_record_hash` = the prior turn's `record_hash`
- Sequence numbers are committed only after a successful audit write — a failed write leaves no mark in the chain

We think of this as the answer to the question: "Can you prove no one edited this conversation?" The answer is yes — any modification, deletion, or reordering of turns is detectable by anyone who can verify SHA3-512 hashes, without accessing the decrypted content.

---

## 6. Audit Storage and Durability

We believe audit records should survive network failures. The gateway uses a two-backend design:

| Backend | When active | Durability |
|---------|-------------|------------|
| **Walacor direct** | Walacor credentials configured | Async HTTP write with JWT auth; Walacor handles long-term storage |
| **SQLite WAL** | Always (when WAL path configured) | fsync-per-write; local queryable via lineage dashboard |

When both backends are configured, the gateway writes to **both** — Walacor for durable long-term storage and the local WAL for the lineage dashboard and as a fallback. If the Walacor write fails, the WAL write still succeeds. No audit record is lost.

The record is committed before the response is returned to the caller (for non-streaming requests) or before the stream ends (for streaming).

The WAL has configurable size limits with three-tier signaling: `healthy` → `degraded` (80% full) → `fail_closed` (100%). Ops teams get a signal before any records are lost.

---

## 7. Multiple Models, One Port, Separate Audit Trails

### The concern

One gateway listens on one port (8000). If GPT-4 traffic, Llama traffic, and Claude traffic all go through the same port, how are their audit trails kept separate? How can you prove that a GPT-4 record wasn't mixed up with an Ollama record?

### How it actually works

Every AI request body contains a `model` field — it's part of the standard API format that every provider uses. The gateway reads that field on every request and uses it to:

1. **Route the request** to the correct provider (OpenAI, Anthropic, Ollama, etc.)
2. **Tag the audit record** with the exact model name, provider, and attestation ID

This means the single port is not a bottleneck for auditability — it is the observation point. Every record that leaves the gateway is stamped with which model produced it, which provider it came from, and which attested model registration it matched against.

```
POST /v1/chat/completions  {"model": "gpt-4", ...}
  → gateway reads model field
  → routes to OpenAI
  → audit record: model=gpt-4, provider=openai, attestation_id=att_001

POST /v1/chat/completions  {"model": "llama3.2", ...}
  → gateway reads model field
  → routes to Ollama
  → audit record: model=llama3.2, provider=ollama, attestation_id=att_002

POST /v1/messages  {"model": "claude-3-5-sonnet", ...}
  → gateway reads model field
  → routes to Anthropic
  → audit record: model=claude-3-5-sonnet, provider=anthropic, attestation_id=att_003
```

All three go through port 8000. All three produce fully differentiated audit records. A compliance reviewer querying the Walacor backend can filter by model, provider, or attestation ID independently.

### Configuring the routing table

```json
[
  {"pattern": "gpt-*",    "provider": "openai",    "url": "https://api.openai.com",    "key": "sk-..."},
  {"pattern": "claude-*", "provider": "anthropic", "url": "https://api.anthropic.com", "key": "sk-ant-..."},
  {"pattern": "llama*",   "provider": "ollama",    "url": "http://localhost:11434",     "key": ""}
]
```

Patterns use standard wildcard matching (e.g., `gpt-*` matches `gpt-4`, `gpt-4o`, `gpt-4-turbo`). The first matching rule wins. Unrecognized models fall through to a path-based default. The routing table is loaded once at startup — there is no per-request parsing cost.

### When you do need separate ports

One port per gateway instance is correct for most deployments. The only reason to run multiple instances on separate ports is **tenant isolation** — one `WALACOR_GATEWAY_TENANT_ID` per instance is a hard boundary. If two business units must have completely separate audit namespaces, they get separate gateway instances. Model isolation does not require separate ports.

### Model capability auto-discovery

Different AI models have different capabilities. Some models (GPT-4, Qwen3) support function calling — the ability to call external tools like web search during a conversation. Others (Gemma3, Phi3) are simpler models that handle only text.

The gateway discovers these capabilities automatically. On the first request to a new model, if tool definitions are sent and the model rejects them, the gateway learns this, strips the tool definitions, and retries transparently — the caller never sees a failure. From that point on, the gateway remembers what each model can and cannot do and adapts its behavior accordingly.

This means teams can add any model to their deployment without configuring its capabilities in advance. The gateway figures it out on its own and adjusts. The discovered capabilities are visible in the health endpoint so operators can see at a glance which models support tools and which do not.

### Concurrent multi-model handling

The gateway handles simultaneous requests to different models correctly. Multiple requests to different models — or even the same model — can arrive at the same time. Session chains remain contiguous, budget counters stay accurate, and no audit records are lost or duplicated. A single session can span multiple models (for example, starting a conversation with one model and continuing it with another), and the chain integrity is maintained across the model switch.

Five provider adapters are fully implemented:

| Provider | Streaming | Notes |
|----------|-----------|-------|
| OpenAI | Yes | Drop-in; captures `chatcmpl-xxx` request ID |
| Anthropic | Yes (SSE) | Drop-in; captures `msg_xxx` request ID |
| Ollama | Yes | Fetches model content digest for local attestation |
| HuggingFace | No | Dual-endpoint adapter |
| Generic | No | JSONPath-configurable for any REST API |

### Using Open WebUI as the interface layer

Teams running local models commonly use **Open WebUI** — a popular open-source chat interface that looks and feels like ChatGPT, but runs entirely on-premise against local models like Ollama. By default it points directly at Ollama, which means every conversation bypasses governance completely.

The fix is one setting change in Open WebUI:

```
Before:  Open WebUI → Ollama              (no governance, no audit)
After:   Open WebUI → Walacor Gateway → Ollama   (fully governed and audited)
```

Open WebUI supports configuring a custom OpenAI-compatible API endpoint. Set that endpoint to the gateway URL (`http://gateway:8000`) and every conversation through the UI — every prompt, every response, every tool call — is now intercepted, enforced, and recorded. Open WebUI does not know or care that a gateway is in between. Ollama does not know or care. Nothing changes for the user.

This makes Open WebUI the recommended UI layer for on-premise deployments: teams get a polished chat interface, and the organization gets a complete audit trail of every interaction through it.

---

## 8. Tool Calls and MCP — Why There Is No Second Gateway

Modern AI models don't just answer questions. They call tools: search the web, query databases, run code, read files. These tool calls happen *after* the model receives the prompt and *before* it gives a final answer. Under a naive proxy design, that entire middle section is invisible to the gateway — and therefore absent from the audit trail.

The gateway includes a built-in web search tool (powered by DuckDuckGo, Brave, or SerpAPI) that models can call directly. This is not a demo feature — it is a production-ready tool that runs through the same governance pipeline as any external MCP tool. Search results are recorded in the audit trail with their own content hashes.

### The option we considered and rejected

Put a second gateway after the LLM specifically to intercept MCP tool calls. This is the obvious answer and the wrong one. Two gateways means two audit trails that need to be stitched together, two infrastructure components to operate, and a structural gap wherever the two systems don't sync. The completeness invariant — every interaction produces exactly one record — breaks the moment you split the audit across two systems.

### What we built instead

The gateway detects which kind of provider it is talking to and applies one of two strategies automatically. There is no second component.

```
Your App → Walacor Gateway ──────────────► LLM Provider
                │                               │
                │          ┌────────────────────┘
                │          │
                │    Cloud model (OpenAI, Anthropic)?
                │          └─► Provider already reports tool calls in its response.
                │               Gateway reads them out and attaches them to the
                │               audit record. No extra infrastructure. No added latency.
                │
                │    Local / private model (Ollama, vLLM, private)?
                │          └─► Gateway runs the tool loop itself:
                │               1. Receives tool call request from LLM
                │               2. Validates tool against policy
                │               3. Executes tool via MCP server
                │               4. Runs content analysis on tool output
                │               5. Sends result back to LLM
                │               6. Repeats until LLM produces a final answer
                │
                └──► One audit record, containing:
                       - the original prompt
                       - every tool that was called and what it returned
                       - the final response
                       - policy outcome, session chain, timestamp
```

### What this means in practice

Every tool call — what was asked of the tool, what the tool returned, how many iterations the model took — is captured in the **same execution record** as the prompt and the final response. Each tool call also produces its own separate audit record containing the tool name, the actual input arguments, content hashes of the input and output, any sources returned (such as web search URLs), and how long the call took.

**Content analysis on tool outputs.** Before a tool's result is fed back to the model, the gateway runs the same content analyzers (PII detection, toxicity filtering, Llama Guard) on the tool output. This catches indirect prompt injection — where a tool returns content that the model would then parrot to the user — before it reaches the model's context. The analysis verdict is recorded in the tool event audit record.

A compliance reviewer looking at a conversation in the lineage dashboard sees the complete picture: not just what the user asked and what the model said, but every tool the model called, what it searched for, what results came back, and whether those results were flagged by content analysis. Tool-augmented requests are visually marked in the timeline so reviewers can immediately identify which conversations involved external data.

For cloud providers (OpenAI, Anthropic), this requires zero additional infrastructure. For local models, the gateway acts as the agentic loop controller, giving it complete visibility and control over every tool execution.

The application changes nothing. One `base_url` change. Full audit of all tool interactions.

---

## 9. Horizontal Scaling

When teams scale to multiple gateway replicas, three things break if state is in-process: session chains diverge, budget counters double-spend, and sequence numbers collide. We solved all three through Redis.

When `WALACOR_REDIS_URL` is set:

- **Session chain** is stored in Redis as a hash (`seq`, `hash` fields per session). The read and write are deliberately separated: `next_chain_values` is a read-only operation — it fetches the current state but does not modify it. `update()` atomically writes both `seq` and `hash` only after the audit record has been successfully committed. This two-phase design means a transient write failure leaves no ghost entry in the chain.

- **Token budgets** use an atomic Lua script for check-and-reserve — no race condition possible, no double-spend across replicas. After each LLM response, the actual token count is reconciled against the estimate with an `INCRBY`/`DECRBY` correction. The counter tracks real consumption, not pre-request guesses.

Without Redis: single-replica, in-process state. All five guarantees still hold. Redis is additive, not required.

```
  Client requests
       │
  ┌────▼──────────┐   ┌─────────────┐
  │  replica 1    │   │             │
  │  replica 2    ├───┤  Redis 7    ├── LLM providers
  │  replica 3    │   │             │
  └───────────────┘   └─────────────┘
       │
       └── All replicas write to Walacor backend
```

---

## 10. Deployment

Three targets are ready. No additional infrastructure is required beyond what is listed.

| Target | Files | Notes |
|--------|-------|-------|
| **Docker** | `deploy/Dockerfile`, `deploy/Dockerfile.fips` | Non-root; healthcheck built in; FIPS-140-2 image available |
| **Docker Compose** | `docker-compose.yml` | Single `docker compose up` starts Gateway + Ollama + OpenWebUI |
| **Kubernetes** | `deploy/helm/` + `deploy/network-policies/` | PVC for WAL, readiness/liveness probes, egress network policy |

Single command: `walacor-gateway` — port 8000.

The Kubernetes egress policy limits outbound connections to the control plane and configured providers only. The gateway cannot be used as a general-purpose HTTP client from within the cluster.

---

## 11. Observability

| Endpoint | Returns |
|----------|---------|
| `GET /health` | Enforcement mode, storage backend, cache staleness, WAL depth, token budget snapshot, active sessions, model capabilities |
| `GET /v1/control/status` | All of `/health` plus auth mode, JWT configuration, content analyzers, configured providers, model routing, session chain state, and lineage status |
| `GET /metrics` | Prometheus: request counters by outcome, attempt counters by disposition, token usage, WAL depth, disk usage, cache age, session count, tool call counts |

Health states: `healthy` → `degraded` → `fail_closed`. The transition from `healthy` to `degraded` is a warning signal, not a failure — ops teams have time to act. `fail_closed` means requests are being rejected and immediate intervention is required.

The lineage dashboard's Overview page consumes the `/metrics` endpoint directly to render live charts — throughput (requests/second), token usage (prompt and completion tokens), and inference latency. Operators can see request rates, allowed vs blocked traffic, token consumption trends, and latency patterns in real time without configuring external monitoring. Historical views (1 hour to 30 days) are served from the WAL database.

---

## 12. Lineage Dashboard — Seeing the Audit Trail

We built a dashboard directly into the gateway at `/lineage/`. It gives anyone — engineers, compliance reviewers, leadership — a visual way to browse and verify the audit trail without writing SQL queries or calling APIs.

### What it shows

| View | What you see |
|------|-------------|
| **Overview** | At-a-glance stat cards (session count, request totals, enforcement mode), a live throughput graph, recent sessions, and recent activity feed |
| **Live Throughput** | A real-time graph on the Overview page showing requests per second flowing through the gateway. Updates every 3 seconds with a rolling 3-minute window. Shows allowed traffic in green and blocked traffic in red, with live counters for request rate, token rate, and compliance percentage. |
| **Session Explorer** | Every session that has passed through the gateway, with record counts, model names, and last activity times |
| **Session Timeline** | The ordered sequence of executions within a conversation — model used, policy outcome, chain hashes, and visual links between records |
| **Execution Detail** | The full audit record for any single request — prompt, response, provider ID, model hash, policy version, tool calls with input data and sources, content analysis on tool output, and reasoning content |
| **Chain Verification** | One-click verification that recomputes every hash in the chain and confirms linkage is intact — runs both on the server and independently in the browser |
| **Token Usage & Latency** | Dual charts showing token consumption (prompt vs completion, stacked area) and inference latency over time. Supports live mode (real-time polling) and historical ranges (1 hour to 30 days). Helps identify cost trends and performance degradation. |
| **Attempts** | Every request that entered the gateway with its disposition — allowed, denied, errored — for completeness auditing |

### Why it matters

The dashboard answers the question regulators and auditors will ask: "Show me the audit trail." It makes the cryptographic session chain visible and verifiable without specialized tooling. The chain verification runs independently in the browser using the same hash algorithm — if a record were tampered with in the database, the browser-side check would catch it.

For tool-augmented requests, the dashboard shows every tool call with full detail: what the model asked the tool to do (the actual input arguments), what sources were returned (clickable links for web search results), content analysis verdicts on the tool output, and timing information. Timeline entries for tool-using conversations are visually marked so reviewers can immediately identify which interactions involved external data access.

The dashboard reads from a separate read-only database connection and cannot modify any audit data.

---

## 13. Reasoning Content Preservation

Modern AI models increasingly use internal reasoning — a "thinking" step where the model works through a problem before answering. This reasoning content is valuable for compliance review and debugging, but most proxy architectures discard it.

The gateway preserves reasoning content in the audit record as a separate `thinking_content` field. The caller receives only the final answer (reasoning tokens are stripped before the response is returned), but the full chain of thought is available to auditors.

For models that natively separate their reasoning (like Qwen3 on Ollama), the gateway reads the structured reasoning field directly. For older models that embed reasoning in special tags within the response, the gateway extracts it automatically. Both paths produce the same auditable result.

---

## 14. Embedded Control Plane — Self-Governing Without External Dependencies

### The problem

In earlier versions, the gateway depended on an external control plane to manage which models are approved, which policies are enforced, and what budget limits apply. Without one, the gateway runs in auto-attestation mode — models are approved on first use and policies pass all requests. More importantly, the health status degrades to `fail_closed` after 15 minutes because the policy cache goes stale with nothing to refresh it.

For teams running a single gateway or a small fleet, deploying and operating a separate control plane service is unnecessary overhead.

### What we built

The gateway now embeds its own control plane. Model attestations, policies, and token budgets are stored locally in a SQLite database and managed through a REST API and the dashboard's Control tab. No external service is needed.

**Key behaviors:**

- **Mutations are immediate.** When an operator approves a model, creates a policy, or sets a budget through the API or the dashboard, the change takes effect on the very next request — the in-memory caches are refreshed synchronously after every write.

- **The staleness problem is solved.** A background sync loop refreshes the policy cache from the local database every 60 seconds. The policy cache never goes stale, so the gateway never enters `fail_closed` due to cache age.

- **The dashboard includes a Control tab.** The lineage dashboard at `/lineage/` now has a fourth tab: Control. It provides a visual interface for managing models (approve, revoke, remove), policies (create, edit, delete, rules builder), budgets (set limits, track usage), and a status overview. The Control tab requires an API key for authentication — the same key used for the gateway API.

- **Model discovery eliminates the catch-22.** The embedded control plane blocks requests to unregistered models — but adding models used to be manual. The Models tab now includes a "Discover Models" button that scans all configured providers (Ollama, OpenAI) for their available model catalogs. The results appear in a table with one-click "Register" buttons, or a "Register All" to approve everything at once. This means an operator can see every model available in their infrastructure and approve them in seconds, without typing model names or knowing them in advance.

- **Fleet support without a coordinator.** For multi-gateway deployments, one gateway is designated the "primary." Other gateways point their `WALACOR_CONTROL_PLANE_URL` to the primary. The existing sync mechanism pulls attestations and policies from the primary every 60 seconds. Changes made on the primary propagate to the fleet automatically. No additional infrastructure is required — the primary serves the sync contract through its existing API endpoints.

### What this means for deployment

The operational readiness progression now looks like this:

| Step | What happens |
|------|-------------|
| **1. Pilot** | Audit-only mode. Observe. |
| **2. Governance** | Full governance with embedded control plane. Auto-attestation for initial models. |
| **3. Control** | Use the Control tab to formally approve models, create enforcement policies, and set budgets — no restart, no external service. |
| **4. Fleet** | Point additional gateways at the primary. Changes propagate within one sync interval. |
| **5. Central control plane** | When available, connect to the Walacor control plane for centrally managed attestations and policies at scale. |

The embedded control plane is not a compromise — it is the correct architecture for single-gateway and small-fleet deployments. It provides the same operational capabilities (model approval, policy enforcement, budget management) that a central control plane would, without the operational cost of running one.

---

## 15. Known Scope Boundaries

These are deliberate design decisions, not gaps:

| Boundary | Rationale |
|----------|-----------|
| **Single tenant per process** | Tenant isolation is a hard boundary. Multi-tenant routing requires multiple instances, load-balanced at the edge. We chose isolation over complexity. |
| **Content analysis is rule-based** | Regex and keyword matching are fast, deterministic, and produce no false positives on exact matches. For organizations that need ML-based content moderation, the custom analyzer interface is the integration point. |
| **No rate limiting** | Rate limiting belongs at the edge (load balancer, ingress). The gateway is the governance layer, not the traffic layer. Mixing them creates coupling that complicates scaling. |
| **Keys are environment variables** | Rotation via Vault or AWS Secrets Manager requires a process restart today. This is a known gap for teams with automated key rotation workflows. |
| **No model hosting** | The gateway controls and audits access to models. It does not run them. Provider infrastructure is the provider's problem. |

---

## 16. What We Are Not Trying to Be

It helps to be clear about what we deliberately chose not to build:

- **Not a firewall.** Firewalls work on connection-level metadata. We work on content.
- **Not a proxy that logs.** Logging is mutable. We produce cryptographic records.
- **Not an ML safety tool.** ML-based content moderation is a deep specialization. We provide the integration point for it via the content analyzer interface.
- **Not a model router for cost optimization.** We route for governance and multi-provider support. Cost optimization is a provider-level concern.
- **Not an application SDK.** Applications require zero code changes. Governance is infrastructure, not a library.

---

## 17. Operational Readiness

The gateway is feature-complete for production deployment. All five guarantees are implemented, tested, and documented. The remaining steps are operational, not engineering:

| Step | What happens |
|------|-------------|
| **1. Pilot** | Route 1–2 internal tools in audit-only mode (`WALACOR_SKIP_GOVERNANCE=true`). Observe the audit trail via the lineage dashboard. |
| **2. Governance** | Switch to full governance (`WALACOR_SKIP_GOVERNANCE=false`). No control plane needed — auto-attestation handles model registration. |
| **3. Control** | Use the embedded Control tab to formally approve models, create enforcement policies, and set budgets — no restart, no external service. |
| **4. Content analysis** | Enable all three analyzers: PII detection, toxicity filtering, and Llama Guard for model-based safety evaluation. |
| **5. Baseline** | Run in `audit_only` enforcement mode. Review `would_have_blocked` records in the lineage dashboard to identify false positives before enforcing. |
| **6. Enforce** | Switch to `enforced`. Policy violations and content blocks now reject requests. |
| **7. Budget** | Set token budget limits per tenant and per user via the Control tab or budget API. Monitor via `/health` endpoint. |
| **8. Multi-model** | Configure model routing table if serving multiple providers from one instance. |
| **9. Fleet** | For multi-gateway, point additional gateways at the primary. Changes propagate within one sync interval. |
| **10. Central control plane** | When available, set `WALACOR_CONTROL_PLANE_URL` for centrally managed attestations and policies at scale. |
| **11. Scale** | Deploy to Kubernetes with Redis; increase replica count. |

---

## 18. Reference Documents

| Document | Audience | Content |
|----------|----------|---------|
| [README.md](../README.md) | Engineers | Full configuration reference, architecture, all guarantees |
| [docs/QUICKSTART.md](QUICKSTART.md) | Engineers | Step-by-step run instructions |
| [docs/CONFIGURATION.md](CONFIGURATION.md) | Engineers | Every environment variable with defaults |
| [docs/FLOW-AND-SOUNDNESS.md](FLOW-AND-SOUNDNESS.md) | Engineering leadership | Detailed flowcharts of every pipeline path; full soundness analysis |
| [OVERVIEW.md](../OVERVIEW.md) | Everyone | One-page product overview |
