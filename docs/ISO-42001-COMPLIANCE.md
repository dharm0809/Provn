# Walacor Gateway — ISO/IEC 42001 and NIST AI 600-1 Compliance Matrix

This document maps Walacor Gateway capabilities to the ISO/IEC 42001:2023 Artificial Intelligence Management System (AIMS) standard and the NIST AI 600-1 (Artificial Intelligence Risk Management Framework: Generative AI Profile). It is intended for compliance officers, auditors, and organizations seeking ISO 42001 certification or NIST AI RMF alignment for their AI deployments.

Walacor Gateway is an ASGI audit and governance proxy that sits between callers and LLM providers. It provides cryptographic record-keeping (Walacor backend `DH` checkpoints), content analysis, policy enforcement, budget controls, Ed25519 record signing, and real-time observability without requiring changes to the upstream model or the downstream application.

---

## ISO/IEC 42001:2023 — AI Management System Controls

ISO 42001 defines controls across Annex A (normative) and Annex B (guidance). The following tables map each control to gateway capabilities.

### A.2 — AI Policy

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.2.1 | AI policy establishment | Embedded control plane provides CRUD for policy rules with pre-inference and post-inference conditions. Policies can enforce allow/deny actions based on model ID, provider, attestation status, tenant, prompt content, PII detection, and toxicity scores. `enforcement_mode` toggles between enforced and audit-only. | `src/gateway/control/api.py`, `src/gateway/control/store.py`, config: `WALACOR_CONTROL_PLANE_ENABLED`, `WALACOR_ENFORCEMENT_MODE` | Met |
| A.2.2 | AI policy communication | Policy version is recorded in every execution record. Lineage dashboard displays policy decisions with full drill-down. Control plane status endpoint exposes active policy count and enforcement mode. | `GET /v1/control/status`, `GET /v1/lineage/sessions/{id}` | Met |

### A.3 — Internal Organization

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.3.1 | Roles and responsibilities for AI | Caller identity resolution from headers (`X-User-Id`, `X-User-Roles`, `X-Team-Id`) and JWT claims. Identity recorded in every execution record for attribution. Role-based access patterns supported via policy rules referencing `tenant_id` and caller roles. | `src/gateway/auth/identity.py`, `src/gateway/auth/jwt_auth.py` | Met |
| A.3.2 | AI system inventory | Model attestation registry tracks all approved models with status (active, revoked, suspended), verification level, and provider. Model discovery endpoint scans connected providers (Ollama, OpenAI) for available models. | `GET /v1/control/attestations`, `GET /v1/control/discover` | Met |
| A.3.3 | Reporting AI system issues | Prometheus metrics expose denied requests by reason (attestation, policy, budget, auth), error rates, and EWMA latency anomaly detection per provider. OTel spans enable distributed trace analysis for incident investigation. | `GET /metrics`, `src/gateway/metrics/anomaly.py`, `src/gateway/telemetry/otel.py` | Met |

### A.4 — Resources for AI Systems

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.4.1 | Resource allocation | Token budget enforcement per tenant per period (daily/monthly). Budget CRUD via control plane API with immediate cache refresh. Cost attribution endpoint aggregates spend per user and model with configurable pricing. | `src/gateway/pipeline/budget_tracker.py`, `GET /v1/lineage/cost`, `src/gateway/control/store.py` (model_pricing table) | Met |
| A.4.2 | Competence | JWT/SSO authentication with HS256/RS256/ES256 support. API key authentication. Auth mode configuration (api_key, jwt, both). Caller identity propagated to audit trail. | `src/gateway/auth/jwt_auth.py`, config: `WALACOR_AUTH_MODE` | Met |
| A.4.3 | Awareness | Lineage dashboard provides browsable session history, execution detail, content analysis results, chain verification, and live throughput monitoring. Compliance documentation (EU AI Act, ISO 42001, NIST) available in `docs/`. | `/lineage/`, `docs/EU-AI-ACT-COMPLIANCE.md` | Met |
| A.4.4 | Communication | Policy decisions logged with explanation. Content analysis verdicts attached to execution records. Lineage API provides structured JSON responses for integration with notification systems. | `GET /v1/lineage/sessions`, `GET /v1/lineage/executions/{id}` | Met |

