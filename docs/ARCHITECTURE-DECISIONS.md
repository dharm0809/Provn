# Architecture Decisions

This document records significant architectural decisions taken during the
evolution of the Walacor Gateway. Each entry follows the lightweight ADR
(Architecture Decision Record) format: context, decision, consequences.

---

## ADR-001: WAL durability uses synchronous=NORMAL, not full fsync

Date: 2026-04-29
Status: Accepted

### Context

The local Write-Ahead Log (`src/gateway/wal/writer.py`) is the gateway's
durability boundary for execution and attempt records during the dual-write
to the Walacor backend. The original entry point was named
`write_and_fsync(...)`, which implies a per-write `fsync()` call. The
implementation has always issued `PRAGMA synchronous=NORMAL` (in WAL
journal mode), which is **not** a per-write fsync. The mismatch between the
function name and the actual durability semantics created confusion for
operators reading the code.

The two options on the table were:

1. **Upgrade the PRAGMA to `synchronous=FULL`** so the function name matches
   the behavior. SQLite would then fsync on every commit. This caps
   throughput at a few hundred writes/sec on consumer SSDs and adds
   roughly 5–15 ms per batch under sustained load.
2. **Rename the function to `write_durable(...)`** and document the
   semantics clearly. SQLite continues to use `synchronous=NORMAL` in
   WAL mode, which is crash-safe (atomic; the database is never
   corrupted) but allows the most recent in-flight transaction to be
   lost on power failure.

Our threat model treats the local WAL as a short-lived buffer. The
authoritative ledger is the Walacor backend; the WAL exists to keep the
gateway functional during backend outages and to satisfy the dual-write
invariant. A power-failure window of at most one in-flight commit is
acceptable because:

- The delivery worker only marks records `delivered=1` after the Walacor
  ledger has accepted them, so a dropped in-flight commit simply
  re-anchors on the next attempt — no record is silently lost from the
  caller's perspective.
- The completeness middleware writes its `gateway_attempts` row as a
  separate transaction, so an in-flight WAL commit loss does not blind us
  to the request having occurred.
- Anchoring throughput is dominated by the Walacor side, not by
  gateway-local fsync latency. Capping local writes to a few hundred per
  second would become the new bottleneck.

### Decision

We chose option 2: **rename `write_and_fsync` to `write_durable`** and
keep `synchronous=NORMAL`. The function's docstring now states explicitly
that durability is crash-safe but not per-write fsync.

A `WALACOR_WAL_FSYNC_FULL` environment variable is reserved (TODO, not
yet wired) for operators who decide their SLA requires the stronger
guarantee. Flipping it would set `PRAGMA synchronous=FULL` on both
connections opened by `WALWriter` — the main-thread connection in
`_ensure_conn` and the dedicated writer thread connection in
`_ensure_thread_conn`.

### Consequences

- The function name now matches its behavior. New contributors do not
  have to read the docstring to discover the discrepancy.
- All callsites in `src/` and `tests/` were updated atomically. No
  external consumers exist; this is an internal API.
- If a future SLA review concludes that per-write fsync is required, the
  upgrade path is mechanical: change the two PRAGMA lines, wire the
  `WALACOR_WAL_FSYNC_FULL` setting through `Settings`, and document the
  throughput trade-off in the readiness page. No callsite changes are
  needed because `write_durable` already encapsulates the choice.
- Operators who suspect a durability problem on power failure should
  re-examine this decision in light of the actual recovery semantics:
  the gateway is expected to lose at most one in-flight commit per
  process, not a record visible to a caller.

## ADR-002: HTML marketing docs are hand-edited, not generated

Date: 2026-04-29
Status: Accepted

### Context

`docs/*.html` files (`gateway-workflow.html`, `walacor-gateway-solution-brief.html`,
`walacor-gateway-2pager.html`, `enterprise-architecture.html`, etc.) are static
marketing artifacts: each is a self-contained HTML page with embedded `<style>`
blocks, hand-tuned typography, and bespoke layout. There is no markdown source,
no generator script, and no template — the HTML is the source of truth.

This means whenever the markdown docs (`docs/*.md`) are rewritten — for example,
when a feature is removed or terminology changes (Merkle chain → ID-pointer chain)
— the corresponding HTML files must be hand-edited to match. There is no automated
sync.

### Decision

We accept the hand-edit cost rather than building a docs generator. Marketing
artifacts have low edit frequency, the styling is bespoke (a generator would
either constrain it or balloon in complexity), and the audience for HTML
(prospects, executives) is more sensitive to visual polish than the audience
for markdown (engineers, operators).

### Consequences

- When a doc rewrite affects user-facing terminology, search `docs/*.html` for
  the old phrasing and update each file in parallel with the markdown.
- A regression check: `grep -niE "merkle|sha3-512|transparency.log|hedge" docs/*.html`
  should return zero matches if the cleanup is complete.
- If a future regeneration framework lands (e.g. mkdocs with custom themes), this
  ADR can be revisited.
