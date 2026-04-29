# Walacor Gateway — Regulatory Compliance Mapping

This document maps Walacor Gateway capabilities to major AI governance frameworks including the EU AI Act, the NIST AI Risk Management Framework (AI RMF), and SOC 2 Trust Service Criteria. It is intended for compliance officers, auditors, and procurement teams evaluating the gateway's suitability for regulated AI deployments.

Walacor Gateway is an ASGI audit and governance proxy that sits between callers and LLM providers. It provides cryptographic record-keeping, content analysis, policy enforcement, budget controls, and real-time observability without requiring changes to the upstream model or the downstream application.

---

## EU AI Act Compliance

### Article 9 — Risk Management System

| | |
|---|---|
| **Requirement summary** | High-risk AI systems must implement a risk management system that identifies, analyzes, and mitigates foreseeable risks throughout the system lifecycle. The system must include appropriate testing and risk mitigation measures. |
| **Gateway capability** | The gateway implements a multi-layer content analysis pipeline that runs on every request and response. Three analyzers operate in sequence: a PII detector (`walacor.pii.v1`) that flags personal data exposure, a toxicity classifier (`walacor.toxicity.v1`) with configurable deny-list terms, and an optional Llama Guard 3 safety classifier covering 14 harm categories (S1 through S14, with S4 child safety mapped to BLOCK and all others to WARN). A policy engine evaluates pre-inference and post-inference rules against request context, attestation status, and content analysis results. Enforcement can be set to `enforced` (block non-compliant requests) or `audit_only` (log without blocking, enabling shadow deployment for risk assessment before production enforcement). |
| **Configuration reference** | `WALACOR_RESPONSE_POLICY_ENABLED`, `WALACOR_PII_DETECTION_ENABLED`, `WALACOR_TOXICITY_DETECTION_ENABLED`, `WALACOR_TOXICITY_DENY_TERMS`, `WALACOR_LLAMA_GUARD_ENABLED`, `WALACOR_LLAMA_GUARD_MODEL`, `WALACOR_ENFORCEMENT_MODE` |
| **Verification step** | Send a request containing known PII (e.g., a Social Security number pattern) and confirm the execution record includes a `content_analysis` block with the PII finding. Toggle `WALACOR_ENFORCEMENT_MODE=enforced` and confirm that a policy rule referencing `pii_detected` blocks the request with HTTP 403. Review the lineage dashboard at `/lineage/` to verify content analysis verdicts are attached to the execution detail view. |

### Article 12 — Record-Keeping

| | |
|---|---|
| **Requirement summary** | High-risk AI systems must enable automatic logging of events (logs) throughout the system's lifetime, to a degree appropriate to the intended purpose of the system and in compliance with recognized standards. Logs must allow traceability of the system's operation. |
| **Gateway capability** | Every request that reaches the gateway produces an immutable audit trail. A completeness middleware guarantees that every inbound request is recorded as a `gateway_attempt` regardless of outcome (allowed, denied, or errored). Requests that proceed to a provider also produce a full `execution_record` containing the prompt text, response content, model attestation ID, policy version, policy result, user identity, session ID, timestamp, and provider request ID. Execution records within the same session are linked by an ID-pointer chain: each record carries a UUIDv7 `record_id` and a `previous_record_id` that points to the prior turn, forming a tamper-evident sequence. All records are dual-written to both the local WAL (SQLite write-ahead log) and the Walacor backend, which issues a tamper-evident `DH` (data hash) on ingest as the cryptographic checkpoint. A `wal_high_water_mark` setting rejects new requests when undelivered records exceed a threshold, preventing unbounded local accumulation. |
| **Configuration reference** | `WALACOR_COMPLETENESS_ENABLED`, `WALACOR_SESSION_CHAIN_ENABLED`, `WALACOR_WAL_PATH`, `WALACOR_WAL_HIGH_WATER_MARK`, `WALACOR_WAL_MAX_SIZE_GB`, `WALACOR_WAL_MAX_AGE_HOURS` |
| **Verification step** | Send several requests within the same session (using the `X-Session-Id` header) and call `GET /v1/lineage/sessions/{session_id}` to retrieve the timeline. Confirm each record has a `sequence_number` incrementing from zero and a `previous_record_id` matching the prior record's `record_id`. Call `GET /v1/lineage/verify/{session_id}` to run server-side chain verification and confirm all records pass. Inspect the `gateway_attempts` table via `GET /v1/lineage/attempts` and verify that denied or errored requests also appear. |

