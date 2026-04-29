# TruzenAI — Executive Briefing

**Audience:** CEO, Engineering leadership, Product leadership, Compliance leadership
**Purpose:** What we built, why we built it this way, what it captures, and how we think about enterprise AI governance.

---

## 1. The Problem — and How We Think About It

Enterprise AI adoption is running years ahead of enterprise AI governance. Every time a developer makes a call to GPT-4, Claude, or a local Llama model, three things are absent:

1. **No proof of what was asked or answered.** Application logs are mutable, rotated, or never collected. There is no way to demonstrate that what was recorded is what actually happened.
2. **No enforcement before or after inference.** Nothing stands between an application and a model. Nothing prevents a model from returning a Social Security Number, an API key, or harmful content.
3. **No way to audit a conversation as a unit.** AI interactions are conversations, not individual API calls. Traditional tooling treats each request atomically. There is no cryptographic guarantee that a series of turns hasn't been edited, reordered, or silently dropped.

Most existing approaches address one of these at a time — and incompletely. Metadata logging tells you a call was made, not what was said. Input filters check the prompt, not the response. Session logging captures turns, but doesn't prove their integrity.

**Our position is that none of these partial solutions are sufficient for regulated industries or high-stakes AI deployments.** Governance requires proof, not trust. TruzenAI is built around that belief end to end.

---

## 2. Our Approach

The gateway is a **security and audit proxy**. Applications point at it instead of the LLM provider. It intercepts every request, authenticates the caller, enforces policies, inspects the response, records a cryptographic audit trail, and then forwards the call to the actual model. From the application's perspective, nothing changes.

```
                            TRUZENAI

   Your App ──▶ ┌──────────────────────────────────────┐ ──▶ LLM
                │                                      │     (OpenAI,
                │   Identity   Attestation   Policy    │     Anthropic,
                │   Budget     Content Safety          │     Ollama,
                │   Routing    Session Chain           │     HF, …)
                │   Audit      Dashboard               │
                │                                      │
                └──────────┬────────────┬──────────────┘
                           │            │
                           ▼            ▼
                   ┌─────────────┐  ┌─────────────┐
                   │  Walacor    │  │  Local WAL  │
                   │  Backend    │  │  (SQLite)   │
                   └─────────────┘  └─────────────┘

       Dual-write: every record lands in both stores at once.
```

Four design principles shape everything we built:

**1. Record everything, not metadata about everything.**
We capture the full prompt text and the full response content — not just a timestamp and a model name. The record is sent to Walacor's backend, which hashes it on ingest. This means the backend can prove the content it stored is exactly what the gateway sent. No summarization, no sampling, no truncation.

**2. Fail closed, always.**
If the governance layer cannot verify something, it does not allow it. Attestation cache stale? Block. Policy cache expired? Block. WAL disk full? Block. We believe a governance system that degrades silently into a pass-through is not a governance system. Operators get a `fail_closed` health signal before requests are impacted, giving them time to react.

**3. The audit trail must be provably complete.**
Every request that enters the gateway — allowed, denied, auth-failed, errored, or timed out — produces exactly one attempt record. This is enforced by the outermost middleware layer, which runs before all other processing. It cannot be bypassed. Regulators and auditors can verify completeness without trusting internal logs:

```
Total attempts = Allowed + Denied + Errors    (no gaps permitted)
```

**4. Governance is infrastructure, not a library.**
Applications require zero code changes. One base URL change is the entire integration. We believe governance must not depend on application developers remembering to call it.

---

## 3. What We Capture

### On every request

| What | How | Why it matters |
|------|-----|---------------|
| Full prompt text | Stored in execution record | Enables retrospective review of what was actually asked |
| Full response content | Stored in execution record | Response is where PII leakage and harmful content appear |
| Caller identity | Resolved from API key, JWT, or forwarded headers | Every record is tied to an authenticated caller — a human user via SSO, or a service account via API key |
| Provider request ID | Extracted from provider response headers | Ties the gateway record to the provider's own logs |
| Model attestation ID | Looked up from the control plane | Proves which registered model was requested |
| Model content digest | Fetched from Ollama for local models | For on-device models, proves which exact weights produced the response |
| Policy version and outcome | From policy evaluation step | Proves which rules were applied and what they decided |
| Content analyzer verdicts | From the post-inference pipeline | Records which analyzers ran, their verdicts, and their confidences |
| Tool events | Captured during tool execution | Records every tool the model called and what each tool returned |
| Token usage | Prompt, completion, and total | Enables budget reconciliation and cost attribution |
| Latency | Gateway-measured end-to-end | Measures real user-facing latency, not model-provider latency |
| Thinking content | Separated from final answer | For reasoning models, reasoning tokens are stored separately |
| Tenant and gateway instance | From configuration | Ties every record to an accountable entity |
| Timestamp | UTC, ISO 8601 | Required for any regulatory chain of custody |

### On every conversation turn (session chain)

When a caller provides a `session_id`, each turn is cryptographically linked to the previous one. Every record contains a fingerprint of the turn before it, creating an unbroken chain across the entire conversation.

- Any deleted turn breaks the chain — the sequence gap is detectable.
- Any edited turn breaks the chain — the fingerprint no longer matches.
- Any reordered turns break the chain — the link to the prior turn no longer matches.
- The first turn in every session always starts at position 0.

**Why conversations, not just calls?** Because AI risk doesn't live in individual requests — it lives in conversations. A model might reveal sensitive information only when a conversation reaches a certain point. Compliance reviewers need to reconstruct the full interaction, not spot-check isolated API calls. The session chain makes that reconstruction cryptographically sound.

The chain is verifiable end-to-end without decrypting or reading any content.

### On every failure

Even requests that never reach a model produce a `GatewayAttempt` record: auth failures, parse errors, policy blocks, provider timeouts. The audit trail is not limited to successful inferences. The completeness invariant holds for error paths as strictly as for success paths.

---

## 4. How We Enforce — The Pipeline

Enforcement runs in a defined sequence. Each step is a gate that the request must pass before proceeding. The order is deliberate: we never want inference to happen on a request that would have been blocked, and we never want a response to leave the gateway without being inspected.

