# Gateway Readiness & Configuration-Drift Detection

| | |
|---|---|
| **Date** | 2026-04-21 |
| **Author** | Dharm + Claude |
| **Status** | Proposed — pending approval to start Phase 1 |
| **Target branch** | `feature/phase26-readiness` (new) |
| **Estimated effort** | ~4 engineer-days end-to-end; ~2 days for Phase 1+2 which delivers most of the value |

---

## 1. Executive summary

The gateway has had four silent-misconfiguration incidents in the last
quarter. In every one the gateway booted cleanly, returned `status: healthy`
from `/health`, and continued to operate — but it wasn't actually doing the
job its config claimed. A user watching dashboards had no way to tell.

This plan adds a **readiness self-check system** that continuously probes
the end-to-end behaviour of every governance dependency (signing, anchoring,
auth enforcement, audit completeness, …) and surfaces drift the moment it
appears, via a machine-readable endpoint and a human-readable dashboard
panel. Plus one targeted auth fix that emerged from the audit: the lineage
read endpoints are currently bypassing the API-key middleware and exposing
prompts/responses to any LAN reader.

Explicitly **not** building: a first-install wizard. Our problem is drift
at day 47, not a bad day 1.

## 2. The incidents this must catch

| # | Incident | Config said | Code actually did | Detected when |
|---|---|---|---|---|
| 1 | Signing-key drift | `record_signing_key_path` set | Nobody called `load_signing_key()`; every record's `record_signature` was null for months | Manual audit (this session) |
| 2 | OpenWebUI secret rotation | `WEBUI_SECRET_KEY` env var set | Container rotated `.webui_secret_key` on disk every restart; all sessions invalidated | Users complained about repeated logouts |
| 3 | `Verify Chain` tautology | Dashboard showed "Chain Valid" | `verify_chain` compared null-to-null on records with no `record_id`/`sequence_number`/`signature`; always passed | Manual audit (this session) |
| 4 | Lineage auth hole | `WALACOR_GATEWAY_API_KEYS` set | `api_key_middleware` explicitly skipped `/v1/lineage/*`; full prompts/responses readable unauthed on LAN (`src/gateway/main.py:184`) | Manual audit (this session) |

**Shared shape**: config looks right → code path is either unreachable or
fail-opens → no surface anywhere says "X is configured but X is not
happening." Each incident is a different check this system must perform.

## 3. Why not a first-install wizard

Evaluated and rejected — it solves the wrong problem. Our failures are
drift, not install:

| | First-install wizard | Continuous readiness |
|---|---|---|
| Catches incident #1 (signing never wired up) | ❌ Config was fine | ✅ "Records aren't being signed" |
| Catches incident #2 (secret rotates on restart) | ❌ Initial setup was fine | ✅ "Secret-key file reset since last boot" |
| Catches incident #3 (verify tautology) | ❌ Not a setup issue | ✅ "Verify Chain checking only null fields" |
| Catches incident #4 (lineage unauthed) | ⚠️ Maybe (if wizard prompts for it) | ✅ "SEC-02 red: `/v1/lineage/*` returns 200 without key" |
| Works in k8s / multi-replica | ❌ Hard — install-state races | ✅ Stateless endpoint |
| Works in CI / headless deploys | ❌ Needs a bypass flag | ✅ No-op in CI, still useful |
| Ongoing value after day 1 | ❌ Run once, forget | ✅ Runs on every dashboard open |

A bootstrap flow also introduces install-state (who consumed the token, how
do replicas coordinate) that we'd have to own forever. The readiness design
carries zero install-state.

## 4. Design

### 4.1 Two artifacts + one hardening

1. **`GET /v1/readiness`** — programmatic self-check endpoint. Runs all
   registered checks, returns per-check status + summary. Auth-gated
   (same API key as `/v1/control/*`).
2. **Dashboard `Control > Readiness` sub-tab** — renders the endpoint
   output with colored pills, auto-refreshes every 60s.
3. **Close `/v1/lineage/*` auth hole** — remove the blanket skip at
   `main.py:184`. Gated by a new config flag (`lineage_auth_required`,
   default `true`) so deliberately-open dev deployments stay working.

### 4.2 Relationship to existing `/health`

This is the first question any reviewer will ask, so answering it upfront.
`/health` and `/v1/readiness` are different things and both stay:

