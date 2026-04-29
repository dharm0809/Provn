# Standalone Gateway Features — Design Document

**Date**: 2026-03-10
**Status**: Historical. Approved at the time. The "SHA3-512 Merkle chains" referenced as part of Phases 1–22 shipped as an ID-pointer chain backed by Walacor-issued `DH`.
**Scope**: Phases 23–29 — 13 features across 7 phases to make Walacor Gateway a complete standalone product

---

## Context

Walacor Gateway is an ASGI audit/governance proxy for LLM providers (Phases 1–22 complete: 207 tests, 8-step pipeline, SHA3-512 Merkle chains, dual-write, session chains, content analysis, embedded control plane, JWT auth, lineage dashboard).

Market analysis of 13 competitors (LiteLLM, Portkey, Helicone, Kong AI Gateway, Langfuse, LangSmith, TrueFoundry, NeMo Guardrails, etc.) identified 16 candidate features. After deep feasibility research across 4 parallel research tracks, 13 features are approved for implementation and 3 are skipped.

## Decision: Compliance-First Build Order

### Why "Compliance Moat First" over alternatives

**Rejected: "Production Readiness First"** (build retry/LB/rate-limiting before compliance)
- Gateway already handles production traffic (8-step pipeline, dual-write, session chains)
- Competing on infrastructure features puts us against LiteLLM (5+ years head start, 200+ contributors)
- Delays our strongest differentiator while building features where we have no competitive advantage

**Rejected: "Parallel Tracks"** (compliance + infrastructure simultaneously)
- Requires sustained context-switching between fundamentally different code areas
- Higher risk of half-finished features on both tracks
- Single-developer velocity is better with sequential focus

**Selected: "Compliance Moat First"**
- Compliance Export is time-sensitive (EU AI Act Article 12 enforcement August 2026)
- Zero competitors offer audit-ready compliance reports (Helicone/Langfuse/LangSmith only do CSV dumps)
- Quick wins (Phase 23, 2-3 days) immediately unblock OpenAI-compatible UI compatibility
- Infrastructure features (Phases 25-26) follow naturally once the "why" of the audit architecture is proven via compliance export

---

## Features: Implement (13) vs Skip (3)

### IMPLEMENT

| # | Feature | Phase | Effort |
|---|---------|-------|--------|
| 1 | GET /v1/models endpoint | 23 | 1 day |
| 2 | Governance response headers | 23 | 0.5 day |
| 3 | SSE keepalives | 23 | 0.5 day |
| 4 | Compliance Export API (JSON/CSV/PDF) | 24 | 2-3 weeks |
| 5 | Fallback / Retry / Circuit Breakers | 25 | 5-6 days |
| 6 | Model Groups (weighted LB) | 25 | 3-4 days |
| 7 | Rate Limiting (RPM + headers) | 26 | 3-4 days |
| 8 | Alerting & Webhooks | 26 | 2-3 days |
| 9 | Governance Waterfall Trace View | 27 | 5-7 days |
| 10 | Provider-Level Prompt Caching | 28 | 3-4 days |
| 11 | Streaming Governance (S4 mid-stream) | 28 | 2-3 days |
| 12 | Prompt Playground (minimal) | 29 | 4-5 days |
| 13 | Weighted Routing / A/B | 29 | 3-5 days |

### SKIP

| Feature | Reason |
|---------|--------|
| Virtual keys / per-key permissions | Not aligned with proxy-first architecture (user decision) |
| SDK / Client libraries | Audit-first proxy — transparent, no client changes needed (user decision) |
| Semantic routing | Overkill for 2-5 models, hurts auditability, fnmatch routing sufficient. Revisit at 20+ models. |

---

## Per-Feature Design: Approach Chosen vs Alternatives Rejected

### 1. GET /v1/models (Phase 23)

**Chosen**: Pull from ControlPlaneStore registered models + discovery endpoint. Return OpenAI-format `{data: [{id, object:"model", owned_by}]}`.

**Why not** build a separate model registry:
- Control plane already tracks attestations with model_id, provider, status
- Discovery endpoint already scans Ollama + OpenAI providers
- Adding a standalone registry would duplicate data and create sync problems

### 2. Governance Response Headers (Phase 23)