### Article 14 — Human Oversight

| | |
|---|---|
| **Requirement summary** | High-risk AI systems must be designed to allow effective human oversight, including the ability to correctly interpret the system's output, to decide not to use the system, to intervene in or interrupt the system, and to monitor the system in operation. |
| **Gateway capability** | The embedded control plane (SQLite-backed CRUD at `/v1/control/`) provides full administrative control over the AI system's operation. Operators can manage model attestations (approve, revoke, or suspend models via the attestation lifecycle), create and update policy rules (pre-inference and post-inference conditions with allow/deny actions), and set token budget limits per tenant per period. Every control plane mutation immediately refreshes the in-memory governance caches, so changes take effect on the next request without restart. The lineage dashboard at `/lineage/` provides real-time monitoring: a live throughput chart (requests per second, allowed vs. blocked), session history with drill-down to individual execution records, chain verification, and content analysis results. Setting `WALACOR_ENFORCEMENT_MODE=audit_only` enables shadow deployment where the gateway logs policy violations without blocking, allowing human review before enforcement. |
| **Configuration reference** | `WALACOR_CONTROL_PLANE_ENABLED`, `WALACOR_CONTROL_PLANE_DB_PATH`, `WALACOR_LINEAGE_ENABLED`, `WALACOR_ENFORCEMENT_MODE` |
| **Verification step** | Open the lineage dashboard at `/lineage/` and confirm you can browse sessions, view execution details, and verify chains. Navigate to the Control tab and create a policy that blocks a specific model. Send a request to that model and confirm it returns HTTP 403. Revoke the model's attestation via the Models sub-view and confirm subsequent requests are denied. Set `WALACOR_ENFORCEMENT_MODE=audit_only`, send a request that would be denied, and confirm the request is allowed but the execution record shows the policy violation in its metadata. |

### Article 15 — Accuracy, Robustness, and Cybersecurity

| | |
|---|---|
| **Requirement summary** | High-risk AI systems must be designed to achieve an appropriate level of accuracy, robustness, and cybersecurity, and to perform consistently in those respects throughout their lifecycle. Security measures must be proportionate to the risks. |
| **Gateway capability** | Session chains provide tamper evidence for the entire audit trail. Each session uses an ID-pointer chain: every record carries a UUIDv7 `record_id` (time-ordered) and a `previous_record_id` that points to the prior turn. The genesis record has `previous_record_id = null` and `sequence_number = 0`; each subsequent record chains from its predecessor. The chain verification API (`GET /v1/lineage/verify/{session_id}`) walks the linkage server-side and reports any breaks. Walacor's backend issues a tamper-evident `DH` (data hash) on ingest, providing the independent cryptographic checkpoint. For access control, the gateway supports API key authentication via the `WALACOR_GATEWAY_API_KEYS` configuration. Caller identity can be resolved from request headers (`X-User-Id`, `X-User-Roles`, `X-Team-Id`), enabling integration with upstream identity providers and API gateways that perform JWT/SSO validation. The gateway records the resolved identity in every execution record for attribution. Tool input and output data are sent in full to the Walacor backend, which hashes them on ingest, providing integrity verification for MCP and built-in tool interactions. |
| **Configuration reference** | `WALACOR_SESSION_CHAIN_ENABLED`, `WALACOR_GATEWAY_API_KEYS`, `WALACOR_SESSION_CHAIN_TTL`, `WALACOR_SESSION_CHAIN_MAX_SESSIONS` |
| **Verification step** | Call `GET /v1/lineage/verify/{session_id}` for a session with multiple records and confirm the response includes `"valid": true` with each record's hash matching the recomputed value. Open the lineage dashboard and use the client-side "Verify Chain" feature to independently confirm chain integrity. Attempt a request without a valid API key (when `WALACOR_GATEWAY_API_KEYS` is configured) and confirm the gateway returns HTTP 401. Review tool event records in an execution detail view and confirm that `input_hash` and `output_hash` fields are present and populated. |