| | `/health` | `/v1/readiness` |
|---|---|---|
| Purpose | Kubernetes liveness — "is the process responsive?" | Governance correctness — "is the process actually doing the job?" |
| Cost | Cheap: in-memory counters only | Expensive: probes external services, samples WAL, verifies signatures |
| Cadence | Every 3-10s (k8s default) | Every 60s from the dashboard; on-demand from ops |
| Auth | None (must work for k8s) | Required (reveals configuration state) |
| Side effects | None | Writes an audit record when any check flips to red (§4.6) |
| Authority | Says "alive" | Says "working as configured" |

If `/health` drifts red, the pod dies and k8s restarts it. If
`/v1/readiness` drifts red, humans get paged.

### 4.3 Check contract

Every check implements the same three-method protocol (`Protocol`, not
`ABC`, to stay duck-typed and test-friendly):

```python
class ReadinessCheck(Protocol):
    id: str                     # "INT-02"
    name: str                   # "Signing active"
    category: Category          # security | integrity | persistence | dependency | feature | hygiene
    severity: Severity          # sec | int | ops | warn

    async def run(self, ctx: PipelineContext) -> CheckResult: ...
```

`CheckResult` is a frozen dataclass:

```python
@dataclass(frozen=True)
class CheckResult:
    status: Literal["green", "amber", "red"]
    detail: str                      # one-line human summary ("48/50 recent records signed")
    remediation: str | None = None   # one-line fix hint ("key loaded but orchestrator not signing — check _apply_session_chain")
    evidence: dict[str, Any] = field(default_factory=dict)  # structured data backing the status
    elapsed_ms: int = 0
```

New check = one new class in `readiness/checks/<category>.py` + one line
in the registry. No framework touches. This is a hard requirement because
checks will accrete over time; the per-check cost must be near-zero.

### 4.4 Runner

```python
async def run_all(ctx, *, timeout_s: float = 5.0) -> ReadinessReport:
    # All checks run concurrently via asyncio.gather(return_exceptions=True).
    # Each check is individually bounded by asyncio.wait_for(timeout_s).
    # A timed-out or exception-raising check becomes an AMBER result with
    #   detail="check timed out after 5s" or detail="internal error: …"
    # This means a single bad check cannot DoS the readiness endpoint.
```

Checks never block each other and never block the endpoint. 5s per-check
is the hard ceiling. A check that needs more time than that needs to be
split or made asynchronous (write-and-read instead of write-then-read).

### 4.5 Endpoint behaviour

- **Caching**: singleflight + 15s TTL. Same *pattern* as `_cached_analytics`
  in `lineage/api.py:40` (that module's own TTL is 2.5s tuned for the 3s
  dashboard poll; readiness runs heavier probes so 15s is a deliberate
  longer ceiling). `?fresh=1` query param bypasses the cache.
- **Auth**: requires `X-API-Key` like `/v1/control/*`. Registered in
  `main.py` routes, NOT in the lineage/compliance skip list.
- **Shape** (stable, additive-only):

```jsonc
{
  "status": "ready",              // ready | degraded | unready
  "generated_at": "2026-04-21T20:45:00Z",
  "cache_age_s": 7,
  "gateway_id": "prod-gw-1",
  "summary": { "green": 26, "amber": 3, "red": 2, "total": 31 },
  "checks": [
    {
      "id": "INT-02",
      "name": "Signing active",
      "category": "integrity",
      "severity": "int",
      "status": "red",
      "detail": "0/50 recent records signed",
      "remediation": "Ed25519 key loaded but records lack record_signature — check orchestrator._apply_session_chain",
      "evidence": { "sampled": 50, "signed": 0, "window": "last 50 execution records" },
      "elapsed_ms": 42
    }
    // …
  ]
}
```

- **Rollup rules**:
  - `status=unready` iff any check with `severity ∈ {sec, int}` is `red`.
  - `status=degraded` iff any check is `red` or `amber` but no sec/int is red.
  - `status=ready` iff all checks green, or only `warn` amber.

### 4.6 Drift-to-audit hook

When `run_all` observes a previously-green check flip to `red` with
`severity ∈ {sec, int}`, the runner writes an attempt record with
`disposition="readiness_degraded"`, `metadata={"check_id": "INT-02",
"detail": "...", "previous_status": "green"}`. The audit trail records
when the gateway noticed its own drift. This is the one place readiness
mutates state.