```
Incoming request
  │
  ├── [Always] Completeness record reserved — GatewayAttempt is guaranteed on every exit
  │
  ├── 1.  Authenticate and resolve identity    API key, JWT, or mixed-mode; user, team, roles
  ├── 2.  Classify and check cache             Tag traffic; serve from cache if the same prompt was just answered
  ├── 3.  Route by model to provider           Read the model field; match against the routing table
  ├── 4.  G1 Model attestation                 Is this model registered and not revoked?
  ├── 5.  G3 Pre-inference policy              Evaluate the active policy; run shadow candidates alongside
  ├── 6.  Prompt-side safety                   Injection/jailbreak scan; OCR and analyze any attached images
  ├── 7.  Backpressure and budget              Is there room to record this? Does this tenant have budget?
  ├── 8.  Forward to a healthy endpoint        With retry, fallback, and circuit-breaker logic
  ├── 9.  Stream safety                        Scan streaming tokens for PII and harmful content in real time
  ├── 10. Tool loop (if the model calls tools) Execute, analyze, and continue until a final answer
  ├── 11. G4 Separate and inspect              Split reasoning from the final answer; run full content analyzers
  ├── 12. G5 Append to the session chain       Link this turn to the previous one
  ├── 13. G2 Dual-write the execution record   Walacor backend + local WAL, both required
  └── 14. Emit telemetry and deliver           Prometheus metrics, OpenTelemetry span, response to caller
```

**The three modes reflect our thinking on rollout, not a compromise on integrity:**

| Mode | Governance | Audit | When to use |
|------|-----------|-------|-------------|
| **Audit-only** | Off | Full | Pilots, initial rollout — record everything, enforce nothing |
| **Shadow** | Simulated | Full + `would_have_blocked` | Baseline before going live — see what would have been blocked |
| **Enforced** | Full | Full | Production |

The progression is: observe → baseline → enforce. Each stage produces a complete audit trail. No stage produces a partial one.

---

## 5. The Five Guarantees

### G1 — Model Attestation

We don't trust that the model being called is the model the application thinks it's calling. Every request is matched against the attestation registry. Unregistered, revoked, or unrecognized models are blocked before inference occurs.

For local model execution environments, the gateway goes further: it fetches the model's SHA256 content digest from the Ollama registry and records it alongside the inference. This means the record contains proof of which exact model weights produced the response — not just the model name.

If the attestation cache is stale (control plane unreachable), the gateway blocks. We chose this over a fail-open default because a stale attestation list could silently allow a revoked model.

**When no control plane is configured**, the gateway can operate in self-attestation mode: models are registered automatically on first use with a `self-attested:<model-id>` identifier. This is intended for development and single-operator deployments; production environments typically run with a control plane that maintains the registry explicitly.

### G2 — Full-Fidelity Audit

The execution record is the core artifact. It contains everything listed in Section 3. It is written to both Walacor's backend and the local WAL on every successful inference.

The gateway sends the full record — including the complete prompt text and response content — to Walacor's backend, which hashes it on ingest. The backend can prove that what it stored is exactly what was received.

For streaming responses, we buffer the full response content alongside the live stream. Chunks are forwarded to the caller in real time while being accumulated for the audit record. The upstream HTTP status code is captured before the first byte is returned, so the caller always gets the actual provider status.

### G3 — Pre-Inference Policy

Requests are evaluated against a versioned policy set before any inference occurs. Policies can reference the model, the caller, the provider, the attestation status, and the prompt text itself. The policy version applied and the outcome (pass, blocked, flagged) are recorded in the execution record.

If the policy cache expires while the control plane is unreachable, the gateway fails closed. We will not enforce an unknown policy on live traffic.

**Shadow mode** is the recommended way to introduce policy enforcement. Every request is forwarded regardless of policy outcome; violations are recorded as `would_have_blocked=true`. Teams can review the shadow audit trail and tune policies before switching to enforced mode — without impacting production traffic during the transition.

### G4 — Post-Inference Content Gate

**Input filtering is not enough.** Models can be prompted correctly and still return harmful content. The risk is in the output. After the model responds and before the response is returned to the caller, we run every response through pluggable content analyzers — deterministic PII scanners, an optional named-entity detector, a keyword toxicity filter, and an LLM-based safety classifier covering fourteen harm categories. Analyzers run in parallel, each with its own enforcement tier: high-risk PII and child-safety findings block the response; lower-risk findings flag it in the audit trail without interrupting delivery. Section 8 describes the analyzers in detail.

### G5 — Session Chain Integrity

Every conversation turn with a `session_id` is cryptographically linked to the previous turn. The chain construction:

- First turn: `sequence_number = 0`, `previous_record_hash = "000…000"` (genesis)
- Every subsequent turn: `previous_record_hash` = the prior turn's `record_hash`
- Sequence numbers are committed only after a successful audit write — a failed write leaves no mark in the chain

**Session chains survive restarts.** When the gateway starts, it re-reads the tail of each active session from the local WAL and restores the in-memory chain state. A gateway crash does not force clients to start a new conversation, and any crash-induced discontinuity in the chain is detectable by the same chain verification that protects the rest of the audit trail.

We think of this as the answer to the question: "Can you prove no one edited this conversation?" The answer is yes — any modification, deletion, or reordering of turns is detectable by anyone who can verify SHA3-512 hashes, without accessing the decrypted content.

---

## 6. Authentication and Caller Identity

An audit record that cannot be tied to a specific person, team, or role is of limited value to a compliance reviewer. Every interaction through the gateway is attributed to an authenticated caller, and the attribution is stored in the execution record alongside the prompt and response. We support three authentication modes:

| Mode | Credentials | Use case |
|------|-------------|----------|
| **API key** | Shared gateway key in `X-API-Key` or `Authorization: Bearer` | Service-to-service, internal automation |
| **JWT / SSO** | Bearer token from the organization's identity provider | Human users behind Okta, Azure AD, Google Workspace, or any OIDC provider |
| **Both** | JWT is checked first, API key is a fallback | Gradual migration from static keys to SSO |

JWT validation supports HS256, RS256, and ES256. For asymmetric algorithms, the gateway fetches the signing keys from the identity provider's JWKS endpoint and caches them for one hour. Issuer and audience claims are validated if configured.

### From token to audit record

```
    Request arrives
          │
          ▼
    ┌─────────────────────┐
    │  Auth middleware    │──── No credential?        → 401, logged
    │                     │──── Invalid JWT?          → 401, logged
    │                     │──── Unknown API key?      → 401, logged
    └──────────┬──────────┘
               │ valid
               ▼
    ┌─────────────────────┐
    │  Identity resolver  │
    │                     │   Reads, in order:
    │                     │     1. JWT claims (sub, email, team, roles)
    │                     │     2. X-User-Id header (fallback)
    │                     │     3. X-Team-Id header
    │                     │     4. X-User-Roles header
    └──────────┬──────────┘
               │
               ▼
    ┌─────────────────────┐
    │   Execution record  │
    │                     │   user_id, email, team, roles, identity_source
    └─────────────────────┘
```

**Identity cross-checking.** When both a JWT and explicit identity headers are present, the gateway compares them. If they disagree, the JWT wins and the mismatch is recorded in the audit trail. This prevents a caller from presenting a valid JWT and then claiming to be someone else via headers.

The dashboard can filter every view by user, team, or role. A compliance officer looking for "every interaction by the marketing team in March" gets the result in one query.