### Article 61 — Post-Market Monitoring

| | |
|---|---|
| **Requirement summary** | Providers must establish a post-market monitoring system proportionate to the nature of the AI system and the risks, to continuously evaluate compliance and detect anomalies or incidents over the system's operational lifetime. |
| **Gateway capability** | The gateway provides three tiers of operational monitoring. First, a Prometheus-compatible `/metrics` endpoint exposes request counts (total, allowed, denied by reason), token usage, latency histograms, and active session counts. Second, the lineage dashboard polls `/metrics` every 3 seconds to render a live throughput chart with requests per second, allowed rate, and blocked rate over a rolling 3-minute window, plus live counters for tokens per second and total requests. Third, optional OpenTelemetry export sends per-request spans with GenAI semantic conventions (model, provider, token usage) and Walacor-specific attributes (execution_id, attestation_id, policy_result) to any OTLP-compatible backend (Jaeger, Datadog, Grafana Tempo, Honeycomb). The lineage dashboard provides historical drill-down into sessions, execution records, and content analysis results for incident investigation. |
| **Configuration reference** | `WALACOR_METRICS_ENABLED`, `WALACOR_OTEL_ENABLED`, `WALACOR_OTEL_ENDPOINT`, `WALACOR_OTEL_SERVICE_NAME`, `WALACOR_LINEAGE_ENABLED` |
| **Verification step** | Curl `GET /metrics` and confirm Prometheus text format output includes `gateway_requests_total`, `gateway_tokens_total`, and latency histogram metrics. Open the lineage dashboard and confirm the throughput chart on the Overview page animates with live data. If OTel is enabled, confirm spans appear in your tracing backend with `gen_ai.system`, `gen_ai.request.model`, and `walacor.execution_id` attributes. Browse the session list in the dashboard and confirm historical sessions are queryable with full execution detail. |

---

## NIST AI Risk Management Framework (AI RMF)

The NIST AI RMF organizes AI risk management into four functions: Govern, Map, Measure, and Manage. The following table maps each function to Walacor Gateway capabilities.

### GOVERN — Establish and maintain governance structures

| Category | Gateway Capability | Key Configuration |
|---|---|---|
| Governance policies | Embedded control plane provides CRUD for policy rules with pre-inference and post-inference conditions. Policies can reference model ID, provider, attestation status, tenant, prompt text, PII detection results, and toxicity scores. | `WALACOR_CONTROL_PLANE_ENABLED` |
| Attestation management | Model attestation lifecycle (active, revoked, suspended) managed via control plane API. Auto-attestation for unmanaged deployments; explicit attestation required when control plane is active. | `WALACOR_CONTROL_PLANE_ENABLED`, `WALACOR_CONTROL_PLANE_DB_PATH` |
| Access control | API key authentication for gateway callers. Caller identity resolution from request headers enables integration with enterprise identity providers. Control plane API requires authentication. | `WALACOR_GATEWAY_API_KEYS` |
| Audit trail | Dual-write to local WAL and Walacor backend ensures no record loss. Completeness invariant guarantees every request is tracked. Lineage dashboard provides browsable history. | `WALACOR_COMPLETENESS_ENABLED`, `WALACOR_LINEAGE_ENABLED` |

### MAP — Identify and categorize AI risks

| Category | Gateway Capability | Key Configuration |
|---|---|---|
| Model routing | Model routing table maps model name patterns (fnmatch) to providers, URLs, and API keys. Path-based routing provides fallback. All routing decisions are recorded in execution records. | `WALACOR_MODEL_ROUTING_JSON` |
| Provider adapters | Dedicated adapters for OpenAI, Anthropic, HuggingFace, and Ollama normalize request/response formats. A generic adapter with auto-detection handles additional providers. Each adapter extracts model ID, prompt text, response content, and token usage for audit. | `WALACOR_GATEWAY_PROVIDER`, provider URL and key variables |
| Model capability registry | Automatic runtime discovery of model capabilities (e.g., tool/function calling support). Models that reject tool calls are flagged and subsequent requests skip tool injection. Registry exposed via `/health` endpoint. | No configuration required (automatic) |