Rate-limited to once per check-id per 5 minutes so a flapping check can't
flood the WAL.

## 5. Check catalog

31 checks in six categories. Every row has a concrete probe and a concrete
green criterion — nothing that's just "config is set."

### 5.1 Security (7)

| ID | Name | Probe | Green when | Severity |
|---|---|---|---|---|
| SEC-01 | API key enforced | read `settings.api_keys_list` | non-empty (auto-generated `wgk-*` counts as amber, see OPS-04) | sec |
| SEC-02 | Lineage auth active | in-process ASGI GET `/v1/lineage/sessions` without `X-API-Key` | returns 401 when `api_keys_list` non-empty | sec |
| SEC-03 | JWT issuer & audience set | config | when `auth_mode ∈ {jwt, both}`: both `jwt_issuer` and `jwt_audience` non-empty | sec |
| SEC-04 | JWT key material present | config | when `auth_mode ∈ {jwt, both}`: `jwt_secret` ≥32 chars **or** `jwt_jwks_url` non-empty | sec |
| SEC-05 | JWKS reachable | `httpx.get(jwt_jwks_url)` (5s timeout) | 200 + parseable JWKS | sec |
| SEC-06 | Enforcement mode | config | `enforcement_mode == "enforced"`; `audit_only` / `skip_governance=true` → amber | sec |
| SEC-07 | Tenant ID set | config | `gateway_tenant_id` non-empty. In prod-heuristic envs: red; in dev: warn | warn |

### 5.2 Integrity (7) — *these look at output, not config*

