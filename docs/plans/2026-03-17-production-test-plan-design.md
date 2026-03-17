# Production Test Plan Design
**Date:** 2026-03-17
**Environment:** AWS EC2 m6a.xlarge (Amazon Linux 2023), IP `16.145.247.20`
**Stack:** Gateway (port 8002) + Ollama (qwen3:1.7b) + OpenWebUI (port 3000) via Docker Compose
**Goal:** Full public product launch validation — data integrity, security, performance baseline, resilience, compliance artifacts

---

## Approach: Risk-Stratified Tiers with Sequential Gates and Parallel Execution

Combines three testing philosophies:
- **Approach C (Risk-Stratified):** Tiers ordered by criticality — integrity first, security second, performance third, resilience fourth, compliance artifacts last
- **Approach A (Sequential Gates):** Each tier has a pass/fail gate; must pass before proceeding to the next
- **Approach B (Parallel Execution):** Tests within each tier run in parallel where they don't contend with each other

---

## Tier Structure

```
Tier 1: Audit Integrity        → Gate: 100% pass, all records verified
Tier 2: Security Controls      → Gate: zero bypass vectors, all blocks audited
Tier 3: Performance Baseline   → Gate: saturation point documented, no memory leaks
Tier 4: Resilience             → Gate: all failure scenarios handled gracefully
Tier 5: Compliance Artifacts   → Gate: all artifacts generated = launch-ready
```

---

## Tier 1 — Audit Integrity
**Execution:** Parallel (no live infra required for unit/compliance)
**Estimated time:** ~15 min

| Test | What it validates | Where |
|------|------------------|-------|
| Unit tests (856 tests) | All mocked unit tests pass | `pytest tests/unit/` |
| Compliance tests G1/G2/G3 | Attestation, hash-only, policy enforcement | `pytest tests/compliance/` |
| StorageRouter fan-out | Both WAL and Walacor legs fire on every request | Unit + live |
| Session chain verification | `/v1/lineage/verify/{id}` returns valid chain for 10+ sessions | Live on AWS |
| Merkle tree chain | Merkle tree hashes computed and verifiable | Unit |
| Ed25519 signing | Tamper-evident records pass signature verification | Unit |
| Completeness invariant | Every request (success, error, denied) produces an attempt record | Live on AWS |
| WAL integrity | WAL readable, batch commits working, no corruption after restart | Live on AWS |
| Transparency log publisher | Log submission succeeds, entry recorded | Unit |
| Trace API | `GET /v1/lineage/trace/{id}` returns correct combined execution data | Live on AWS |
| Lineage API (all 5 endpoints) | `/sessions`, `/executions`, `/attempts`, `/verify`, `/token-latency` correct | Live on AWS |
| Dual-write verification | WAL and Walacor backend both contain matching records | Live on AWS |

**Gate:** 100% unit + compliance pass rate; all live integrity checks pass

---

## Tier 2 — Security Controls
**Execution:** Parallel (different attack vectors, no contention)
**Estimated time:** ~20 min

| Test | What it validates |
|------|------------------|
| No API key → 401 | Auth middleware blocks unauthenticated requests |
| Wrong API key → 401 | Key validation correct |
| JWT tampering | Tampered tokens rejected; mismatched X-User-Id vs JWT sub → JWT wins |
| Both auth modes | `api_key`, `jwt`, `both` modes all behave correctly |
| Policy deny | Revoked model returns 403; audit record written |
| Attestation revoke | Revoked model via control plane cannot be auto-re-attested |
| Budget exhaustion | Token budget enforced; subsequent requests blocked with 429 |
| PII blocking (high-risk) | SSN/credit card/AWS key in response → BLOCK, `pii_detected=true` in audit |
| PII warn (low-risk) | IP address/email in response → WARN only, not blocked |
| Toxicity blocking | Toxic content blocked, not leaked to caller |
| Llama Guard S4 | Child safety category → hard BLOCK |
| Llama Guard S1/S9/S11 | WARN categories recorded in audit, not blocked |
| Prompt injection (PromptGuard) | Direct + indirect (via tool output) injection detected |
| DLP classifier | Enterprise DLP patterns detected and flagged |
| Image OCR + PII | Images with embedded PII (screenshots) scanned and flagged |
| Shadow policy | Test policies do not leak block decisions to callers |
| Concurrency limiter | Per-client cap enforced; excess requests queued or rejected |
| Token rate limiter | Token-per-minute cap enforced |
| Control plane auth | `/v1/control` requires X-API-Key; returns 401 without it |
| API surface audit | No stack traces in 500 responses; CORS headers correct |
| Lineage read-only | `/lineage` and `/v1/lineage` bypass auth correctly (read-only) |

**Gate:** Zero bypass vectors found; all block events produce audit records