### A.5 — AI System Impact Assessment

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.5.1 | AI impact assessment process | Three-tier content analysis pipeline: PII detection (built-in regex + optional Presidio NER), toxicity classification (deny-list + configurable terms), Llama Guard safety classifier (14 harm categories S1-S14). Streaming content analysis with windowed PII/toxicity checks every 500 chars. | `src/gateway/content/`, `src/gateway/content/presidio_pii.py`, `src/gateway/content/stream_safety.py` | Met |
| A.5.2 | AI impact assessment results | Content analysis results stored in execution records with category, verdict (PASS/WARN/BLOCK), confidence score, and matched entities. Lineage dashboard displays verdicts per execution. | `GET /v1/lineage/executions/{id}`, execution record `content_analysis` field | Met |
| A.5.3 | AI impact assessment review | `enforcement_mode=audit_only` enables shadow deployment where policy violations are logged without blocking. Allows pre-production risk assessment against real traffic. Cost attribution dashboard tracks spend trends for budget impact review. | Config: `WALACOR_ENFORCEMENT_MODE`, `GET /v1/lineage/cost` | Met |

### A.6 — AI System Lifecycle

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.6.1 | Management of AI lifecycle | Model attestation lifecycle (active, revoked, suspended) managed via control plane CRUD. Revocation immediately blocks all requests. Auto-attestation for initial deployment; explicit management when control plane is active. | `src/gateway/control/api.py`, `src/gateway/pipeline/orchestrator.py` (_attestation_check) | Met |
| A.6.2 | AI data management | Dual-write architecture: every execution record written to both local WAL (SQLite) and Walacor backend. WAL high-water mark prevents unbounded accumulation. WAL max size and max age constraints for data lifecycle management. Group commit batching for burst throughput. | `src/gateway/wal/writer.py`, `src/gateway/wal/batch_writer.py`, config: `WALACOR_WAL_HIGH_WATER_MARK`, `WALACOR_WAL_MAX_SIZE_GB` | Met |
| A.6.3 | AI system design and development | Adaptive startup probes validate provider health, routing endpoints, disk space, and API versions at boot. Model capability registry auto-discovers tool support per model. Resource monitor tracks provider error rates with LiteLLM-style cooldown. | `src/gateway/adaptive/startup_probes.py`, `src/gateway/adaptive/capability_registry.py`, `src/gateway/adaptive/resource_monitor.py` | Met |
| A.6.4 | AI system testing | Completeness invariant guarantees every request produces an attempt record. Chain verification API (`GET /v1/lineage/verify/{id}`) recomputes SHA3-512 hashes server-side. Client-side verification via js-sha3 in dashboard. 650+ unit tests covering all subsystems. | `tests/`, `GET /v1/lineage/verify/{id}`, `src/gateway/middleware/completeness.py` | Met |
| A.6.5 | AI system deployment | Docker Compose deployment with profiles (demo, redis, ollama). Helm chart for Kubernetes. `.env.example` with all configuration variables. Startup probes validate environment before accepting traffic. | `deploy/docker-compose.yml`, `deploy/helm/`, `.env.example` | Met |
| A.6.6 | AI system operation and monitoring | Prometheus RED metrics (Rate, Errors, Duration): inflight gauge, status counter by code, event loop lag, per-model latency histogram. EWMA anomaly detection per provider. Live throughput chart in dashboard. OTel multi-span traces (7 pipeline spans). | `src/gateway/metrics/prometheus.py`, `src/gateway/metrics/anomaly.py`, `src/gateway/telemetry/otel.py` | Met |
| A.6.7 | AI system retirement | Model revocation via control plane immediately blocks traffic. Budget deletion removes enforcement. Attestation status transitions (active → suspended → revoked) provide graduated decommissioning. | `src/gateway/control/api.py` (delete/update attestation endpoints) | Met |

### A.7 — Data for AI Systems

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.7.1 | Data quality | PII detection (built-in + Presidio NER) identifies personal data in prompts and responses. Toxicity classifier filters harmful content. Content analysis runs on tool outputs for indirect prompt injection detection. | `src/gateway/content/pii.py`, `src/gateway/content/presidio_pii.py`, `src/gateway/content/toxicity.py` | Met |
| A.7.2 | Data provenance | ID-pointer session chains (`record_id` + `previous_record_id`, UUIDv7) provide tamper-evident record sequences. Walacor's backend issues a `DH` (data hash) on ingest as the cryptographic checkpoint. Ed25519 digital signatures provide non-repudiation. Tool input/output sent in full and hashed by Walacor on ingest. | `src/gateway/pipeline/session_chain.py`, `src/gateway/walacor/`, `src/gateway/crypto/signing.py` | Met |
| A.7.3 | Data preparation | Request classification (task field, user-agent detection, prompt regex) categorizes requests before processing. Thinking token strip separates reasoning from final output. Provider-specific adapters normalize heterogeneous request/response formats. | `src/gateway/adaptive/request_classifier.py`, `src/gateway/adapters/thinking.py` | Met |
| A.7.4 | Data labeling | Content analysis verdicts (PASS/WARN/BLOCK) with category labels and confidence scores attached to every execution record. PII entity types (SSN, credit card, AWS key, email, phone) are explicitly labeled. Llama Guard categories (S1-S14) provide safety classification labels. | Execution record `content_analysis` field | Met |