### MEASURE — Assess, analyze, and monitor AI risks

| Category | Gateway Capability | Key Configuration |
|---|---|---|
| Content analysis | PII detector flags personal data patterns. Toxicity classifier with configurable deny-list terms. Llama Guard 3 classifier covers 14 safety categories. Analyzers run on prompts, responses, and tool outputs. | `WALACOR_PII_DETECTION_ENABLED`, `WALACOR_TOXICITY_DETECTION_ENABLED`, `WALACOR_LLAMA_GUARD_ENABLED` |
| Token budget tracking | Per-tenant token budget enforcement on daily or monthly periods. Budget usage tracked across requests. Budget exceeded condition blocks new requests (enforced mode) or logs warnings (audit_only mode). Redis backend available for multi-replica deployments. | `WALACOR_TOKEN_BUDGET_ENABLED`, `WALACOR_TOKEN_BUDGET_PERIOD`, `WALACOR_TOKEN_BUDGET_MAX_TOKENS` |
| Prometheus metrics | Request counts by disposition (allowed, denied_attestation, denied_policy, denied_budget, error), token usage counters, latency histograms, active session gauges. Scrapable by any Prometheus-compatible system. | `WALACOR_METRICS_ENABLED` |
| Distributed tracing | OpenTelemetry spans with GenAI semantic conventions and Walacor-specific attributes. Compatible with Jaeger, Datadog, Grafana Tempo, Honeycomb, and other OTLP backends. | `WALACOR_OTEL_ENABLED`, `WALACOR_OTEL_ENDPOINT` |

### MANAGE — Prioritize and act on AI risks

| Category | Gateway Capability | Key Configuration |
|---|---|---|
| Budget enforcement | When token budget is exhausted, requests are denied (enforced mode) with HTTP 429. Budget limits can be created, updated, or removed via the control plane API with immediate effect. | `WALACOR_TOKEN_BUDGET_ENABLED`, `WALACOR_TOKEN_BUDGET_MAX_TOKENS` |
| Policy deny/allow | Policy rules define conditions and actions (allow or deny). Denied requests are blocked before reaching the provider (pre-inference) or after content analysis (post-inference). Policy version is recorded in every execution record. | `WALACOR_CONTROL_PLANE_ENABLED` |
| Model revocation | Revoking a model's attestation via the control plane immediately blocks all requests to that model. No restart required; cache refresh is synchronous with the mutation. | `WALACOR_CONTROL_PLANE_ENABLED` |
| Fail-closed safety | When policy cache staleness exceeds the configured threshold and the control plane is unreachable, the gateway returns HTTP 503 rather than allowing unvalidated requests. A local sync loop keeps the policy cache fresh when using the embedded control plane. | `WALACOR_POLICY_STALENESS_THRESHOLD`, `WALACOR_SYNC_INTERVAL` |

---

## SOC 2 Trust Service Criteria

### CC6 — Logical and Physical Access Controls

| Criterion | Gateway Capability | Configuration |
|---|---|---|
| CC6.1 — Logical access security | API key authentication: callers must present a valid key in the `Authorization` or `X-API-Key` header when `WALACOR_GATEWAY_API_KEYS` is configured. Requests without a valid key receive HTTP 401. The control plane API at `/v1/control/` requires API key authentication for all operations. | `WALACOR_GATEWAY_API_KEYS` |
| CC6.2 — Credentials management | API keys are provided via environment variable, not stored in application code. Provider API keys (OpenAI, Anthropic, etc.) are similarly configured via environment. Redis URL passwords are redacted in log output. | `WALACOR_GATEWAY_API_KEYS`, provider key variables |
| CC6.3 — Identity resolution | Caller identity is resolved from request headers (`X-User-Id`, `X-User-Roles`, `X-Team-Id`) and recorded in every execution record. This integrates with upstream API gateways or identity proxies that handle JWT/SSO validation. The identity source (header or JWT) is tagged in the record. | Identity headers set by upstream infrastructure |
| CC6.6 — Access restriction to system interfaces | The gateway exposes a single port (default 8000). Health and metrics endpoints are unauthenticated for infrastructure probes. Lineage endpoints are read-only and bypass authentication by design. All proxy and control plane routes require authentication when API keys are configured. | `WALACOR_GATEWAY_PORT`, `WALACOR_GATEWAY_API_KEYS` |