---

## 7. Audit Storage and Durability

We believe audit records should survive network failures. The gateway uses a two-backend design and writes to both in every governance path.

| Backend | When active | Durability |
|---------|-------------|------------|
| **Walacor direct** | Walacor credentials configured | Async HTTP write with JWT Bearer auth; Walacor handles long-term storage and on-ingest hashing |
| **Local WAL (SQLite)** | Always, when lineage is enabled | SQLite WAL mode with `synchronous=FULL` (fsync on commit); background worker delivers records to the control plane when online |

```
    Execution complete
            │
            ▼
    ┌─────────────────┐
    │  Dual writer    │
    └───┬─────────┬───┘
        │         │
        ▼         ▼
  ┌──────────┐ ┌──────────┐
  │ Walacor  │ │ Local WAL│
  │ backend  │ │ (SQLite) │
  └──────────┘ └──────────┘
        │         │
        │         └── Delivery worker drains to control plane
        │             when connectivity resumes
        │
        └── On-ingest hash — tamper-evident at the backend
```

In both paths, the record is committed before the response is returned to the caller (for non-streaming requests) or before the stream ends (for streaming). A network partition between the gateway and Walacor's backend produces a queue of local records that drain automatically when connectivity resumes.

**Backpressure is explicit.** The WAL has configurable size limits with three-tier signaling:

```
   healthy ─────▶ degraded ─────▶ fail_closed
                  (80% full)      (100% full)

      Normal       Warning         Requests
      operation    only            rejected
```

Ops teams get a signal before any records are lost. Fail-closed is the only state in which the gateway rejects requests due to its own inability to record them.

---

## 8. Content Safety — Layered by Design

The post-inference gate runs multiple analyzers in parallel, each with its own responsibility and enforcement tier. We chose a layered design because no single analyzer is sufficient: deterministic rules are fast and precise but miss novel content; ML classifiers catch novel content but produce false positives; domain-specific detectors handle the categories that other tools miss.

```
                          CONTENT SAFETY PIPELINE

                      Model response (or streamed chunk)
                              │
          ┌───────────────────┼───────────────────────┐
          │                   │                       │
          ▼                   ▼                       ▼
    ┌──────────┐      ┌───────────────┐       ┌──────────────┐
    │   PII    │      │   Toxicity    │       │ Llama Guard  │
    │ scanner  │      │    filter     │       │  classifier  │
    │          │      │               │       │              │
    │ regex +  │      │ keyword list  │       │ 14 safety    │
    │ Presidio │      │               │       │ categories   │
    └────┬─────┘      └───────┬───────┘       └──────┬───────┘
         │                    │                      │
         └──────── verdicts ──┼──────────────────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │  Policy combiner     │
                   │                      │
                   │  PASS / WARN / BLOCK │
                   └──────────┬───────────┘
                              │
                              ▼
                         To the caller
                         (or blocked, with verdict in audit record)
```

### The analyzers

| Analyzer | Approach | What it catches | Verdict tiers |
|----------|----------|-----------------|---------------|
| **PII (deterministic)** | Regex, Luhn-validated | Credit cards, SSNs, AWS keys, API tokens | High-risk types `BLOCK`; emails, phones, IPs `WARN` |
| **PII (Presidio)** | Named-entity recognition | Broader PII coverage than regex — names, addresses, dates | Configurable per category |
| **PII sanitizer** | Redaction instead of blocking | Same detection, different action | Inline redaction for lower-risk contexts |
| **Toxicity** | Keyword deny-list | Harmful language, custom terms | Typically `WARN`; configurable |
| **Llama Guard** | Llama-based classifier | 14 safety categories: violence, sexual content, hate, self-harm, child safety, weapons, privacy, and more | Child safety `BLOCK`; all others `WARN` |
| **Image OCR** | Tesseract + content analyzers | PII and harmful content in attached images | Runs the other analyzers on extracted text |

**Why PII severity tiers?** In practice, blocking every email address or phone number produces false positives that drive operators to disable the analyzer entirely. We split high-risk PII (which should block) from lower-risk PII (which should flag) so that the block tier remains credible and the flag tier captures what needs review.

**Why both deterministic and ML?** Regex is fast, deterministic, and free of false positives on exact matches. Presidio and Llama Guard catch what regex cannot. Running them in parallel means the gateway does not have to choose between speed and coverage; the fast deterministic layer handles the common cases and the slower classifiers handle the rest.

### Verdicts

- **PASS** — no concerns; content forwarded unchanged.
- **WARN** — concerns recorded in the audit trail; content forwarded.
- **BLOCK** — response rejected; caller receives a 403 with an explanation; the full record, including the triggering content, is stored for review.

### Extensibility

Custom analyzers can be added by implementing a single interface without touching the pipeline. This is intentional: we expect content analysis requirements to evolve — new regulations, new attack patterns, domain-specific detectors — and we built for extensibility.

### Analyzer caching

Content analysis is deterministic for a given input. We cache analyzer verdicts keyed by a hash of the content, bounded to a safe in-memory size. When the same response is analyzed twice (due to retry, fallback, or legitimate repetition), the cached verdict is reused.

---

## 9. Prompt Injection and Jailbreak Detection

The other half of content safety is the prompt side. Users — or upstream systems inserting untrusted content into prompts — can try to override a model's instructions. The most common attack patterns include jailbreak prompts, system-prompt injection, and indirect injection through documents or web content.

We run an optional prompt injection classifier based on Meta's Prompt Guard 2 — a small DeBERTa-based model that categorizes prompts into three classes: benign, injection, or jailbreak. The classifier runs on CPU with low enough overhead to be invoked inline on every request, and does not require a GPU.

**Why a small specialized model?** General-purpose content analyzers are not designed to detect adversarial prompt patterns. A focused classifier, trained specifically for this task, catches injection attempts that a general PII or toxicity filter would miss.

**Indirect injection through tools.** When the model uses a tool (web search, document retrieval, file read), the tool's output becomes part of the prompt context for the next inference step. We run the full content analyzer pipeline on tool outputs before they are returned to the model, so that an attacker cannot inject instructions by poisoning a web page or a document.

---

## 10. Streaming Safety

Streaming responses must be subject to the same content gate as non-streaming responses. When a model streams tokens to the caller one piece at a time, the naive approach is to forward each token immediately and log the assembled response afterwards. That design lets harmful content reach the user before any analyzer sees it, which would turn streaming into an effective bypass of the safety layer. We took a different approach.