**Chosen**: Non-streaming: HTTP headers (`X-Walacor-*`). Streaming: `event: governance` SSE event after `data: [DONE]`.

**Why not** HTTP/2 trailer headers:
- Most SSE clients and reverse proxies strip trailers
- SSE comment-based embedding (`:`-prefixed lines) is non-standard and easily lost

**Why not** embed in the response body:
- Would modify the provider's response, breaking the transparent proxy contract

### 3. SSE Keepalives (Phase 23)

**Chosen**: `asyncio.create_task` in `stream_with_tee` sending `: keepalive\n\n` every 15 seconds, cancelled on stream completion.

**Why not** rely on TCP keepalives:
- TCP keepalives operate at the socket level and don't prevent HTTP-level idle timeouts in reverse proxies (Nginx default: 60s, ALB: 60s)
- SSE comments are the standard pattern used by all major SSE implementations

### 4. Compliance Export API (Phase 24)

**Chosen**: `GET /v1/compliance/export` with JSON + CSV + PDF formats, mapped to EU AI Act / NIST AI RMF / SOC 2 / ISO 42001. PDF via WeasyPrint. JSON aligned with VeritasChain Protocol v1.1.

**Why not** only JSON/CSV (skip PDF):
- Auditors require printable, formatted reports. PDF is non-negotiable for external audits.
- SOC 2 and ISO 42001 auditors expect professional documents, not raw data dumps.

**Why not** ReportLab for PDF:
- ReportLab requires imperative layout code (coordinates, boxes, fonts). WeasyPrint renders HTML/CSS templates — dramatically simpler to maintain and iterate on report design.

**Why not** integrate with Credo AI or other compliance platforms instead of building:
- Those platforms help document governance. We generate evidence from actual traffic. Fundamentally different value: evidence-based compliance vs documentation-based compliance.
- Our data (SHA3-512 chains, policy evaluations, content analysis) is richer than what any external platform can capture.

**Why** VCP v1.1 format alignment:
- VeritasChain Protocol is the only emerging IETF standard for cryptographic audit trails
- Pursuing standardization (draft-kamimura-scitt-vcp)
- Our SHA3-512 session chain maps directly to VCP's inner evidence layer
- First-mover advantage before standard is finalized

### 5. Fallback / Retry / Circuit Breakers (Phase 25)

**Chosen**: `tenacity` for retry (exponential backoff, max 2-3 attempts), `pybreaker` for per-model circuit breakers (closed→open→half-open), error-specific fallback routing.

**Why not** `hyx` (all-in-one resilience toolkit):
- `hyx` is newer with smaller community. `tenacity` (30K+ GitHub stars) and `pybreaker` (1K+ stars) are battle-tested.
- Separate libraries = can adopt incrementally and replace independently.

**Why not** retry mid-stream:
- Once SSE bytes are sent, they cannot be recalled. Retrying mid-stream would produce duplicate/corrupted output.
- Industry consensus: send `event: error` SSE event and let client handle reconnection.

**Why not** LiteLLM's two-stage recovery (retry within group → fallback to other groups):
- Adds complexity with diminishing returns. Simple: retry same endpoint 2-3x → if all fail, try next endpoint in model group → if group exhausted, return error.
- Our circuit breaker automatically prevents retrying known-broken endpoints.

**Audit trail design for retries**:
- One attempt record per inbound request (completeness invariant preserved)
- One execution record per provider call (each attempt is audited)
- Only final successful execution advances session chain
- Failed attempts recorded with `retry_of: original_execution_id`

### 6. Model Groups / Load Balancing (Phase 25)

**Chosen**: Weighted random selection across multiple endpoints per model pattern + health-aware removal with configurable cooldown.

**Why not** latency-based routing:
- Requires Redis for cross-instance latency metrics storage
- LLM inference latency is dominated by model computation, not network — routing to "fastest" endpoint provides marginal benefit
- LiteLLM explicitly warns latency-based routing is not recommended for production due to Redis overhead

**Why not** least-connections:
- For external APIs, we cannot observe provider-side queue depth
- "Least connections from our gateway" is a meaningless proxy for actual provider load
- Weighted random with health checks achieves the same practical result with far less complexity

**Why not** sticky sessions:
- LLM API calls are stateless (conversation history is in the request payload)
- Sticky sessions only matter for self-hosted inference with KV cache reuse — not our primary use case