### CC7 — System Operations

| Criterion | Gateway Capability | Configuration |
|---|---|---|
| CC7.1 — Detection of anomalies | Prometheus `/metrics` endpoint provides real-time counters for denied requests (by reason: attestation, policy, budget, auth), error rates, and latency. Live throughput chart in the lineage dashboard surfaces anomalies visually. OTel spans enable distributed trace analysis. | `WALACOR_METRICS_ENABLED`, `WALACOR_OTEL_ENABLED` |
| CC7.2 — Monitoring of system components | `/health` endpoint reports gateway status, provider connectivity, session chain state, budget tracker state, model capability registry, and control plane status. Suitable for Kubernetes liveness/readiness probes. | Always enabled |
| CC7.3 — Evaluation of system changes | Completeness invariant ensures every request is tracked with a disposition (allowed, denied, or error). WAL high-water mark rejects requests when local storage is at capacity, preventing silent data loss. Attempt records include timestamps, model IDs, and disposition for change-impact analysis. | `WALACOR_COMPLETENESS_ENABLED`, `WALACOR_WAL_HIGH_WATER_MARK` |

### CC8 — Change Management

| Criterion | Gateway Capability | Configuration |
|---|---|---|
| CC8.1 — Infrastructure and software changes | Control plane CRUD operations (create, update, delete attestations, policies, and budgets) immediately refresh in-memory caches. No gateway restart is required for governance changes. Policy version is incremented and recorded in every execution record, providing a full change audit trail. | `WALACOR_CONTROL_PLANE_ENABLED` |
| CC8.2 — Authorization of changes | Control plane API requires API key authentication. Only authenticated operators can modify attestations, policies, or budgets. Changes are persisted in SQLite with WAL mode for durability. The sync contract endpoints (`/v1/attestation-proofs`, `/v1/policies`) also require authentication for fleet security. | `WALACOR_GATEWAY_API_KEYS` |
| CC8.3 — Testing of changes | `enforcement_mode=audit_only` enables shadow deployment where policy changes can be evaluated in production traffic without blocking requests. Content analysis results and policy decisions are logged in execution records, allowing comparison of proposed policy behavior against actual traffic. | `WALACOR_ENFORCEMENT_MODE` |

---

## Appendix: Configuration Quick Reference

All environment variables use the `WALACOR_` prefix. Defaults shown are for a standard deployment.

### Authentication

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_GATEWAY_API_KEYS` | Comma-separated API keys for caller authentication | (empty, no auth) | CC6.1, Art. 15 access control |
| `WALACOR_CONTROL_PLANE_API_KEY` | API key for gateway-to-control-plane communication | (empty) | CC6.2 credential management |

### Governance

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_SKIP_GOVERNANCE` | When true, run as transparent proxy (audit only, no chain/policy/budget) | `false` | Core governance toggle |
| `WALACOR_ENFORCEMENT_MODE` | `enforced` blocks non-compliant requests; `audit_only` logs without blocking | `enforced` | Art. 9 risk management, Art. 14 human oversight |
| `WALACOR_GATEWAY_TENANT_ID` | Tenant identifier for multi-tenant deployments | (empty) | Record attribution |
| `WALACOR_GATEWAY_PROVIDER` | Default provider for path-based routing | `openai` | Art. 12 record context |
| `WALACOR_POLICY_STALENESS_THRESHOLD` | Seconds before stale policy cache triggers fail-closed | `900` | MANAGE fail-closed safety |
| `WALACOR_SYNC_INTERVAL` | Seconds between policy sync cycles | `60` | GOVERN policy freshness |

