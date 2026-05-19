# Design: Multi-worker shared-state redesign (Finding 4)

Status: **DESIGN — not implemented.** Deliberately separate from the
audit-integrity hotfix (PR #37, merged). Each phase below is gated by the
same live-verification discipline that proved the durability fixes.

## Problem

Production runs a **single-worker uvicorn** (`deploy/Dockerfile`,
`scripts/native-setup.sh` — no `--workers`). Under genuine distributed
load (many source IPs) the per-IP rate limiter does not shed — each IP
gets its own budget — and all traffic lands on one event loop with no
horizontal capacity. This is the only unresolved finding from the stress
test; it is architectural, not a patch.

It is single-worker **by design**, because two pieces of state are
process-local and unsafe to share naively:

1. **WAL writer** (`wal/writer.py`): one `sqlite3` connection + a
   dedicated writer thread + an unbounded `queue.Queue`. N OS processes
   writing the *same* SQLite file ⇒ lock contention and, with the
   `PRAGMA synchronous=NORMAL` WAL-mode config, real corruption risk.
2. **In-process governance state**: the per-IP sliding-window rate
   limiter, the attestation/policy/budget caches, the model-capability
   registry, singleflight TTL caches, and the `_pending_attempt_writes`
   set. Split across workers these become per-worker copies.

Naively adding `--workers N` would change how the gateway works
(SQLite multi-writer corruption; N× looser rate limit; N× token
budgets). That is why it was left alone under the "don't change
behaviour" constraint. This design is the deliberate, staged way to
lift the ceiling **without** regressing those invariants.

## State inventory & per-component decision

| State | Multi-worker hazard | Decision |
|---|---|---|
| WAL writer (SQLite + thread + queue) | Corruption / lock contention | **Per-worker WAL files**, namespaced by worker id; lineage reader unions them; each worker's DeliveryWorker sink drains its own file. Preserves local-durability semantics with no new infra. |
| DeliveryWorker (now Walacor-sink, #34) | Two workers draining same rows | Already keyed per WAL file ⇒ with per-worker WALs each drains only its own. No change beyond #1. |
| Per-IP rate limiter (sliding window, in-mem) | Becomes per-IP-per-worker ⇒ N× looser | **Edge limit now** (LB/reverse-proxy concurrency cap — zero code, ships anywhere). Optional later: shared store (Redis) if per-edge is insufficient. |
| Attestation / policy caches | Per-worker copies, eventually consistent | **Accept.** Already eventually-consistent via the sync loop; read-mostly; correctness unaffected. Document. |
| Budget tracker (token budgets, in-mem) | N× over-spend (each worker has full budget) | **Must address.** Options: (a) single-writer budget worker; (b) shared store; (c) divide budget by worker count at boot (crude but safe, no infra). Recommend (c) for phase-1, (b) later. |
| Model-capability registry / singleflight TTL | Per-worker; at worst N× upstream probes | **Accept.** Self-healing, fail-open, bounded. Document. |
| `_pending_attempt_writes` set | Process-local; fine per-worker | No action (already correct per process). |

## Phased rollout (each phase independently shippable & verifiable)

- **Phase 0 — edge concurrency cap (immediate, no code).** Put a
  connection/inflight limit on the LB/reverse-proxy in front of the
  gateway. Sheds distributed load *before* the single event loop. This
  is the safe interim mitigation and is recommended regardless of the
  rest. Ships to any environment (infra config, documented in deploy).
- **Phase 1 — multi-worker-safe WAL.** Per-worker WAL filename
  (`wal-{worker_id}.db`); `LineageReader`/`WalacorLineageReader` union
  across files for reads; each worker's DeliveryWorker sink drains its
  own. Budget divided by `--workers` at boot (crude-safe). No behaviour
  change at workers=1 (single file, byte-identical).
- **Phase 2 — externalize the loose state (optional).** Redis-backed
  per-IP rate limiter + shared budget counter, *only if* Phase 0's edge
  limit proves insufficient. Adds an infra dependency — explicitly opt-in,
  fail-open to per-worker behaviour if Redis is down.
- **Phase 3 — raise workers + prove.** Bump `--workers`, re-run the
  live-backend stress suite, assert: zero audit loss per worker, WAL
  union read consistency, budget not exceeded in aggregate, rate-limit
  behaviour matches the documented model.

## Risks (explicit)

- **SQLite multi-writer corruption** — eliminated by per-worker files
  (Phase 1); the only acceptable design without external infra.
- **Token budget over-spend** — N× unless divided/shared; Phase 1 uses
  divide-by-workers (safe, slightly conservative).
- **Rate-limit dilution** — per-worker is N× looser; mitigated by the
  edge cap (Phase 0) which is the real enforcement point.
- **Lineage read consistency** — union across per-worker WALs must be
  ordered/deduped; covered by Phase 1 reader changes + tests.

## Non-goals

Not changing request-path behaviour at `workers=1` (must stay
byte-identical — this is the regression gate for every phase). Not
introducing a hard infra dependency by default (Redis is opt-in,
fail-open).

## Recommendation

Do **Phase 0 immediately** (it is the real distributed-load defense and
costs nothing in code or behaviour). Schedule Phase 1 as the next
engineering block. Treat Phase 2 as conditional. Phase 3 gates the
worker bump on the same live-verification bar used for the durability
fixes — no "assume", measured proof per worker.