### 7. Rate Limiting (Phase 26)

**Chosen**: Sliding window counter for RPM alongside existing BudgetTracker TPM. Per-user, per-model granularity. In-memory + Redis variants. Rate limit response headers.

**Why not** token bucket:
- Token bucket allows bursts up to bucket capacity. For LLM workloads where one burst request can consume $10+ of compute, controlled bursting is undesirable.
- Sliding window counter provides smooth, predictable rate enforcement.

**Why not** prompt token estimation (tiktoken pre-counting):
- Adds 5-20ms latency per request
- Model-specific (different tokenizers per provider)
- The reservation pattern in BudgetTracker already handles async token counting: reserve estimated amount pre-request, reconcile with actual usage post-response.

**Why not** build rate limiting into BudgetTracker:
- Different concerns: budgets are cumulative (total spend over period), rate limits are instantaneous (throughput per minute)
- Different data structures: budget = counter with ceiling, rate limit = sliding window
- Separate modules = clearer code, independent testing

### 8. Alerting & Webhooks (Phase 26)

**Chosen**: Thin async event bus → webhook dispatcher. Slack + generic webhook + PagerDuty Events API v2. Prometheus gauges for AlertManager integration.

**Why not** build a full alerting system (dedup, escalation, snooze, grouping):
- That is what PagerDuty, OpsGenie, and Prometheus AlertManager already do
- Building our own would take months and never match mature alerting platforms
- Our gateway already exposes Prometheus metrics — teams use existing AlertManager rules

**Why not** email notifications:
- SMTP integration is a maintenance burden and security surface (credentials, TLS, bounce handling)
- Slack webhooks and PagerDuty cover 95% of enterprise alerting needs
- Teams that need email use PagerDuty→email routing

**Why not** use an external event bus (Redis Streams, RabbitMQ):
- Over-engineering for alert volumes (budget thresholds fire at most 3 times per budget period)
- In-process async queue is sufficient. If the gateway restarts, missed alerts are acceptable — the underlying data is in the WAL.

### 9. Governance Waterfall Trace View (Phase 27)

**Chosen**: Build natively in React dashboard using canvas rendering. Show each pipeline step as horizontal bar with governance annotations.

**Why not** integrate with Jaeger/Zipkin:
- Jaeger's UI doesn't understand LLM concepts (token counts, prompt/response, tool calls, session chains)
- Our WAL data is richer than OTel spans (policy verdicts, SHA3-512 hashes, content analysis, thinking content)
- Building custom Jaeger UI plugins is more effort than a native canvas component

**Why not** use `flame-chart-js` library:
- Adds a dependency for a relatively simple visualization
- Our dashboard already has canvas-based charts (ThroughputChart) — consistent pattern
- Flame charts are designed for profiling (thousands of stack frames), our waterfall has ~10 steps — overkill

**Why not** use Recharts (already in dependencies):
- Recharts is great for time-series charts but awkward for waterfall/gantt-style visualizations
- A horizontal bar chart approximation would look clunky compared to a purpose-built canvas waterfall

### 10. Provider-Level Prompt Caching (Phase 28)

**Chosen**: Auto-inject Anthropic `cache_control` breakpoints for system prompts. Detect OpenAI `cached_tokens` in usage response. Log cache metrics in execution records.

**Why not** semantic caching (GPTCache, LangChain SemanticCache):
- **Incompatible with audit-first governance.** Each request must produce a unique, hashable execution record. Returning a cached response for a "semantically similar" query violates the audit trail contract.
- False positive rates up to 99% in domain-specific contexts (banking, medical) — NDSS 2026 documented cache poisoning attacks on semantic caches
- Real production hit rates are 15-25%, not the 60-80% marketing claims
- Introduces a mutable, attackable layer between the caller and the audit trail

**Why not** exact-match response caching:
- Acceptable IF cache hits are recorded as distinct audit record type (`cache_hit: true`)
- However, LLM responses are non-deterministic — same prompt rarely produces identical responses
- The value is low compared to provider-level caching which operates at the inference layer

**Why** auto-injection (not require users to add `cache_control` manually):
- Transparent cost savings — users don't change their code
- System prompts are the highest-value cache targets (repeated across every request in a session)
- Anthropic's cache_control is per-block, so we can add it without modifying the user's actual prompt content