```
       Model                 Gateway                  Caller
         │                      │                        │
         │─── token chunk ─────▶│                        │
         │                      │── stream safety scan  │
         │                      │                        │
         │                      │   PASS → forward ─────▶│
         │                      │   HOLD → buffer        │
         │                      │   BLOCK → close stream │
         │                      │                        │
         │─── token chunk ─────▶│                        │
         │                      │   ...                  │
         │                      │                        │
         │─── final chunk ─────▶│                        │
         │                      │── full analyzers run   │
         │                      │                        │
         │                      │   PASS → drain buffer ▶│
         │                      │   BLOCK → replace      │
         │                      │           with error   │
```

The stream safety scanner runs on token windows as they arrive. High-confidence hits on high-severity patterns interrupt the stream immediately. Everything else is finalized with the full analyzer pipeline when the stream ends. The caller sees the final answer only after both the streaming scanner and the final analyzers have approved it.

**The cost of this design is a small buffering delay.** The benefit is that streaming cannot be used as a bypass for content safety.

---

## 11. Reasoning Model Handling

Modern reasoning models — Ollama's `<think>` blocks, Anthropic's extended thinking, OpenAI's o-series reasoning tokens — produce two kinds of output: the reasoning trace and the final answer. Under a naive design, the reasoning trace would either be hidden from the user and also hidden from the audit trail, or mixed in with the final answer and shown to the user.

We separate them explicitly.

```
                  Reasoning model response
                          │
                          ▼
              ┌──────────────────────┐
              │  Thinking separator  │
              └──────────┬───────────┘
                         │
            ┌────────────┴────────────┐
            ▼                         ▼
      ┌─────────────┐           ┌─────────────┐
      │  Reasoning  │           │    Final    │
      │   content   │           │   answer    │
      └──────┬──────┘           └──────┬──────┘
             │                         │
             ▼                         ▼
     Stored in audit record   Returned to the caller
     as thinking_content      as the primary response
             │                         │
             └──────────┬──────────────┘
                        ▼
             Content analyzers run on both
```

**Why analyze the reasoning trace?** Because a reasoning model can reach the final answer through a reasoning path that itself contains harmful content, PII, or policy violations. Analyzing only the final answer would let the reasoning content slip past the safety gate — and with some models the visible answer is essentially a summary while the substantive content lives in the reasoning trace. Our analyzers treat the reasoning content as response content for safety purposes: when a model produces an empty or thin final answer, we fall back to analyzing the reasoning trace directly.

The reasoning content is stored in the audit record but is not returned to the caller by default. Operators can choose to surface it in the dashboard for review.

---

## 12. Multiple Models, One Port, Separate Audit Trails

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

All three go through port 8000. All three produce fully differentiated audit records. A compliance reviewer querying the backend can filter by model, provider, or attestation ID independently.

### Configuring the routing table

```json
[
  {"pattern": "gpt-*",    "provider": "openai",    "url": "https://api.openai.com",    "key": "sk-..."},
  {"pattern": "claude-*", "provider": "anthropic", "url": "https://api.anthropic.com", "key": "sk-ant-..."},
  {"pattern": "llama*",   "provider": "ollama",    "url": "http://localhost:11434",     "key": ""}
]
```

Patterns use standard wildcard matching (`gpt-*` matches `gpt-4`, `gpt-4o`, `gpt-4-turbo`). The first matching rule wins. Unrecognized models fall through to a path-based default. The routing table is loaded once at startup — there is no per-request parsing cost.

### When you do need separate ports

One port per gateway instance is correct for most deployments. The only reason to run multiple instances on separate ports is **tenant isolation** — one `WALACOR_GATEWAY_TENANT_ID` per instance is a hard boundary. If two business units must have completely separate audit namespaces, they get separate gateway instances. Model isolation does not require separate ports.

### Supported providers

Five provider adapters are fully implemented:

| Provider | Streaming | Notes |
|----------|-----------|-------|
| OpenAI | Yes | Drop-in; captures `chatcmpl-xxx` request ID |
| Anthropic | Yes (SSE) | Drop-in; captures `msg_xxx` request ID |
| Ollama | Yes | Fetches model content digest for local attestation |
| HuggingFace | Yes | TGI and Inference Endpoint formats |
| Generic | Yes | JSONPath-configurable for any REST API |

### Using Open WebUI as the interface layer

Teams running local models commonly use **Open WebUI** — a popular open-source chat interface that looks and feels like ChatGPT, but runs entirely on-premise against local models. By default it points directly at the model server, which means every conversation bypasses governance.

The fix is one setting change:

```
Before:  Open WebUI → Ollama              (no governance, no audit)
After:   Open WebUI → TruzenAI → Ollama   (fully governed and audited)
```

Open WebUI supports configuring a custom OpenAI-compatible API endpoint. Set that endpoint to the gateway URL and every conversation through the UI — every prompt, every response, every tool call — is intercepted, enforced, and recorded. Nothing changes for the user.

---

## 13. Model Capability Discovery

Not every model supports function calling. Sending a tool definition to a model that does not support it produces a 400 or 422 error, wastes a round trip, and confuses operators. The naive alternative is to require per-model configuration: maintain a list of which models support which features and update it as new models ship.

We took a different approach.

```
                 First request to a new model
                           │
                           ▼
                 ┌─────────────────────┐
                 │  Inject tools and   │
                 │  send to provider   │
                 └──────────┬──────────┘
                            │
                ┌───────────┴──────────┐
                ▼                      ▼
         200 OK (success)     400/422 (tool-unsupported)
                │                      │
                ▼                      ▼
         Cache: supports_tools=True   Retry without tools
                                      Cache: supports_tools=False
                                      │
                                      ▼
                              Return the non-tool response

               Subsequent requests to the same model
                           │
                           ▼
                 ┌─────────────────────┐
                 │  Check capability   │
                 │       cache         │
                 └──────────┬──────────┘
                            │
                ┌───────────┴──────────┐
                ▼                      ▼
          True / unknown            False
          Inject tools              Skip tool injection
                                    Preserve streaming
```

The gateway learns each model's tool support on the first request, caches the result, and applies it to every subsequent request. Operators never configure this. Models that do not support tools get real SSE streaming on every request after the first. Models that do support tools are routed through the tool-calling flow described in the next section.

The capability cache includes a TTL and re-probes periodically to catch provider-side capability changes.

---

## 14. Tool Calls and MCP — Why There Is No Second Gateway

Modern AI models don't just answer questions. They call tools: search the web, query databases, run code, read files. These tool calls happen *after* the model receives the prompt and *before* it gives a final answer. Under a naive proxy design, that entire middle section is invisible to the gateway — and therefore absent from the audit trail.

### The option we considered and rejected

Put a second gateway after the LLM specifically to intercept MCP tool calls. This is the obvious answer and the wrong one. Two gateways means two audit trails that need to be stitched together, two infrastructure components to operate, and a structural gap wherever the two systems don't sync. The completeness invariant — every interaction produces exactly one record — breaks the moment you split the audit across two systems.

### What we built instead