---

## Tier 3 — Performance Baseline
**Execution:** Sequential ramp (each step builds on previous)
**Estimated time:** ~45 min on AWS

| Test | Target metric |
|------|--------------|
| Baseline latency (1 user, 10 requests) | p50 < 2s, p99 < 5s (model-dependent, document actual) |
| Semantic cache hit rate | Repeated identical prompts: p99 < 200ms (cache hit), document hit % |
| Ramp: 10 concurrent users | No errors; latency within 2× baseline |
| Ramp: 50 concurrent users | Error rate < 1%; p99 documented |
| Ramp: 100 concurrent users | Establish saturation point; document max stable req/s |
| 30-min sustained load (50% saturation) | Memory stable; no WAL corruption; no OOM |
| Streaming under load | SSE streams complete without interruption |
| CPU/Memory at saturation | CPU < 90%; RAM < 90% of 16 GB |
| WAL batch commit throughput | Batch fsync visible in metrics; throughput > unbatched baseline |

**Tool:** `locust` or `aiohttp`-based script (parameterized for `qwen3:1.7b`)
**Note:** governance_stress.py must be parameterized to use `qwen3:1.7b` instead of `qwen3:4b`/`gemma3:1b`

**Gate:** Saturation point documented; 30-min sustained test passes; no memory leaks; SLA card generated

---

## Tier 4 — Resilience
**Execution:** Parallel (different failure scenarios, independent)
**Estimated time:** ~30 min

| Test | What it validates |
|------|------------------|
| Ollama goes down mid-request | Gateway returns 502/503; audit record (attempt) written; circuit breaker trips |
| Circuit breaker recovery | After reset_timeout, probe request succeeds; circuit closes |
| Gateway restart mid-load | WAL not corrupted after kill + restart; session chain resumable |
| Provider cooldown trigger | >50% error rate in 60s → 30s cooldown; subsequent requests queued |
| Fallback routing | Primary provider 503 → fallback endpoint serves request correctly |
| Disk pressure | DiskSpaceProbe triggers; WAL limits auto-scale; no crash |
| Memory pressure | 100 concurrent requests; gateway does not OOM |
| SSE keepalive | Long-running streams send keepalive pings; client does not timeout |
| Stream safety | Partial/interrupted streams produce attempt record, no half-written WAL entries |

**Gate:** All failure scenarios handled gracefully; no data loss; no unhandled exceptions in logs

---

## Tier 5 — Compliance Artifacts
**Execution:** Mostly automated, some manual captures
**Estimated time:** ~20 min

| Artifact | Method |
|----------|--------|
| Governance stress report | Run `governance_stress.py` (parameterized for `qwen3:1.7b`); capture JSON results |
| PDF compliance report | `GET /v1/compliance/report` → save PDF; verify non-empty sections |
| Audit log export | File exporter: verify JSONL output; Webhook exporter: verify delivery |
| Cost attribution accuracy | Create pricing entry; send requests; verify per-model cost computed correctly |
| Lineage dashboard screenshots | Playwright: sessions list, execution detail, chain verification, tool events |
| Chain + signing audit report | Verify 50+ sessions via `/v1/lineage/verify/{id}`; export pass/fail summary |
| EU AI Act coverage | Cross-check `docs/EU-AI-ACT-COMPLIANCE.md` against live gateway features |
| Performance SLA card | Summary doc from Tier 3 results (p50/p95/p99, max req/s, saturation point) |
| Health endpoint completeness | `/health` exposes model_capabilities, budget tracker, content analyzers |
| Metrics endpoint validation | `/metrics` returns valid Prometheus text format; key counters present |

**Gate:** All artifacts generated and saved to `tests/artifacts/`; EU AI Act doc matches live features = **LAUNCH READY**

---

## Environment Notes

- **Model on AWS:** `qwen3:1.7b` (not `qwen3:4b` or `gemma3:1b` — adjust all test scripts)
- **No Redis:** AWS setup uses in-memory trackers; Redis tier tests can run locally or be noted as out-of-scope for this instance
- **No Walacor backend:** Running in self-attested / skip-governance or local-only mode; WAL is the authoritative audit store
- **Ports:** Gateway on `8002`, OpenWebUI on `3000`, Ollama on `11434` (internal)
- **Start command:** `docker compose up -d` from `~/Gateway`

---

## Success Criteria (Launch-Ready Checklist)

- [ ] Tier 1: 856+ unit tests pass, all integrity checks pass
- [ ] Tier 2: Zero security bypass vectors; all governance controls enforced
- [ ] Tier 3: Performance SLA card generated with documented saturation point
- [ ] Tier 4: All resilience scenarios handled gracefully; no data loss
- [ ] Tier 5: All compliance artifacts saved to `tests/artifacts/`