### Content Analysis

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_RESPONSE_POLICY_ENABLED` | Enable post-inference content analysis pipeline | `true` | Art. 9 risk management |
| `WALACOR_PII_DETECTION_ENABLED` | Enable built-in PII detector | `true` | Art. 9, MEASURE content analysis |
| `WALACOR_TOXICITY_DETECTION_ENABLED` | Enable built-in toxicity classifier | `false` | Art. 9, MEASURE content analysis |
| `WALACOR_TOXICITY_DENY_TERMS` | Comma-separated additional deny-list terms | (empty) | Customizable risk thresholds |
| `WALACOR_LLAMA_GUARD_ENABLED` | Enable Llama Guard 3 safety classifier (14 categories) | `false` | Art. 9, MEASURE content analysis |
| `WALACOR_LLAMA_GUARD_MODEL` | Ollama model name for Llama Guard inference | `llama-guard3` | Llama Guard configuration |
| `WALACOR_LLAMA_GUARD_TIMEOUT_MS` | Llama Guard inference timeout in milliseconds | `5000` | Fail-open latency budget |

### Token Budget

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_TOKEN_BUDGET_ENABLED` | Enable per-tenant token budget enforcement | `false` | MANAGE budget enforcement |
| `WALACOR_TOKEN_BUDGET_PERIOD` | Budget reset period: `daily` or `monthly` | `monthly` | Budget cycle definition |
| `WALACOR_TOKEN_BUDGET_MAX_TOKENS` | Maximum tokens per period per tenant (0 = unlimited) | `0` | MANAGE cost control |

### Session Chain

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_SESSION_CHAIN_ENABLED` | Enable ID-pointer chain (record_id + previous_record_id) for session records | `true` | Art. 12, Art. 15 integrity |
| `WALACOR_SESSION_CHAIN_MAX_SESSIONS` | Maximum concurrent sessions tracked in memory | `10000` | Capacity planning |
| `WALACOR_SESSION_CHAIN_TTL` | Session state TTL in seconds (evict inactive sessions) | `3600` | Memory management |

### Observability

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_METRICS_ENABLED` | Enable Prometheus `/metrics` endpoint | `true` | Art. 61, CC7.1 monitoring |
| `WALACOR_OTEL_ENABLED` | Enable OpenTelemetry span export | `false` | Art. 61, MEASURE tracing |
| `WALACOR_OTEL_ENDPOINT` | OTLP gRPC endpoint for trace export | `http://localhost:4317` | OTel backend destination |
| `WALACOR_OTEL_SERVICE_NAME` | OTel `service.name` resource attribute | `walacor-gateway` | Service identification |
| `WALACOR_LOG_LEVEL` | Application logging level | `INFO` | Operational visibility |

### Control Plane

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_CONTROL_PLANE_ENABLED` | Enable embedded SQLite control plane | `true` | GOVERN, Art. 14 oversight |
| `WALACOR_CONTROL_PLANE_DB_PATH` | SQLite path for control plane state | (alongside WAL db) | Data storage location |
| `WALACOR_CONTROL_PLANE_URL` | Remote control plane URL (fleet sync) | (empty) | Multi-gateway fleet management |

### Lineage

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_LINEAGE_ENABLED` | Enable `/lineage/` dashboard and `/v1/lineage/*` API | `true` | Art. 12, Art. 14, Art. 61 |
| `WALACOR_COMPLETENESS_ENABLED` | Enable gateway_attempts completeness tracking | `true` | Art. 12 completeness invariant |
| `WALACOR_ATTEMPTS_RETENTION_HOURS` | Retention for attempt records in hours | `168` (7 days) | Data retention policy |

### WAL and Storage

| Variable | Purpose | Default | Compliance Relevance |
|---|---|---|---|
| `WALACOR_WAL_PATH` | Local WAL storage directory | `/var/walacor/wal` | Art. 12 durability |
| `WALACOR_WAL_HIGH_WATER_MARK` | Maximum undelivered records before rejecting new requests | `10000` | CC7.3 data loss prevention |
| `WALACOR_WAL_MAX_SIZE_GB` | Maximum WAL disk usage in GB | `10.0` | Storage capacity |
| `WALACOR_WAL_MAX_AGE_HOURS` | Maximum WAL record age in hours | `72.0` | Data lifecycle |
| `WALACOR_REDIS_URL` | Redis URL for multi-replica state sharing | (empty, in-memory) | Multi-replica deployments |

---

*Document version: 2026-03-04. Generated from Walacor Gateway source configuration and architecture.*