The gateway detects which kind of provider it is talking to and applies one of two strategies automatically. There is no second component.

```
Your App → TruzenAI ──────────────► LLM Provider
                │                               │
                │          ┌────────────────────┘
                │          │
                │    Cloud model (OpenAI, Anthropic)?
                │          └─► Provider already reports tool calls in its response.
                │               Gateway reads them out and attaches them to the
                │               audit record. No extra infrastructure.
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

Every tool call — what was asked of the tool, what the tool returned, how many iterations the model took — is captured in the **same execution record** as the prompt and the final response. Cryptographic hashes of tool inputs and outputs are stored alongside them.

For cloud providers, this requires zero additional infrastructure. For local models, the gateway acts as the agentic loop controller, giving it complete visibility and control over every tool execution — including the ability to run content analyzers on tool outputs before the results are returned to the model. The application changes nothing.

---

## 15. Built-in Tools

We ship a small set of built-in tools that plug into the same tool loop described in Section 14 without requiring an external MCP server. The first one is **web search**.

Web search supports three providers: DuckDuckGo, Brave, and SerpAPI. The gateway executes the search, normalizes the results, runs content analysis on the returned snippets, and attaches the full source list (URLs, titles, and snippets) to the audit record. Every source is hashed individually. A compliance reviewer can see exactly which web pages the model read before it wrote its answer.

**Why built-in?** External MCP servers are the right long-term answer for most tools, but web search is common enough and simple enough that requiring an external server adds operational cost without adding value. A built-in tool also means content analysis runs on tool output with no additional configuration.

Built-in tools conform to the same interface that external MCP servers use. Adding a new built-in tool is a matter of implementing two methods — `get_tools()` and `call_tool()` — and registering the tool at startup. No changes to the pipeline are required.

---

## 16. Multi-Endpoint Routing and Resilience

A single model often has multiple endpoints behind it. A self-hosted Llama model might be deployed on several GPUs. A commercial provider might offer multiple regions. A disaster recovery setup might have a primary and a secondary. The gateway handles this natively, without pushing the problem onto the caller or a separate load balancer.

### Model groups

A model group is a set of endpoints that all serve the same model. Requests matching the group are distributed across its healthy endpoints. Each endpoint has its own URL, API key, and weight.

### Load balancing — Power of Two Choices

When a group has more than one healthy endpoint, the gateway picks two at random and selects the one with fewer outstanding requests. This is the Power-of-Two-Choices algorithm: cheap, robust, and known to approximate optimal load distribution without requiring global state.

```
    Request for model X
            │
            ▼
    ┌──────────────┐
    │  Find healthy│
    │  endpoints   │
    └──────┬───────┘
           │
           ▼
     ┌─────────────┐
     │  Pick two   │   (at random)
     │  candidates │
     └──────┬──────┘
            │
            ▼
     ┌────────────────────────┐
     │  Choose the one with    │
     │  fewer outstanding      │
     │  requests               │
     └────────────┬───────────┘
                  │
                  ▼
           Forward the request
```

### Circuit breakers

Each endpoint has a circuit breaker that tracks recent failures. The breaker has three states:

```
       closed ─── failures ≥ threshold ──▶ open
          ▲                                  │
          │                              cooldown
          │                                  │
          │           probes succeed         ▼
          └───── closed ◀──── half-open ───── (after cooldown)
```

A healthy endpoint stays in the `closed` state and serves traffic normally. After enough failures, it moves to `open` and is bypassed for a cooldown period. After cooldown it moves to `half-open` and serves a limited number of probe requests. If the probes succeed, the breaker returns to `closed`. If they fail, cooldown restarts with exponential backoff.

**Why circuit breakers?** A sick endpoint that fails slowly is worse than one that fails fast — it ties up resources, produces partial errors that are hard to diagnose, and invites the rest of the system to keep sending it traffic it cannot handle. A circuit breaker converts a slow failure into a fast one, gives the endpoint room to recover without being hammered, and protects the rest of the fleet from correlated failure.

**Slow-call detection** treats requests that exceed a configurable duration as failures, even when they return successfully. A responsive-but-broken endpoint fails quietly under many designs; we chose to catch it.

### Fallback — error-aware routing

Not all errors should trigger the same reaction. A rate-limited request should try a different endpoint; a context-overflow error should try a larger-context model; a content-filter rejection should be surfaced to the caller as-is.

```
             Error from primary
                    │
                    ▼
         ┌──────────────────────┐
         │  Classify the error  │
         └──────────┬───────────┘
                    │
    ┌───────────────┼───────────────────┬───────────────┐
    ▼               ▼                   ▼               ▼
 rate_limited   context_overflow   content_policy    5xx / timeout
    │               │                   │               │
    ▼               ▼                   ▼               ▼
 Try another    Try larger-         Return as-is    Try another
 endpoint in    context model       (do not         endpoint;
 the group                          mask moderation) cooldown primary
```

This is implemented as a small set of classification rules over status code and error message text. The classifier is deterministic and can be audited.

### Retry with backoff

Transient failures retry up to a configured limit with exponential backoff and jitter. Retries are recorded in the audit record so that a single logical request is reconstructable even when it touched multiple endpoints.

### Concurrency limits

Per-endpoint concurrency limits prevent a single endpoint from being saturated under load. When the limit is reached, new requests wait briefly for a slot or are routed to another endpoint in the group.

---

## 17. A/B Testing and Traffic Splitting

When a team wants to compare two models — "does this new smaller model work as well as the large one?" — the governed way to do this is to split traffic between them and compare the audit records. We support this natively.

```
    Incoming request for qwen3:*
              │
              ▼
     ┌─────────────────┐
     │   A/B selector  │     Weighted random:
     │                 │       50% qwen3:1.7b
     │                 │       50% qwen3:4b
     └───────┬─────────┘
             │
             ▼
    Rewrite model field
    Record original and variant
             │
             ▼
    Continue pipeline normally