### 11. Streaming Governance (Phase 28)

**Chosen**: Post-stream evaluation (current approach, keep) + fast keyword regex for S4 (child safety) mid-stream abort only.

**Why not** full mid-stream ML-based content moderation:
- Running Llama Guard on each 128-token chunk adds 50-100ms per chunk. On a 20-chunk response, that is 1-2 seconds cumulative latency.
- Small chunks lack context — "kill the process" is benign in coding but flagged in isolation.
- Once SSE bytes are sent, they cannot be recalled. Mid-stream detection can only truncate, not redact.
- This is an application-layer concern, not a proxy concern.

**Why** S4 keyword check specifically:
- Child safety (S4) is the only category where false positives are acceptable and immediate action is required
- Keyword/regex is sub-millisecond — no latency impact
- Other categories (PII, toxicity) are better handled post-stream where full context is available

### 12. Prompt Playground (Phase 29)

**Chosen**: Thin "Try It" tab in dashboard that routes through the gateway. Side-by-side model comparison. Every test generates real audit records.

**Why not** build a full playground (prompt management, versioning, collaboration, templating):
- Portkey, Langfuse, Helicone have dedicated teams building playgrounds. We cannot out-feature them as a side project.
- Our target users are platform engineers and compliance officers, not prompt engineers.
- ROI on minimal playground (demo vehicle, 4-5 days) is very high. ROI on full playground (2-4 months) is negative.

**Why not** embed an existing open-source playground (Agenta, Langfuse):
- None are designed as embeddable widgets — they are full applications
- Embedding creates dependency management burden
- A playground that routes through our own gateway (generating real audit records) is both simpler and more valuable

**Why** side-by-side comparison:
- Natural tie-in to audit trail: each comparison generates N execution records
- Demonstrates governance pipeline working in real-time
- The "aha moment" for demos: send prompt to 2 models, see responses AND full governance metadata side by side

### 13. Weighted Routing / A/B (Phase 29)

**Chosen**: Add weight support to model routing table. Execution records already capture model_id. Comparison via SQL at query time.

**Why not** full statistical A/B testing with significance calculations:
- LLM output quality is too high-variance for traditional hypothesis testing
- Measurement problem is unsolved — you need human evaluation or LLM-as-judge, both expensive and biased
- "Traffic splits aren't true A/B testing" — without sticky user assignment, you conflate user behavior with model behavior

**Why not** skip entirely:
- Weighted routing (canary deployment) IS genuinely useful: send 5% to new model before full switch
- Implementation is trivial given existing model routing infrastructure (~50 lines in resolver)
- Nearly free to add alongside model groups (Phase 25)

---

## Technology Choices

| Need | Choice | Why |
|------|--------|-----|
| PDF generation | **WeasyPrint** | Python-native HTML/CSS→PDF. Modern CSS support (flexbox, grid). Dramatically simpler than ReportLab. |
| Retry logic | **tenacity** | 30K+ GitHub stars. Battle-tested. Async support. Composable with circuit breakers. |
| Circuit breakers | **pybreaker** | Standard Python circuit breaker. 3-state model. Configurable thresholds and recovery. |
| Compliance format | **VCP v1.1 aligned JSON** | Emerging IETF standard for cryptographic audit trails. First-mover advantage. |
| Gov't compliance | **OSCAL** (future) | NIST machine-readable format. FedRAMP requirement. |
| Trace visualization | **Canvas (native)** | Consistent with existing ThroughputChart. No new dependencies. Full control over governance annotations. |
| Alerting integration | **Prometheus AlertManager** | Gateway already exposes /metrics. Zero alerting logic in codebase. Teams use existing rules. |

---

## Timeline

| Phase | Feature | Days | Weeks |
|-------|---------|------|-------|
| 23 | Quick Wins | 2-3 | 1 |
| 24 | Compliance Export | 10-15 | 2-4 |
| 25 | Resilience | 8-10 | 5-6 |
| 26 | Rate Limiting + Alerting | 5-7 | 7 |
| 27 | Trace Waterfall | 5-7 | 8 |
| 28 | Caching + Streaming Gov | 5-7 | 9 |
| 29 | Playground + A/B | 7-10 | 10-11 |

**Total: ~11 weeks to feature-complete standalone product**