### A.8 — Information for Interested Parties

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.8.1 | AI system transparency | Lineage dashboard provides full visibility into every request: prompt, response, model, policy decision, content analysis, chain hash, tool events with source links. Governance status card shows auth mode, active analyzers, provider configuration. | `/lineage/`, dashboard Status tab | Met |
| A.8.2 | AI system documentation | Comprehensive documentation: `README.md` (engineer reference), `OVERVIEW.md` (one-page summary), `docs/WIKI-EXECUTIVE.md` (leadership-facing), `docs/FLOW-AND-SOUNDNESS.md` (pipeline flowcharts), `docs/EU-AI-ACT-COMPLIANCE.md` (regulatory mapping), `.env.example` (configuration). | `docs/` directory | Met |
| A.8.3 | Provision of information about AI system decisions | Policy decisions include version, result, and enforcement mode. Content analysis includes per-analyzer verdicts. Budget denial includes remaining quota. Attestation denial includes model status. All recorded in execution records and surfaced via lineage API. | `GET /v1/lineage/executions/{id}` | Met |
| A.8.4 | Communication about AI-related incidents | EWMA anomaly detection flags latency spikes per provider. Prometheus metrics track error rates. OTel spans propagate error status. Health endpoint exposes provider connectivity and model capability state. | `GET /health`, `GET /metrics`, `src/gateway/metrics/anomaly.py` | Met |

### A.9 — Use of AI Systems

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.9.1 | Appropriate use of AI | Model attestation controls which models are approved for use. Policy rules can restrict by model ID, provider, or tenant. Content analysis prevents misuse (S4 child safety → BLOCK). Budget limits prevent excessive consumption. | `src/gateway/control/store.py`, `src/gateway/pipeline/orchestrator.py` | Met |
| A.9.2 | Use of AI system in accordance with policies | Pre-inference policies evaluate before forwarding to provider. Post-inference policies evaluate after content analysis. Both policy stages recorded in execution records. Enforcement mode determines whether violations block or warn. | `src/gateway/pipeline/orchestrator.py` (_pre_inference_policy, _post_inference_policy) | Met |
| A.9.3 | Monitoring of AI system use | Completeness middleware tracks every request attempt. Session-level aggregation in lineage API. Per-user attribution via caller identity. Cost attribution aggregates spend by user and model. Live dashboard monitoring. | `GET /v1/lineage/sessions`, `GET /v1/lineage/cost`, `GET /v1/lineage/attempts` | Met |
| A.9.4 | AI system use records | Execution records include: prompt, response, model ID, provider, session ID, timestamp, policy version/result, content analysis, token usage, latency, estimated cost, caller identity, chain hash, and optional Ed25519 signature. Attempt records track every request regardless of outcome. | `src/gateway/crypto/hasher.py`, `src/gateway/middleware/completeness.py` | Met |

### A.10 — Third-Party and Customer Relationships

| Control | Description | Gateway Feature | Evidence Location | Status |
|---------|-------------|-----------------|-------------------|--------|
| A.10.1 | AI supply chain management | Provider adapters abstract upstream LLM providers (OpenAI, Anthropic, HuggingFace, Ollama). Model routing table maps patterns to specific providers/URLs. Provider health probes validate connectivity at startup. Resource monitor tracks provider error rates with automatic cooldown. | `src/gateway/adapters/`, `src/gateway/adaptive/resource_monitor.py` | Met |
| A.10.2 | Third-party AI components | Model capability registry discovers and caches per-model capabilities (tool support). MCP client integration for external tool providers. Walacor backend ingests every record and issues an independent `DH` checkpoint, providing third-party verifiability without a separate transparency log. | `src/gateway/walacor/`, `src/gateway/mcp/` | Met |
| A.10.3 | Customer obligations | Token budget enforcement per tenant. Policy rules support tenant-scoped conditions. Caller identity attribution to individual users. Cost reporting by user and model. | `src/gateway/pipeline/budget_tracker.py`, `GET /v1/lineage/cost` | Met |

---

## NIST AI 600-1 — Generative AI Profile

NIST AI 600-1 extends the AI RMF with 12 risk categories specific to generative AI systems. The following table maps each risk to gateway capabilities.

### GAI Risks and Mitigations