```

The selected variant is stored in the execution record as `ab_variant`. The originally requested model is stored as `ab_original_model`. A compliance reviewer can reconstruct which variant served a given request, and analysts can compare the two populations directly against the same audit fields — latency, token usage, content verdicts, downstream errors.

**Why this lives in the gateway.** Traffic splitting outside the gateway produces either two audit trails that need to be stitched together, or one audit trail that is missing the information about which variant was chosen. Both options erode the completeness invariant. Moving the split into the gateway keeps the invariant intact — whichever variant is chosen, there is exactly one record, and the record explicitly notes the choice. It also means the safety gate, the session chain, and the policy engine all see the variant that was actually used, not the variant that was originally requested.

---

## 18. Semantic Response Caching

When the same prompt is sent to the same model more than once, the provider charges twice and returns the same answer. For many workloads this represents real waste.

The gateway has an optional exact-match response cache. It is keyed by a hash of the model identifier and the prompt content, with a bounded size and a time-to-live. Cache hits are served directly from the gateway without contacting the provider.

**Why exact-match rather than embedding similarity?** Because deterministic caches have no false positives. An embedding-based cache would return "similar" responses that differ in important ways. We chose the trade-off of lower hit rate in exchange for zero risk of silently returning a non-matching answer.

**Cache entries still produce audit records.** A cached response is served to the caller, and a new execution record is written that references the cache entry. The audit trail continues to reflect one record per request.

---

## 19. The Adaptive Layer

The gateway should know when it is sick before users do, and it should report its own state honestly. Most governance systems fail silently at startup and discover their own misconfiguration only after the first real request hits a real user. We built a small set of components that watch the gateway's own environment and surface problems earlier.

**At startup**, the gateway runs probes against each configured provider URL, each endpoint in each model group, the WAL partition's free space, and each provider's API version. Probes fail open — a transient startup problem does not prevent the gateway from starting — but every result is exposed in the `/health` endpoint for operators to inspect. A misconfigured provider URL surfaces at startup instead of at the first real request.

**At runtime**, the gateway watches its own health: disk space on the WAL partition, per-provider error rates over a rolling window, endpoint response times, and memory pressure. When a provider's error rate crosses a threshold (for example, more than half of requests failing in the last minute), the gateway puts it into a short cooldown. Load shifts to healthy endpoints until the cooldown ends, at which point the provider is probed and returned to service if healthy. Cascade failures are contained before they reach the caller.

**Some configuration is hot-reloadable.** Content analyzer policies — which categories should BLOCK, WARN, or PASS — are stored in the control plane and can be updated without restart. A compliance team can tighten or loosen a policy in response to a specific incident without shipping a new gateway version.

**Per-model capabilities are learned, not configured.** The capability registry records tool support, context window size, response format support, embedding dimensions, and per-model timeouts. It probes periodically and caches results with a short TTL. Reasoning models get longer timeouts automatically; embedding models get shorter ones. Operators do not maintain a per-model capability list.

**Request classification** is an orthogonal concern handled in the same layer. A synthetic health check, a batch job, and an interactive user request deserve different handling for routing, logging, and metrics — though not for security. The gateway reads a `task` field from the request body, detects synthetic traffic via the user-agent, and falls back to a prompt-level regex if neither is present. Classification never changes security decisions.

**Every component is extensible.** Startup probes, request classifiers, resource monitors, and identity validators are all defined by interfaces. Organizations can register their own implementations via a configuration path that loads a Python class at startup. A security team can add a custom probe — for example, verifying that their secrets manager is reachable — without forking the gateway.

---

## 20. The Embedded Control Plane

Governance systems usually require a separate control plane — a second service that stores policies, attestations, and budgets, and that the gateway pulls from at runtime. This is fine for large deployments but creates operational overhead for smaller ones.

We built an embedded control plane that runs inside the gateway process and is backed by SQLite. It exposes the same CRUD API as the external control plane. A team can start governing AI interactions immediately, without standing up a second component.

```
   ┌────────────────────────────────────────────────────┐
   │                                                    │
   │                      TRUZENAI                      │
   │                                                    │
   │   ┌──────────────┐         ┌──────────────────┐    │
   │   │              │         │                  │    │
   │   │  Enforcement │◀────────│ Embedded control │    │
   │   │   pipeline   │         │     plane        │    │
   │   │              │         │                  │    │
   │   │              │         │  Attestations    │    │
   │   │              │         │  Policies        │    │
   │   │              │         │  Budgets         │    │
   │   │              │         │  Content policies│    │
   │   │              │         │  Shadow policies │    │
   │   │              │         │  Pricing         │    │
   │   │              │         │  Key assignments │    │
   │   │              │         │                  │    │
   │   └──────────────┘         └──────────────────┘    │
   │                                                    │
   └────────────────────────────────────────────────────┘
                           │
                           ▼
                     SQLite database
                     (local file)
```

The embedded control plane is fully functional and manages:

- Attestations (add, remove, update, revoke a model registration)
- Policies (pre-inference rules, with versioning)
- Budgets (per tenant, user, and period)
- Content analyzer policies (per category, per analyzer)
- Shadow policies (candidate rules evaluated in silent mode)
- Model pricing (for cost attribution)
- API key to policy and tool assignments
- Policy templates (pre-built rule sets for common compliance profiles)
- On-demand model discovery (scan configured providers for available models)

All of the above are exposed through CRUD endpoints and can be managed from the dashboard.

**When to use the external control plane.** When multiple gateway instances must share the same registry. When policies and attestations are managed by a dedicated security team separate from operations. When a centralized audit of governance state is required.

**When to use the embedded control plane.** Everything else. A single gateway, a single team, a single set of policies. The embedded mode removes the "stand up a second service" prerequisite that often slows down the initial rollout of governance.

### Background sync

In embedded mode, a background worker keeps the in-memory caches (attestation, policy, budget, content policy) fresh by re-reading the SQLite database on a short interval. Mutations via the API also refresh caches immediately. This is the mechanism that prevents the fail-closed trigger from misfiring: the policy cache is never stale.

### Auto-attestation

In development environments without an explicit attestation list, the gateway can auto-attest models on first use. The first request to a new model creates a `self-attested:<model-id>` registration; subsequent requests match against it. Auto-attestation is disabled when an explicit control plane is present — we do not want auto-attestation to silently re-approve a revoked model.

---

## 21. Horizontal Scaling

When teams scale to multiple gateway replicas, three things break if state is in-process: session chains diverge, budget counters double-spend, and sequence numbers collide. We solved all three through Redis.

When Redis is configured:

- **Session chain** is stored in Redis as a hash (`seq`, `hash` fields per session). The read and write are deliberately separated: the read operation fetches the current state but does not modify it; the write atomically updates both fields only after the audit record has been successfully committed. This two-phase design means a transient write failure leaves no ghost entry in the chain.

- **Token budgets** use an atomic Lua script for check-and-reserve — no race condition possible, no double-spend across replicas. After each LLM response, the actual token count is reconciled against the estimate with an atomic correction. The counter tracks real consumption, not pre-request guesses.

Without Redis: single-replica, in-process state. All five guarantees still hold. Redis is additive, not required.

```
  Client requests
       │
  ┌────▼──────────┐   ┌─────────────┐
  │  replica 1    │   │             │
  │  replica 2    ├───┤    Redis    ├── LLM providers
  │  replica 3    │   │             │
  └───────────────┘   └─────────────┘
       │
       └── All replicas write to Walacor backend + local WAL
```

---

## 22. Observability

### Health

```
GET /health
```

Returns the enforcement mode, storage backend, cache staleness, WAL depth, token budget snapshot, active session count, content analyzer status, and model capability cache. Health has three states:

```
   healthy ─────▶ degraded ─────▶ fail_closed
                  (warning)       (rejecting)
