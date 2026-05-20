# Production Hosting Checklist

Run through this before exposing the gateway to real traffic. Items marked
**MUST** block hosting; **SHOULD** are strong recommendations.

Most of these are operator-side: the code is correct, but it ships with
developer-friendly defaults (plaintext listen, no TLS to Walacor, shared
bootstrap key) that you have to harden per-deployment.

---

## Code blockers — fixed in repo, must be DEPLOYED

These are merged but you still need to be on a build that contains them.

- [x] **PR #69** — `/v1/lineage/envelope` timeout, VerdictFlush DLQ,
      schema-field contract guard
      (`src/gateway/walacor/client.py:_EXECUTION_SCHEMA_FIELDS`), WAL size
      default. Confirm commit `520cba5` (`fix(storage): decouple Walacor
      delivery from the request path`) or later is in your deploy.
- [x] **PR #70** — observability auth gate. The endpoints `/v1/readiness`,
      `/v1/connections`, `/metrics` now honour
      `WALACOR_OBSERVABILITY_AUTH_REQUIRED`. Set it to `true` on prod (see
      `src/gateway/config.py`).

---

## MUST-fix — operator-side, not code

### 1. TLS to Walacor backend
`WALACOR_SERVER` must use `https://`. Today's plaintext `http://` (e.g. the
prod-audit-flagged `http://32.196.5.38/api`) leaks every prompt and
response between the gateway and Walacor over the wire. Either terminate
TLS at the Walacor side or front it with a TLS-terminating proxy.

### 2. Secrets out of disk
All credentials in `.env.gateway` (Walacor user/password, provider API
keys, gateway API key, JWT signing keys) must move to AWS SSM Parameter
Store as `SecureString`. The gateway already hydrates from SSM at boot —
see commit `3a67fed` (`feat(secrets): hydrate gateway secrets from SSM
Parameter Store at boot`) and `src/gateway/secrets/` for the loader.

Plaintext `.env.gateway` on the host is the open P0 from the
2026-05-13 prod audit.

### 3. Per-tenant API keys
The auto-generated `wgk-*` bootstrap key
(`{wal_path}/gateway-bootstrap-key.txt`, written by `ensure_bootstrap_key`
in `src/gateway/auth/`) is shared across every caller. Mint per-tenant
keys via the control plane API (`/v1/control/api-keys`) and distribute
those. Keep the bootstrap key as an admin-only fallback; never hand it to
application code.

### 4. WAL size limit
Default is 10 GB (`WALACOR_WAL_MAX_SIZE_GB` — set via PR #69 in
`src/gateway/config.py`). Confirm it matches your disk capacity — a good
rule is ~30–50% of the EBS / EFS volume so you have headroom for
backups, swap, and the lineage SQLite reader caches.

### 5. Observability lockdown
Set `WALACOR_OBSERVABILITY_AUTH_REQUIRED=true`. After PR #70,
`/v1/readiness`, `/v1/connections`, and `/metrics` all require a valid
API key. Leaving these open exposes the model allowlist, attestation
state, budget headroom, and per-tile failure reasons to unauthenticated
scrapers.

### 6. TLS termination in front
The gateway listens plaintext on port 8000 (8100 on the dharm EC2
sandbox). Put an ALB, nginx, or Caddy in front to terminate HTTPS.
Forward `X-Forwarded-For` / `X-Forwarded-Proto`; the JWT validator and
audit records honour them.

---

## SHOULD-do

### Multi-worker shape
For sustained traffic >50 req/s, set BOTH
`WALACOR_UVICORN_WORKERS=4` AND `WALACOR_REDIS_URL=redis://redis:6379/0`.
Redis is part of `docker-compose.yml` (`redis:7-alpine`); bumping workers
without Redis flips the FEA-06 readiness check red on purpose. With
Redis, session-chain and budget state are shared across workers — but
sticky sessions at the LB are still required for chain correctness
(`src/gateway/pipeline/orchestrator.py:_apply_session_chain`).

### Backups
Snapshot `{wal_path}/` to S3 daily. Critical files:

- `wal-*.db` — pending records not yet anchored at Walacor
- `intelligence.db` — verdict log + training data
- `control.db` — attestations, policies, budgets, content_policies
- **Do not snapshot** `gateway-bootstrap-key.txt` — rotate via SSM instead

### Persistent log sink
Ship container logs to CloudWatch (or your aggregator of choice).
Stdout-only logging loses everything if the container dies; the audit
chain survives in Walacor + WAL, but the request-level debug context
does not.

### LLM provider opt-in
`WALACOR_LLAMA_GUARD_ENABLED` now defaults to `false`
(`src/gateway/config.py`). Enable it only if Ollama and
`llama-guard3:1b` are reachable in the same network — otherwise FEA-01
and DEP-03 stay green on a no-Ollama deployment.

---

## Smoke tests before opening to traffic

1. `GET /health` — returns `status: healthy`
2. `GET /v1/readiness` with API key — returns `ready`
3. `POST /v1/chat/completions` with each attested model — succeeds, and
   `/v1/lineage/executions/{id}` shows the resulting execution + attempt
   record
4. `POST /v1/chat/completions` with an UNattested model — returns 403
   `model_not_attested`
5. Same call with wrong API key — returns 401
6. Bring Walacor down for 60 s — gateway keeps serving, WAL grows,
   `/v1/connections` `walacor_delivery` tile flips amber/red, and on
   recovery the queue drains (see `src/gateway/walacor/sink.py`)
7. `GET /v1/compliance/export?format=pdf` — returns a real PDF (requires
   Pango + Cairo on the host; see CLAUDE.md Operations note)
8. Push 50 concurrent requests at `/v1/chat/completions` while watching
   `/v1/connections` — every tile stays green; budget and chain state
   stay coherent across workers

---

## References

- `CLAUDE.md` — architectural invariants, failure-mode guards
- `docs/FLOW-AND-SOUNDNESS.md` — pipeline flowcharts + soundness analysis
- `README.md` — config knobs, env vars
- `.env.gateway.example` — full env var reference with defaults
