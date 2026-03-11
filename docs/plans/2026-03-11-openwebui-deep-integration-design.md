# OpenWebUI Deep Integration Design

**Goal:** Make Gateway + OpenWebUI the strongest governed AI platform by combining Gateway's cryptographic audit trail with OpenWebUI's user-facing experience. Three pillars: governance visibility in chat, enterprise identity/RBAC, and operational intelligence.

**Approach:** Hybrid — Gateway always emits governance metadata in HTTP response headers (works with any UI). An optional OpenWebUI Pipeline plugin reads those headers to render governance badges, budget alerts, and model status directly in the chat UI.

**Tech Stack:** Python Pipeline plugin (OpenWebUI Pipelines framework), Gateway REST endpoints, existing policy engine + budget tracker.

---

## Section 1: Enriched Governance Response Headers

**Existing headers** (already shipped):
- `x-walacor-execution-id` — audit trail link
- `x-walacor-attestation-id` — model attestation ID
- `x-walacor-chain-seq` — sequence number in session chain
- `x-walacor-policy-result` — `pass` / `deny`

**New headers:**

| Header | Value | Source |
|--------|-------|--------|
| `x-walacor-content-analysis` | `clean` / `pii_warn` / `toxicity_warn` / `blocked` | Response evaluator |
| `x-walacor-budget-remaining` | Token count or `-1` (unlimited) | Budget tracker |
| `x-walacor-budget-percent` | `0`–`100` | Computed from budget state |
| `x-walacor-model-id` | Actual model name (e.g. `qwen3:4b`) | call.model_id |

**CORS fix:** Add `Access-Control-Expose-Headers: x-walacor-*` so browsers (OpenWebUI) can read custom headers.

**Streaming:** The existing `build_governance_sse_event` and `governance_meta` dict gain the same 4 new fields.

**Changes:**
- `_add_governance_headers()` in `orchestrator.py` — add 4 new parameters
- Call site at line ~1553 — pass content analysis result, budget state, model_id
- `build_governance_sse_event()` in `forwarder.py` — add same 4 fields to SSE payload
- `_CORS_HEADERS` in `main.py` — add `Access-Control-Expose-Headers`

---

## Section 2: OpenWebUI Identity Enrichment

**Gap:** `resolve_identity_from_headers()` reads `X-User-Id`, `X-Team-Id`, `X-User-Roles`. OpenWebUI sends different header names (`X-OpenWebUI-User-Name`, `X-OpenWebUI-User-Email`, `X-OpenWebUI-User-Role`, `X-OpenWebUI-User-Id`).

**Solution:** Expand fallback chain in `resolve_identity_from_headers()`:

```
user_id:  X-User-Id  →  X-OpenWebUI-User-Name  →  X-OpenWebUI-User-Id
email:    X-User-Email  →  X-OpenWebUI-User-Email
roles:    X-User-Roles  →  X-OpenWebUI-User-Role  (single: "admin"/"user"/"pending")
team:     X-Team-Id  (no OpenWebUI equivalent)
```

Priority: generic headers first (any UI), OpenWebUI-specific as fallback.

**Changes:**
- `src/gateway/auth/identity.py` — expand `resolve_identity_from_headers()` with fallback headers
- Add `email` resolution from headers (currently only populated from JWT)

---

## Section 3: OpenWebUI Pipeline Plugin

**What:** A Python outlet filter (~120 lines) that reads `x-walacor-*` headers and appends a governance footer to each chat message.

**Where:** `plugins/openwebui/governance_pipeline.py` in our repo. User copies into OpenWebUI Pipelines.

**User sees:**
```
─── Walacor Governance ───────────────────────
🔒 Chain #4  ✅ Policy: pass  🛡️ Clean  💰 82% budget remaining
Execution: abc123... | Model: qwen3:4b (attested)
```

**Hooks used:**
- `outlet(body, __user__)` — post-response, appends governance footer
- `inlet(body, __user__)` — pre-request, polls `/v1/openwebui/status` for alerts

**Design:** Standard OpenWebUI Pipeline pattern. No OpenWebUI fork. Optional — without it, headers still flow for API consumers and Lineage Dashboard.

---

## Section 4: Budget & Banner Operational Intelligence

**New endpoint:** `GET /v1/openwebui/status`

```json
{
  "banners": [
    {"type": "warning", "text": "Token budget at 90% — 10,000 tokens remaining"},
    {"type": "error", "text": "Model gpt-4o attestation revoked"}
  ],
  "budget": {
    "percent_used": 90,
    "tokens_remaining": 10000,
    "period": "monthly"
  },
  "models_status": {
    "active": ["qwen3:4b", "gemma3:1b"],
    "revoked": ["gpt-4o"]
  }
}
```

**Banner triggers** (computed from existing Gateway state, no new storage):
- Budget threshold crossed (70%, 90%, 100%) — from `BudgetTracker`
- Model attestation revoked — from `control_store.list_attestations()`
- Content analysis blocks in last hour — from `gateway_attempts` query
- Health degraded (WAL disk threshold) — from `/health` data

**Integration:** Pipeline plugin polls this on `inlet` hook. Prepends system notification if banners exist.

**Where:** New route handler in `src/gateway/control/api.py` or a new `src/gateway/openwebui/status_api.py`.

---

## Section 5: Enterprise RBAC via OpenWebUI Roles

**How:** OpenWebUI's `X-OpenWebUI-User-Role` is captured as `CallerIdentity.roles` (Section 2). Policy engine already evaluates rules against context fields.

**One code change:** Add `caller_role` to the attestation context (`att_ctx`) dict in the orchestrator so pre-inference policy rules can reference it.

**Example policies (configured via control plane API):**

Restrict expensive models to admins:
```json
{
  "name": "restrict-expensive-models-to-admins",
  "rules": [
    {"field": "model_id", "op": "in", "value": ["gpt-4o", "claude-sonnet-4-20250514"]},
    {"field": "caller_role", "op": "equals", "value": "admin"}
  ],
  "action": "allow"
}
```

Block pending users:
```json
{
  "name": "block-pending-users",
  "rules": [
    {"field": "caller_role", "op": "equals", "value": "pending"}
  ],
  "action": "deny"
}
```

**Per-role budgets:** Control plane budget API already supports per-user budgets. OpenWebUI's `X-OpenWebUI-User-Name` is the user key. Configure different limits per user via `POST /v1/control/budgets`.

**Trust model:** Headers are advisory (`source: "header_unverified"`), same as existing identity headers. JWT auth provides verified identity when needed.

---

## Summary of Changes

| Component | File(s) | Effort |
|-----------|---------|--------|
| Enriched headers + CORS | `orchestrator.py`, `forwarder.py`, `main.py` | Small |
| Identity enrichment | `auth/identity.py` | Small |
| `caller_role` in policy context | `orchestrator.py` | Tiny |
| `/v1/openwebui/status` endpoint | New: `openwebui/status_api.py` | Medium |
| Pipeline plugin | New: `plugins/openwebui/governance_pipeline.py` | Medium |
| Tests | New test files for each component | Medium |
| Documentation | Update quickstart + README | Small |

## What We DON'T Do (YAGNI)
- No OpenWebUI fork or custom frontend extension
- No webhook push TO OpenWebUI (their API doesn't support it well)
- No user sync from OpenWebUI (we read headers per-request)
- No replication of OpenWebUI's group/RBAC system
- No Pipeline service deployment automation (user installs it themselves)