```

The transition from `healthy` to `degraded` is a warning signal — ops teams have time to act. `fail_closed` means requests are being rejected and immediate intervention is required.

### Metrics

```
GET /metrics
```

Returns Prometheus-format metrics under the `walacor_gateway_*` namespace. Key areas covered:

| Area | What is exposed |
|------|-----------------|
| **Request volume** | Total requests and attempts by provider, model, and outcome (allowed, blocked, errored) |
| **Pipeline latency** | Request and forward duration histograms by route and model |
| **Token usage** | Prompt, completion, and total tokens by tenant, user, and model |
| **WAL state** | Pending record count, disk usage in bytes, age of oldest pending record |
| **Sync health** | Age of the last successful sync with the control plane |
| **Delivery worker** | Total delivery attempts by outcome |
| **Content gate** | Response blocks by analyzer and category |
| **Budget** | Budget exceeded and budget fail-open events |
| **Tools** | Tool call counts and outcomes |
| **Cache** | Semantic cache hits and misses |
| **Event loop** | Event loop lag (indicator of gateway saturation) |

### Tracing

The gateway emits OpenTelemetry spans using the GenAI semantic conventions. Each request produces one retroactive span with attributes covering the model, provider, tenant, token usage, latency, safety verdicts, and session context. Spans can be exported to any OTLP-compatible backend.

**Why retroactive?** The span is emitted after the record is written, so its attributes include the actual outcome (allowed, blocked, analyzer findings). A live span would have to be updated multiple times during the request, which is error-prone and loses attributes if the span is sampled out.

### Structured logging

Logs are JSON by default, with one line per event. Correlation IDs (request ID, session ID, user ID) are included in every line that can carry them. Logs are designed to be ingested by any standard log aggregator.

---

## 23. The Dashboard

The gateway ships with a built-in web dashboard served from the same process. It is a read-only interface by default (with an authenticated control-plane tab for mutation) and does not require any additional infrastructure.

```
                          DASHBOARD NAVIGATION

    ┌─────────────────────────────────────────────────────┐
    │                                                     │
    │   Overview       Live health, throughput, charts    │
    │                                                     │
    │   Sessions       Searchable list of conversations   │
    │     Timeline     Turn-by-turn visual history        │
    │     Execution    Full detail of a single interaction│
    │                                                     │
    │   Attempts       Every request, including blocked   │
    │                                                     │
    │   Control        Models, policies, budgets, status  │
    │                                                     │
    │   Compliance     Regulatory reports on demand       │
    │                                                     │
    │   Playground     Test prompts against governed      │
    │                  models                             │
    │                                                     │
    └─────────────────────────────────────────────────────┘
```

### Overview

Live request throughput, allowed-versus-blocked counts, and system health at a glance. A real-time chart shows request rate, token rate, and analyzer verdicts over the last several minutes. A range bar switches between live and historical views covering the last hour, day, week, and month.

### Sessions and Timeline

The Sessions view lists every conversation with filters for user, team, model, and date. The Timeline view opens a single conversation and renders each turn as a card, with tool events displayed inline, content analyzer verdicts as badges, and a one-click chain verification button that recomputes every hash client-side.

### Execution Detail

The Execution view shows a single interaction in full: the prompt, the response, the reasoning content (if any), every policy that evaluated it, every content analyzer verdict, every tool that was called, token usage, latency, and the complete metadata chain. This is the view a compliance reviewer opens when they want to see exactly what happened.

### Attempts

The Attempts view lists every request that was received, including requests that were blocked before reaching a model. This is where operators look when they need to understand why a request was denied.

### Control

The Control view manages attestations, policies, and budgets. It includes an on-demand model discovery action that scans configured providers for available models and lets an operator register them with one click. The Status tab shows authentication mode, content analyzer health, provider connectivity, runtime state, and model capability cache contents.

### Compliance

The Compliance view generates regulatory reports on demand, mapped to EU AI Act Article 12, NIST AI RMF, SOC 2, and ISO 42001. Reports are rendered to PDF with the compliance status of each relevant control. See Section 24.

### Playground

The Playground provides an interactive prompt tester that runs through the full governance pipeline. It is the fastest way to confirm that a new policy behaves as intended or that a new model attestation is working.

### Chain verification from the browser

Chain verification is client-side. The browser fetches the raw records, recomputes every SHA3-512 hash with a JavaScript library, and compares the computed hashes against the stored ones. If any hash does not match, the verification fails visibly. The user does not have to trust the server to trust the verification.

---

## 24. Compliance Reporting

Regulators ask for documents. A well-governed AI system must be able to produce them on demand. The dashboard can generate a compliance report in PDF form for each of the major frameworks.

| Framework | What the report covers |
|-----------|------------------------|
| **EU AI Act** | Article 9 (risk management), Article 12 (record-keeping), Article 14 (human oversight), Article 15 (accuracy, robustness, cybersecurity), Annex IV evidence |
| **NIST AI Risk Management Framework** | Govern, Map, Measure, Manage functions with evidence from gateway operations |
| **SOC 2** | Security, availability, processing integrity, and confidentiality trust service criteria |
| **ISO 42001** | AI management system controls including risk management, operational controls, monitoring |

Each report includes:

- A compliance status summary (compliant, partial, non-compliant) per control
- Evidence drawn from the audit trail (record counts, chain integrity status, analyzer coverage)
- Model attestation inventory
- Policy versions in effect
- Incident and alert history

Reports are generated from the live audit trail — there is no separate compliance database. A report produced today reflects the actual state of the system today, not a snapshot from a periodic export.

**Why PDF?** PDF is the format auditors and regulators typically request, and it is straightforward to attach to a ticket, email, or regulatory filing. The underlying data remains available in raw form for teams that want to run their own analysis or pipe it into another system.

---

## 25. Alerts and Incident Response

Governance systems need to surface incidents to humans. A blocked credit card in a response, a chain break, a budget overrun, a circuit breaker opening — these events must be delivered to whoever is on call, in the channel they use.

The gateway has a pluggable alert bus with three built-in dispatchers:

| Dispatcher | Delivery | Use case |
|------------|----------|----------|
| **Webhook** | HTTP POST to any URL | Integration with incident management platforms |
| **Slack** | Slack API message | Real-time team notification |
| **PagerDuty** | PagerDuty Events API | On-call escalation for critical events |

```
       Event occurs in gateway
            │
            ▼
     ┌──────────────┐
     │  Alert bus   │
     └──────┬───────┘
            │
  ┌─────────┼─────────┐
  ▼         ▼         ▼
Webhook   Slack   PagerDuty
  │         │         │
  ▼         ▼         ▼
  Ticket   Channel  On-call
  created  message  paged