| Risk ID | Risk Category | Gateway Mitigation | Evidence Location | Status |
|---------|---------------|-------------------|-------------------|--------|
| GAI-1 | CBRN Information | Llama Guard 3 classifier covers S10 (weapons/drugs/regulated substances) with WARN verdict. Policy rules can escalate WARN to BLOCK. Content analysis runs on both prompts and responses. | `src/gateway/content/llama_guard.py` (S10 category) | Met |
| GAI-2 | Confabulation | Execution records store full prompt and response text for human review. Tool-augmented responses include source URLs with Walacor-issued `DH` for provenance verification. Session chains enable temporal sequence analysis. | `src/gateway/pipeline/hasher.py`, tool event `sources` field | Partial |
| GAI-3 | Data Privacy | Three-tier PII detection: built-in regex (SSN, credit card, AWS key, API key), optional Presidio NER (person, email, phone, location), and streaming PII analysis every 500 chars. High-risk PII (credit card, SSN) → BLOCK; low-risk (email, phone) → WARN. | `src/gateway/content/pii.py`, `src/gateway/content/presidio_pii.py`, `src/gateway/content/stream_safety.py` | Met |
| GAI-4 | Environmental Impact | Token usage tracking per request (prompt + completion tokens). Cost attribution aggregates by model and user. Budget limits constrain total consumption per period. Per-model latency histograms identify inefficient models. | `GET /v1/lineage/cost`, `src/gateway/metrics/prometheus.py` (forward_duration_by_model) | Partial |
| GAI-5 | Human-AI Configuration | Caller identity resolution (headers + JWT) attributes every request to a specific user. Enforcement mode toggle (enforced/audit_only) enables human-in-the-loop workflows. Control plane allows real-time model and policy adjustments. | `src/gateway/auth/identity.py`, `src/gateway/control/api.py` | Met |
| GAI-6 | Information Integrity | ID-pointer session chains (`record_id` + `previous_record_id`) provide tamper-evident record sequences. Walacor's backend issues a `DH` (data hash) on ingest as the independent cryptographic checkpoint. Ed25519 record signing for non-repudiation. Completeness invariant ensures no records are silently dropped. | `src/gateway/pipeline/session_chain.py`, `src/gateway/walacor/`, `src/gateway/crypto/signing.py` | Met |
| GAI-7 | Information Security | API key and JWT authentication. Provider API keys stored in environment variables, never in code. Redis URL passwords redacted in logs. Control plane API requires authentication. WAL database uses SQLite WAL mode with `synchronous=NORMAL`. Lineage reader opens database in read-only mode (`?mode=ro`). | `src/gateway/auth/`, `src/gateway/main.py` (api_key_middleware) | Met |
| GAI-8 | Intellectual Property | Execution records store full prompt and response text for IP audit. Tool input/output hashes provide integrity verification. Session-level aggregation enables per-use analysis. Lineage dashboard provides searchable history. | `GET /v1/lineage/sessions`, `GET /v1/lineage/executions/{id}` | Partial |
| GAI-9 | Obscene/Degrading Content | Llama Guard 3 covers S1 (violent crimes), S2 (non-violent crimes), S3 (sex-related crimes), S5 (defamation), S7 (privacy), S11 (sexual content). Toxicity classifier with configurable deny-list terms. S4 (child safety) mapped to BLOCK; others to WARN. Content policies allow per-category BLOCK/WARN/PASS configuration. | `src/gateway/content/llama_guard.py`, `src/gateway/content/toxicity.py`, content_policies table | Met |
| GAI-10 | Value Chain and Component Integration | Provider adapters normalize 5 provider types. MCP client for external tool integration. Built-in tool registry for gateway-native tools (web search). Model capability auto-discovery with caching. Adaptive resource monitor with provider cooldown. | `src/gateway/adapters/`, `src/gateway/mcp/`, `src/gateway/tools/`, `src/gateway/adaptive/` | Met |
| GAI-11 | Harmful Bias and Homogenization | Content analysis results stored per-request for bias auditing. Per-model and per-user attribution enables demographic analysis. Configurable content policies allow per-category enforcement tuning. Multiple analyzer tiers (PII, toxicity, Llama Guard) provide defense in depth. | `src/gateway/content/`, `GET /v1/lineage/cost` (user attribution) | Partial |
| GAI-12 | Dangerous/Violent/Hateful Content | Llama Guard 3 covers S1 (violent crimes), S6 (weapons), S9 (hate). Toxicity deny-list for custom blocked terms. Policy rules can block models or providers that generate harmful content. Post-inference content analysis runs before response delivery. | `src/gateway/content/llama_guard.py`, `src/gateway/content/toxicity.py` | Met |

---