| ID | Name | Probe | Green when | Severity |
|---|---|---|---|---|
| INT-01 | Signing key loaded | `signing.signing_key_available()` | True | int |
| INT-02 | Signing active | WAL sample of last 50 execution records | ≥95% have non-null `record_signature` | int |
| INT-03 | Signatures verify | Run `verify_canonical` over last 20 signed records | all verify | int |
| INT-04 | Walacor anchoring active | WAL sample of last 50 execution records | when `walacor_storage_enabled`: ≥95% have non-null `walacor_block_id` | int |
| INT-05 | Anchor round-trip | Re-query Walacor by EId for a random recent record, compare BlockId/TransId/DH | all match | int |
| INT-06 | Chain continuity | Pick most-recent multi-record session, call `verify_chain` | `errors == []` | int |
| INT-07 | Attempt completeness | SQL join `wal_records` (event_type='execution') ⨝ `gateway_attempts` on `execution_id`, restricted to executions older than 30s (avoids race with the async attempt write in `completeness_middleware`'s finally block) | 100% of sampled executions have a matching attempt row | int |

### 5.3 Persistence (5)

| ID | Name | Probe | Green when | Severity |
|---|---|---|---|---|
| PER-01 | WAL writable | write-then-delete a probe file in `wal_path` | succeeds | ops |
| PER-02 | WAL disk headroom | free-bytes / total-bytes on WAL mount | ≥ `disk_min_free_percent` | ops |
| PER-03 | WAL backlog | `wal_writer.pending_count()` | < `wal_high_water_mark * 0.8` (amber at 80%, red at 100%) | ops |
| PER-04 | Control-plane DB writable | open + write-noop on `control_plane_db_path` | succeeds | ops |
| PER-05 | Signing-key file integrity | stat + parse `record_signing_key_path` | exists, mode 0600, loads as Ed25519 PEM | int |

### 5.4 Dependencies (5)

| ID | Name | Probe | Green when | Severity |
|---|---|---|---|---|
| DEP-01 | Walacor auth | `walacor_client.start()` against configured server | succeeds within 5s | ops |
| DEP-02 | Walacor query | `$match → $limit 1` probe on executions ETId | non-error response | ops |
| DEP-03 | Ollama reachable | when any Ollama-dependent feature enabled: `GET {ollama_url}/api/tags` | 200 | ops |
| DEP-04 | Redis reachable | when `redis_url` set: `PING` | PONG within 2s | int |
| DEP-05 | Provider keys present | configured provider keys pass basic shape check (no outbound call — don't burn quota) | warn only | warn |

### 5.5 Feature-coherence (7) — *X is enabled but X's dependency isn't*

Every row is a pattern we've hit or nearly hit.

| ID | Name | Coherence rule | Severity |
|---|---|---|---|
| FEA-01 | Llama Guard | `llama_guard_enabled ⇒ DEP-03 green ∧ model pullable` | ops |
| FEA-02 | Web search | `web_search_enabled ⇒ tool_aware_enabled ∧ registry has web_search tool` | ops |
| FEA-03 | Presidio | `presidio_pii_enabled ⇒ import presidio_analyzer` | ops |
| FEA-04 | Prompt Guard | `prompt_guard_enabled ⇒ HF model loadable` | ops |
| FEA-05 | OTel | `otel_enabled ⇒ opentelemetry importable ∧ otel_endpoint set` | ops |
| FEA-06 | Worker/Redis | `uvicorn_workers > 1 ⇒ redis_url set` (else session chain + budgets desync) | int |
| FEA-07 | Intelligence | `intelligence_enabled ⇒ model registry populated ∧ intelligence DB writable` | ops |

### 5.6 Hygiene (3)

| ID | Name | Green when | Severity |
|---|---|---|---|
| HYG-01 | Log level | `log_level == "INFO"` in prod-heuristic envs | warn |
| HYG-02 | Rate limiting | `rate_limit_enabled == true` | warn |
| HYG-03 | OpenWebUI secret persistence | if co-located: `.webui_secret_key` file present & stable since last reboot | warn |

**Dropped from the initial audit** (40+ items): duplicates across categories,
items with no concrete probe, items requiring speculation about installed
packages. Keeping the bar at "we can actually check this in ≤5s against
running state."

## 6. File layout

```
src/gateway/readiness/
├── __init__.py                  # re-exports run_all, ReadinessReport
├── protocol.py                  # ReadinessCheck Protocol, CheckResult, enums
├── runner.py                    # run_all(), concurrency + timeout handling
├── registry.py                  # _REGISTERED list + register() decorator
├── drift_audit.py               # write drift attempt records (§4.6)
├── api.py                       # Starlette route handler
└── checks/
    ├── __init__.py              # imports each module to populate registry
    ├── security.py              # SEC-01 … SEC-07
    ├── integrity.py             # INT-01 … INT-07
    ├── persistence.py           # PER-01 … PER-05
    ├── dependencies.py          # DEP-01 … DEP-05
    ├── features.py              # FEA-01 … FEA-07
    └── hygiene.py               # HYG-01 … HYG-03
```

Tests mirror the source layout under `tests/unit/readiness/`.

Dashboard:
```
src/gateway/lineage/dashboard/src/
├── views/Readiness.jsx
├── views/Control.jsx            # add new sub-tab
├── styles/readiness.css
└── api.js                       # getReadiness()
```

## 7. Phased rollout

### Phase 1 — scaffold + 6 highest-signal checks (≈1 day)

**What ships:** `/v1/readiness` endpoint returning the stable shape, with
six checks wired in: **SEC-01, SEC-02, INT-01, INT-02, PER-01, DEP-01**.

**Why these six:** each directly corresponds to one of the four past
incidents.

**Success criteria (must all hold):**
- `curl -H "X-API-Key: …" localhost:8000/v1/readiness` returns JSON
  matching the §4.5 shape.
- On current gateway: SEC-02 red, INT-02 red (records unsigned), all
  others green or amber with explanations.
- `tests/unit/readiness/test_runner.py` passes: checks run concurrently,
  timeouts become amber, exceptions become amber with detail, endpoint
  enforces auth.
- Adding a 7th check takes <20 lines of code, no framework changes.

**Rollback**: Single feature flag `readiness_enabled` (default `true`). If
flipped off, route 503s immediately. Zero impact on other subsystems.

### Phase 2 — close `/v1/lineage/*` auth hole (≈½ day)

**What ships:**
- New config: `lineage_auth_required: bool = True`.
- `main.py:184` skip list only exempts `/v1/lineage/` + `/v1/compliance`
  when `lineage_auth_required=False`.
- Dashboard `api.js`: `fetchJSON()` attaches stored control API key to
  lineage calls too (falls back to no-auth when no key stored — preserves
  the "no key configured = open dev mode" behaviour).

**Success criteria:**
- Prod-like config (`api_keys_list` non-empty, `lineage_auth_required=true`):
  `/v1/lineage/sessions` returns 401 without a key, 200 with.
- Dashboard loads sessions/attempts/execution pages cleanly after user
  enters the key in the Control AuthGate.
- Dev config (`api_keys_list` empty): no behaviour change.
- Existing unit tests pass with the new flag default.

**Rollback**: set `WALACOR_LINEAGE_AUTH_REQUIRED=false` without redeploy.

### Phase 3 — remaining 25 checks, in batches (≈2 days)

Each batch is independently mergeable. The endpoint shape is additive.

1. Integrity batch (INT-03 … INT-07) — biggest remaining gap.
2. Security batch (SEC-03 … SEC-07).
3. Persistence (PER-02 … PER-05) + Dependencies (DEP-02 … DEP-05).
4. Feature-coherence (FEA-01 … FEA-07).
5. Hygiene (HYG-01 … HYG-03).

Per-batch success criterion: each new check has a green-path test, a
red-path test, and shows up in the endpoint output.

### Phase 4 — dashboard panel (≈½ day)

New `Readiness.jsx` with:
- Headline chip (big green/amber/red READY / DEGRADED / UNREADY).
- Summary line (`26 green · 3 amber · 2 red`).
- Checks grouped by category, each row = dot + id + name + detail,
  expandable for `remediation` + `evidence`.
- Auto-refresh every 60s with visible "next refresh in N s" countdown.
- Manual "Recheck" button hits endpoint with `?fresh=1`.
- Uses existing Control AuthGate — no separate auth flow.

**Success criteria:** on a misconfigured gateway (e.g. set
`record_signing_key_path=/nonexistent`), the panel surfaces the red INT-01
check within 60s, with a clear remediation line.

### Phase 5 — persist auto-generated bootstrap key (≈2 hours)

When `main.py:1472` auto-generates an API key, persist it to
`{wal_path}/gateway-bootstrap-key.txt` (mode 0600) and reload on next boot.
Idempotent pattern, same as `ensure_signing_key`. SEC-01 amber →
`evidence.bootstrap_key_stable=true` still amber but with honest detail
"using auto-generated key — recommend moving to a secret store"
(not "key rotating on every restart").

## 8. Testing strategy

Testing a drift-detector is harder than testing a normal feature because
the thing under test is itself a test. Approach:

1. **Each check has a green-path and red-path unit test.** Red-path fixtures
   construct a minimal `ctx` with the drift condition present.
2. **Runner tests** cover: concurrent execution, per-check timeout, per-check
   exception, cache TTL, `?fresh=1` bypass.
3. **Endpoint tests** cover: 401 without API key, 200 with, shape stability,
   auth-gated under the same middleware as `/v1/control/*`.
4. **Integration test** — spin up the gateway with a **deliberately** broken
   config (no signing key, lineage auth off, audit_only mode) and assert the
   readiness report lists exactly the expected red/amber items. This is the
   test that would have caught all four past incidents; it must be a
   permanent part of the suite.
5. **Snapshot test for the response shape** — JSON schema in
   `tests/unit/readiness/snapshot.schema.json`. Any change to the shape
   requires a snapshot refresh, forcing explicit review.

## 9. Observability & alerting

- Prometheus gauge per check: `walacor_readiness_check{id, category,
  severity} 0|1|2` (green=2, amber=1, red=0). Allows ops to alert on
  specific IDs without parsing JSON.
- Gauge `walacor_readiness_last_run_timestamp_seconds` — stops alerting
  only if the runner itself stops running.
- Structured log line per check transition ("INT-02 green → red") so the
  log pipeline has the same signal as the audit record.
- Suggested alert rules (separate `alerts.yml`, not shipped with code):
  - Any `severity=sec` check red for >5m → page.
  - Any `severity=int` check red for >15m → page.
  - `status=unready` for >10m → page.
  - `status=degraded` for >1h → ticket.

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Readiness endpoint itself leaks config details | med | med — reveals JWT URL, WAL path, etc. to an attacker who already has the key | Auth-gate behind `X-API-Key`; redact URL passwords; never echo `jwt_secret` or `walacor_password` in `evidence`. |
| A single slow check degrades the endpoint | high (network flakiness) | low (15s cache) | 5s per-check timeout, concurrent execution, timeouts become amber not red. |
| Drift-audit hook floods the WAL | low | med | Rate-limited: one audit record per check-id per 5 minutes. |
| Check writes (e.g. PER-01 write-then-delete) corrupt state | low | high | Write to a namespaced probe filename `./.readiness-probe-<uuid>`; delete even on failure; never touch application files. |
| False-positive flapping on transient network | high | med | Require 2 consecutive runs in the same state before emitting drift-audit. Endpoint response reports the instantaneous state. |
| Readiness itself becomes misconfigured | med | high (silent drift detector) | Meta-check `META-01` that verifies the runner ran within the last 120s; exposed in the summary. (Deferred to Phase 3 or later.) |
| `/v1/lineage/*` auth change breaks existing dashboard sessions | med | low (one-time reload) | Gate behind `lineage_auth_required=true`; dashboard already has the API key in sessionStorage from the Control tab; new code falls back to no-auth when the key isn't set. |

## 11. Decisions (resolving the v1 open questions)

1. **Drift writes an audit record**: YES, with 5-minute rate-limit per
   check-id. Makes drift itself part of the audit trail, which is the
   gateway's one job.
2. **No auto-fix from readiness** except the two idempotent cases we've
   already committed to: `ensure_signing_key` (landed this session) and the
   Phase-5 persisted bootstrap key. Everything else is report-only —
   readiness observes, operators decide.
3. **Per-replica readiness, not aggregated**: aggregation belongs to a
   future fleet console; adding it here bloats scope. Operators can scrape
   all replicas via the Prom gauges.
4. **Production-heuristic** for SEC-07 / HYG-01: gateway is considered
   production when `host` binds to a non-loopback address AND TLS is
   terminated in front. Both checked cheaply at startup. Dev is the default
   amber-not-red posture.

## 12. Success criteria (overall)

- Each of the four incidents in §2 produces a **specific red check**,
  visible in the dashboard within 60s of drift onset.
- First-run on a clean dev laptop: worst case amber, never red. Dev UX
  does not regress.
- Adding a new check = one new file in `checks/<category>.py` + one line
  registration. No framework changes. Average time to add a check < 30
  minutes including test.
- Readiness endpoint 99p latency < 5s on a gateway with 100k WAL records.

## 13. Non-goals (explicit)

- No first-install wizard.
- No forced traffic gating on red — readiness reports, Kubernetes/ops
  decide.
- No replacement for `/health`. Both stay.
- No multi-replica aggregation. Per-replica only.
- No automatic remediation beyond the two already-committed idempotent
  key/file provisioners.

---

**Approval requested:** green-light to start **Phase 1 + Phase 2**
(readiness scaffold, six checks, close the lineage auth hole). Phases 3–5
land iteratively over the following week. Each phase is independently
shippable and independently rollback-able.

---

## Appendix A — Code verification

Every claim in this plan was verified against the actual codebase on
2026-04-21. Nothing was assumed or carried over from the earlier audit
without cross-checking. The table below pins each load-bearing claim to
`file:line` where the evidence lives; if any of these move, that claim
needs re-verification.

### A.1 Lineage auth hole (Phase 2 foundation)

| Claim | File:line | Confirmed |
|---|---|---|
| `api_key_middleware` skips `/lineage/`, `/v1/lineage/`, `/v1/compliance` | `src/gateway/main.py:184` | ✅ |
| Middleware is active on all routes | `src/gateway/main.py:1989` (`app.add_middleware(BaseHTTPMiddleware, dispatch=api_key_middleware)`) | ✅ |

### A.2 Auth configuration

| Claim | File:line | Confirmed |
|---|---|---|
| `auth_mode ∈ {api_key, jwt, both}` | `config.py:35` | ✅ |
| `jwt_secret`, `jwt_jwks_url`, `jwt_issuer`, `jwt_audience` exist | `config.py:36–39` | ✅ |
| `api_keys_list` is a computed property (split comma list) | `config.py:615` | ✅ |
| JWT `both` mode: try JWT then fall back to API key | `main.py:210–214` (and `_try_jwt_auth` at `main.py:114`) | ✅ |
| Auto-generated `wgk-*` API key when control plane enabled without keys | `main.py:1470–1478` | ✅ |
| JWT issuer/audience warnings emitted but non-fatal | `main.py:1463–1466` | ✅ |

### A.3 Signing

| Claim | File:line | Confirmed |
|---|---|---|
| `_apply_session_chain` is the signing site | `pipeline/orchestrator.py:544` | ✅ |
| `sign_canonical` called, `record_signature` set on success | `pipeline/orchestrator.py:570–578` | ✅ |
| `signing_key_available()` exists | `crypto/signing.py:109` (added this session) | ✅ |
| `verify_canonical` exists | `crypto/signing.py:81` | ✅ |
| `ensure_signing_key` auto-provisions with mode 0600 | `crypto/signing.py:120+` (added this session) | ✅ |
| Called at startup | `main.py:1351–1364` (added this session) | ✅ |

### A.4 Integrity probes (signature / anchor / chain)

| Claim | File:line | Confirmed |
|---|---|---|
| `verify_chain` reports per-record `signature`, `anchor`, `structural_ok` | `lineage/reader.py:701+` and `lineage/walacor_reader.py:533+` (rewritten this session) | ✅ |
| Walacor reader does independent round-trip via `query_complex` | `lineage/walacor_reader.py` (`verify_chain` body) | ✅ |
| `wal_records` PRIMARY KEY = `execution_id` | `wal/writer.py:31` | ✅ |
| `gateway_attempts` PRIMARY KEY = `request_id`, has `execution_id` column | `wal/writer.py:44, 51` | ✅ |
| `event_type = 'execution'` filter used everywhere | `lineage/reader.py:48` and `reader.py:325` | ✅ |
| Completeness middleware is installed globally | `main.py:2008` | ✅ |

### A.5 Persistence & resource monitoring

| Claim | File:line | Confirmed |
|---|---|---|
| `wal_writer.pending_count()` exists | `wal/writer.py:551` | ✅ |
| `wal_writer.oldest_pending_seconds()` exists | `wal/writer.py:556` | ✅ |
| `wal_writer.disk_usage_bytes()` exists | `wal/writer.py:573` | ✅ |
| `wal_high_water_mark` config field | `config.py:58` | ✅ |
| `disk_min_free_percent` config field | `config.py:535` | ✅ |
| `disk_degraded_threshold` config field | `config.py:462` | ✅ |
| `wal_path` config field | `config.py:55` | ✅ |
| `control_plane_db_path` config field | `config.py:481` | ✅ |

### A.6 Existing `/health` (for §4.2 comparison)

| Claim | File:line | Confirmed |
|---|---|---|
| Returns `status: healthy | degraded | fail_closed` | `health.py:71–77` | ✅ |
| Already reports WAL pending / disk | `health.py:55–67` | ✅ |
| Already reports attestation/policy cache freshness | `health.py:30–46` | ✅ |
| Uses `attestation_cache.entry_count` | `cache/attestation_cache.py:77` | ✅ |
| Uses `policy_cache.is_stale` / `last_sync` | `cache/policy_cache.py:51–57` | ✅ |
| Does NOT require auth (k8s liveness) | `main.py:184` (included in skip list) | ✅ |

### A.7 Existing patterns reused

| Claim | File:line | Confirmed |
|---|---|---|
| `_cached_analytics` (singleflight + TTL) pattern for the readiness cache | `lineage/api.py:40` | ✅ |
| Walacor query primitive | `walacor/client.py:374` (`query_complex(etid, pipeline)`) | ✅ |
| `walacor_client.start()` for auth probe | `walacor/client.py:102` | ✅ |

### A.8 Claims I deliberately did NOT verify (out of scope)

- Presidio / Prompt Guard / OTel import behaviour (FEA-03/04/05) — these
  are Python import probes that only have value when run in the target
  venv. Will be verified during Phase 3 batch implementation.
- OpenWebUI co-location detection (HYG-03) — deployment-dependent, will
  be validated on the EC2 instance where the original incident occurred.
- Multi-worker Redis coherence (FEA-06) — config-level check; behaviour
  verified by the CLAUDE.md note on `uvicorn_workers`.

### A.9 Facts that changed during verification

The earlier audit draft had two errors that this pass caught and the
plan now reflects correctly:

1. **INT-07 join key was wrong.** The initial draft said "matching
   `gateway_attempts` row with the same `request_id`." The correct join
   key is `execution_id` — `request_id` is the attempts-table primary key
   but `wal_records` keys on `execution_id`. Fixed in §5.2.
2. **`_cached_analytics` TTL.** The pattern is correct to reuse but the
   existing TTL is 2.5s (matched to the 3s dashboard poll), not a general
   template. §4.5 now calls this out explicitly so nobody copies the 2.5s
   value by mistake.