```

Alertable events include content-gate blocks, policy blocks, chain verification failures, WAL fill warnings, circuit breaker state changes, cache staleness warnings, and authentication failures above a configurable threshold. Each event type has its own severity and routing configuration. Custom dispatchers can be added by implementing a single interface.

---

## 26. External Audit Integration

The audit trail lives in the Walacor backend and the local WAL, but many organizations also require the records to flow into their existing security tooling — Splunk, Datadog, Elastic, or a generic SIEM. The gateway includes a pluggable exporter with two built-in backends.

| Exporter | Format | Target |
|----------|--------|--------|
| **Webhook** | JSON per record | Splunk HEC, Datadog Logs, Elastic, any HTTP endpoint |
| **File** | JSON lines | Local file for offline forwarding or long-term archive |

Exporters run asynchronously and batch records for efficiency. They retry failures with exponential backoff and drop to the file exporter if the webhook is unreachable for an extended period. Records sent to external systems are the same records written to Walacor — there is no divergence between what the gateway stores and what it exports.

**Why pluggable?** Every organization has a different SIEM configuration, and we do not want to maintain adapters for every possible target. The exporter interface is stable; a team can write their own exporter in a small amount of Python and register it at startup.

---

## 27. Deployment

Three targets are supported. No additional infrastructure is required beyond what is listed.

| Target | Files | Notes |
|--------|-------|-------|
| **Docker** | `deploy/Dockerfile`, `deploy/Dockerfile.fips` | Non-root; healthcheck built in; FIPS-140-2 image available |
| **Docker Compose** | `deploy/docker-compose.yml` | Includes optional Ollama, Open WebUI, and Redis profiles |
| **Kubernetes** | `deploy/helm/` + `deploy/network-policies/` | PVC for WAL, readiness/liveness probes, egress network policy |

Single command: `walacor-gateway` — port 8000.

### Network posture

The Kubernetes egress policy limits outbound connections to the control plane and configured providers only. The gateway cannot be used as a general-purpose HTTP client from within the cluster. This is intentional: a governance component that can reach arbitrary endpoints is a governance risk in itself.

### FIPS mode

A FIPS-140-2 build is available for environments with federal compliance requirements. The FIPS image uses a validated cryptographic module for all hashing and signing operations.

### Hosting

The gateway runs on standard infrastructure, requires no special hardware, and can be deployed on-premises, in a private cloud, or in a commercial cloud. No GPU is required for the gateway itself; content analysis models that require a GPU run in separate containers.

---

## 28. Known Scope Boundaries

These are deliberate design decisions, not gaps.

| Boundary | Rationale |
|----------|-----------|
| **Single tenant per process** | Tenant isolation is a hard boundary. Multi-tenant routing requires multiple instances, load-balanced at the edge. We chose isolation over complexity. |
| **Deterministic PII by default** | Regex and Luhn validation are fast, deterministic, and produce no false positives on exact matches. Organizations that need ML-based PII detection enable Presidio or bring their own analyzer through the content analyzer interface. |
| **No rate limiting** | Rate limiting belongs at the edge (load balancer, ingress). The gateway is the governance layer, not the traffic layer. Mixing them creates coupling that complicates scaling. |
| **Keys are environment variables or control plane entries** | Rotation via Vault or AWS Secrets Manager can be integrated through the control plane or a sidecar. We did not build a first-party secrets manager because mature ones already exist. |
| **No model hosting** | The gateway controls and audits access to models. It does not run them. Provider infrastructure is the provider's problem. |
| **Exact-match semantic cache** | We chose zero-false-positive caching over higher-hit-rate embedding caching. Adding embedding similarity is an extension point, not a default. |
| **Stream safety interrupts mid-stream only on high-confidence hits** | Interrupting on every possible concern would produce too many false positives and degrade streaming quality. We chose high confidence for stream-time interruption and the full analyzer pipeline at stream end. |

---

## 29. What We Are Not Trying to Be

It helps to be clear about what we deliberately chose not to build.

- **Not a firewall.** Firewalls work on connection-level metadata. We work on content.
- **Not a proxy that logs.** Logging is mutable. We produce cryptographic records.
- **Not an ML safety tool.** ML-based content moderation is a deep specialization. We provide the integration point for it via the content analyzer interface.
- **Not a model router for cost optimization.** We route for governance and multi-provider support. Cost optimization is a provider-level concern.
- **Not an application SDK.** Applications require zero code changes. Governance is infrastructure, not a library.
- **Not a secrets manager.** We consume keys; we do not rotate them.
- **Not a rate limiter.** That belongs at the edge.
- **Not a model host.** We audit access to models; we do not run them.

---

## 30. Reference Diagram

```
                          TRUZENAI

    Caller ──▶  ┌─────────────────────────────────────────┐
                │        COMPLETENESS MIDDLEWARE          │
                └─────────────────────┬───────────────────┘
                                      ▼
                ┌─────────────────────────────────────────┐
                │     IDENTITY        SEMANTIC CACHE      │
                └─────────────────────┬───────────────────┘
                                      ▼
                ┌─────────────────────────────────────────┐
                │     ATTESTATION (G1)    POLICY (G3)     │
                │     PROMPT SCAN         BUDGET          │
                └─────────────────────┬───────────────────┘
                                      ▼
                ┌─────────────────────────────────────────┐
                │     ROUTING    LOAD BALANCE             │
                │     FALLBACK   BREAKER   RETRY          │
                └─────────────────────┬───────────────────┘
                                      ▼
                ┌─────────────────────────────────────────┐
                │             LLM PROVIDER                │
                └─────────────────────┬───────────────────┘
                                      ▼
                ┌─────────────────────────────────────────┐
                │   STREAM SAFETY     TOOL LOOP           │
                │   REASONING SPLIT   CONTENT GATE (G4)   │
                └─────────────────────┬───────────────────┘
                                      ▼
                ┌─────────────────────────────────────────┐
                │   CHAIN APPEND (G5)   DUAL-WRITE (G2)   │
                │   TELEMETRY                             │
                └─────────────────────┬───────────────────┘
                                      ▼
                        Response to caller

    Supporting: embedded control plane  ·  adaptive probes  ·
                dashboard  ·  alerts  ·  SIEM export  ·  Redis scaling
```

---

## 31. Closing Statement

Every decision in the gateway traces back to a single proposition: governance that cannot be proven is not governance. That belief is the reason the audit trail is cryptographically sealed rather than merely logged, the reason policies are enforced before inference rather than reviewed after it, the reason content safety runs on both the prompt and the response, and the reason every request produces exactly one record regardless of outcome.

We built TruzenAI because enterprises adopting AI deserve the same standard of evidence they apply to every other regulated system they operate. Each feature in this document exists in service of that standard.