## Gap Analysis Summary

### Fully Met Controls

The gateway fully satisfies 34 of 38 ISO 42001 controls and 8 of 12 NIST AI 600-1 risk categories through:

- **Cryptographic audit trail**: ID-pointer session chains (UUIDv7), Walacor backend-issued `DH` checkpoints, Ed25519 record signing
- **Multi-tier content analysis**: Built-in PII + toxicity, Presidio NER, Llama Guard 3 (14 categories), streaming analysis
- **Policy enforcement**: Pre/post-inference policies, attestation lifecycle, budget limits, enforcement mode toggle
- **Identity and access**: API key + JWT/SSO authentication, caller identity attribution, role-based policy conditions
- **Observability**: Prometheus RED metrics, EWMA anomaly detection, OTel multi-span traces, lineage dashboard
- **Adaptive runtime**: Startup probes, capability auto-discovery, resource monitoring, provider cooldown

### Partial Controls (4 gaps)

| Area | Gap | Recommendation |
|------|-----|----------------|
| GAI-2 Confabulation | Gateway records prompts/responses and tool sources for review, but does not independently verify factual accuracy of model outputs. | Integrate retrieval-augmented generation (RAG) fact-checking or hallucination detection models as a content analyzer. |
| GAI-4 Environmental Impact | Token and cost tracking provide consumption visibility, but gateway does not measure direct energy consumption or carbon emissions per request. | Integrate provider-specific energy/carbon APIs when available (e.g., cloud carbon footprint tools). Map token usage to estimated energy via published model-specific coefficients. |
| GAI-8 Intellectual Property | Full prompt/response recording enables IP audit, but gateway does not perform automated copyright or license compliance checks on model outputs. | Add a content analyzer that checks outputs against known copyrighted material databases or license-restricted content. |
| GAI-11 Harmful Bias | Per-user and per-model attribution supports bias auditing, but gateway does not perform automated fairness or bias measurement on model outputs. | Integrate fairness metrics tooling (e.g., demographic parity analysis on recorded outputs) as a post-hoc analysis pipeline. |

### Controls Outside Gateway Scope

The following ISO 42001 controls relate to organizational management processes rather than technical AI system capabilities. They require organizational policies and procedures that complement the gateway's technical controls:

- **A.2 policy review cycles** — Organization must establish periodic review schedules for AI policies configured in the control plane.
- **A.3 competence assessment** — Organization must verify that personnel operating the gateway and managing policies have appropriate AI governance training.
- **A.4 resource planning** — Organization must budget for infrastructure, monitoring, and incident response beyond what the gateway provides automatically.

---

## Configuration Quick Reference for Compliance

### Critical Controls for ISO 42001 Certification

| Control Area | Required Configuration | Minimum Setting |
|--------------|----------------------|-----------------|
| Policy enforcement | `WALACOR_CONTROL_PLANE_ENABLED=true` | Must be `true` for A.2, A.6, A.9 |
| Content analysis | `WALACOR_PII_DETECTION_ENABLED=true` | Must be `true` for A.5, A.7 |
| Audit trail | `WALACOR_COMPLETENESS_ENABLED=true` | Must be `true` for A.6.4, A.9.4 |
| Session chains | `WALACOR_SESSION_CHAIN_ENABLED=true` | Must be `true` for A.7.2 |
| Authentication | `WALACOR_GATEWAY_API_KEYS` or `WALACOR_AUTH_MODE=jwt` | Must be configured for A.3, A.4 |
| Monitoring | `WALACOR_METRICS_ENABLED=true` | Must be `true` for A.6.6, A.8.4 |
| Lineage | `WALACOR_LINEAGE_ENABLED=true` | Must be `true` for A.8.1, A.9.3 |
| Enforcement | `WALACOR_ENFORCEMENT_MODE=enforced` | Must be `enforced` for production A.9.2 |

### Enhanced Controls for NIST AI 600-1

| Risk Category | Recommended Configuration | Purpose |
|---------------|--------------------------|---------|
| GAI-3 Data Privacy | `WALACOR_PRESIDIO_PII_ENABLED=true` | NER-based PII detection for higher accuracy |
| GAI-6 Information Integrity | `WALACOR_RECORD_SIGNING_ENABLED=true` | Ed25519 non-repudiation |
| GAI-9 Content Safety | `WALACOR_LLAMA_GUARD_ENABLED=true` | 14-category safety classification |
| GAI-12 Content Safety | `WALACOR_TOXICITY_DETECTION_ENABLED=true` | Configurable deny-list enforcement |

---

*Document version: 2026-03-13. Generated from Walacor Gateway source configuration and architecture.*
