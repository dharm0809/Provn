# Connections Page — Design

**Date:** 2026-04-23
**Status:** Design approved, pending implementation plan
**Owner:** Gateway team
**Related:** Phase 26 readiness (`/v1/readiness`), Phase 23 adaptive (`DefaultResourceMonitor`)

## Problem

Silent failures in the Gateway (fail-open analyzers, swallowed tool-loop exceptions, Walacor delivery dropping, stream interruptions, stale policy cache, etc.) are logged but have no dashboard surface. Operators cannot answer "is anything silently broken right now?" without tailing logs.

## Goal

One dashboard page (`/connections`) that combines:
- **Live ops view** — 10 subsystem tiles, each green/amber/red with one-line detail, refreshed every 3s.
- **Recent events stream** — last ~50 degradation events from in-memory ring buffers, newest first, clickable into the affected session when a session context exists.

**Non-goals:** historical storage, alerting/notifications, per-user filtering. No new tables; everything derived from live in-process state (option C from the brainstorm).

## Architecture

One new endpoint `GET /v1/connections` returning a snapshot JSON. Singleflight + 3s TTL cache (same pattern as `/v1/readiness`). Aggregates:

### Reused (no new code, just compose)

| Tile | Source |
|---|---|
| Providers | `DefaultResourceMonitor` (+ new `snapshot()` accessor composing existing internals) |
| Model capabilities | `/health` `model_capabilities` (already exposed) |
| Control plane | `/health` `policy_cache.{version,last_sync,stale}` + `_sync_task.done()` check |
| Auth (bootstrap) | `/v1/readiness` SEC-01 `bootstrap_key_stable` |
| Readiness | `/v1/readiness` rollup + new `disposition` filter on `get_attempts()` |
| Intelligence (partial) | `/health` `intelligence` block (`verdict_log_rows`, `last_training_at`) |

### New instrumentation (bounded deques, no new storage)

| Gap | Change | File |
|---|---|---|
| Walacor delivery fail-open | `deque(maxlen=100)` + `.delivery_snapshot()` | `src/gateway/walacor/client.py` |
| Analyzer fail-opens | Mixin on `ContentAnalyzer` base + 4 one-line call-site patches | `src/gateway/content/base.py` (+ `llama_guard.py`, `presidio_pii.py`, `safety_classifier.py`, `prompt_guard.py`) |
| Tool-loop swallowed exc | Module-level `deque(maxlen=50)` + accessor | `src/gateway/pipeline/tool_executor.py` |
| Stream interruption | Module-level `deque(maxlen=50)` + accessor | `src/gateway/pipeline/forwarder.py` |
| Intelligence worker state | `.snapshot()` reading `_queue.qsize()` + `_last_error` attr | `src/gateway/intelligence/worker.py` |

All new deques are bounded. No persistence. No schema changes.

### Fail-open contract

If any probe errors while computing the snapshot, its tile goes `status:"unknown"` (grey, not red) with `detail.error` set. Endpoint never returns 5xx.

## Data Contract

```json
{
  "generated_at": "2026-04-23T21:04:00Z",
  "ttl_seconds": 3,
  "overall_status": "green | amber | red",
  "tiles": Tile[10],
  "events": Event[]
}
```

Rollup: `red` if any tile red; `amber` if any tile amber; else `green`. `unknown` tiles do not contribute to rollup.

### Tile envelope

```json
{
  "id": TileId,
  "status": "green" | "amber" | "red" | "unknown",
  "headline": string,              // ≤60 chars
  "subline": string,               // ≤80 chars
  "last_change_ts": string | null, // ISO8601
  "detail": object                 // per-tile shape below
}
```

### Tiles (fixed order)

**1. `providers`**
```json
{"providers": {"<name>": {"error_rate_60s": 0.02, "cooldown_until": null | "ISO", "last_error": null | "..."}}}
```
red if any provider in cooldown; amber if any `error_rate_60s > 0.20`; else green.

**2. `walacor_delivery`**
```json
{
  "success_rate_60s": 0.98,
  "pending_writes": 4,
  "last_failure": null | {"ts": "...", "op": "...", "detail": "..."},
  "last_success_ts": "...",
  "time_since_last_success_s": 1.2
}
```
red if `success_rate_60s < 0.5` or `time_since_last_success_s > 120`; amber if `< 0.95`; else green.

**3. `analyzers`**
```json
{"analyzers": {"<name>": {"enabled": true, "fail_opens_60s": 0, "last_fail_open": null | {"ts":"...","reason":"..."}}}}
```
red if any enabled analyzer `fail_opens_60s >= 5`; amber if any `>= 1`; else green.

**4. `tool_loop`**
```json
{
  "exceptions_60s": 0,
  "last_exception": null | {"ts":"...","tool":"...","error":"..."},
  "loops_60s": 42,
  "failure_rate_60s": 0.00
}
```
red if `failure_rate_60s > 0.2`; amber if `> 0.05` or exception within 60s; else green.

**5. `model_capabilities`**
```json
{"models": [{"model_id": "...", "supports_tools": true|false, "auto_disabled": true|false, "since": null | "ISO"}], "auto_disabled_count": 1}
```
amber if `auto_disabled_count > 0`; green otherwise. Never red.

**6. `control_plane`**
```json
{
  "mode": "embedded" | "remote" | "disabled",
  "policy_cache": {"version": "...", "last_sync_ts": "...", "age_s": 7, "stale": false},
  "sync_task_alive": true,
  "attestations_count": 4,
  "policies_count": 3
}
```
red if `sync_task_alive=false` or `stale=true`; amber if `age_s > sync_interval * 2`; else green.

**7. `auth`**
```json
{
  "auth_mode": "api_key" | "jwt" | "both",
  "jwt_configured": true,
  "jwks_last_fetch_ts": null | "ISO",
  "jwks_last_error": null | {"ts":"...","detail":"..."},
  "bootstrap_key_stable": true
}
```
red if `jwks_last_error` within 60s; amber if `bootstrap_key_stable=false`; else green.

**8. `readiness`**
```json
{
  "rollup": "ready" | "degraded" | "unready",
  "reds":   [{"check_id":"...","detail":"..."}],
  "ambers": [{"check_id":"...","detail":"..."}],
  "degraded_rows_24h": 2
}
```
Maps 1:1 — `unready`→red, `degraded`→amber, `ready`→green.

**9. `streaming`**
```json
{
  "interruptions_60s": 0,
  "last_interruption": null | {"ts":"...","provider":"...","detail":"..."},
  "streams_60s": 18,
  "interruption_rate_60s": 0.00
}
```
red if `interruption_rate_60s > 0.3`; amber if `> 0.1` or interruption within 60s; else green.

**10. `intelligence_worker`**
```json
{
  "running": true,
  "queue_depth": 3,
  "oldest_job_age_s": 1.2,
  "last_error": null | {"ts":"...","detail":"..."},
  "last_training_at": "...",
  "verdict_log_rows": 18432
}
```
red if `running=false` or `last_error` within 60s; amber if `queue_depth > 100` or `oldest_job_age_s > 60`; else green.

### Event

```json
{
  "ts": "ISO8601",
  "subsystem": "providers|walacor_delivery|analyzers|tool_loop|model_capabilities|control_plane|auth|readiness|streaming|intelligence_worker",
  "severity": "info" | "amber" | "red",
  "message": string,              // ≤140 chars
  "session_id":   string | null,
  "execution_id": string | null,
  "request_id":   string | null,
  "attributes":   object
}
```

Stream merges all deque entries across subsystems, sorted `ts` desc, capped 50.

### Edge states

- Subsystem disabled by config (e.g. `intelligence_worker` absent): tile present with `status:"unknown"`, `headline:"disabled"`.
- Probe failure: `status:"unknown"` + event entry describing the probe error.
- Empty `events: []`: UI shows reassuring "no silent failures in the last N minutes".

## Rollout

1. **Instrumentation deques** — 5 independent PRs, no behavior change, each with unit tests.
2. **`DefaultResourceMonitor.snapshot()`** — pure refactor.
3. **`get_attempts()` `disposition` kwarg** — additive.
4. **`/v1/connections` endpoint** — new module `src/gateway/connections/api.py`, mounted under `api_key_middleware`.
5. **Dashboard** — `/connections` route via Claude Design handoff.

Rollback flag: `WALACOR_CONNECTIONS_ENABLED=true` (default true). When false → 503.

## Testing

- **Unit**: `tests/unit/connections/test_api.py` — envelope shape, rollup rules per tile, fail-open behavior (forced exception in each source), cache TTL.
- **Unit**: one test per new deque (Walacor / analyzer / tool / stream / intelligence) — record events, assert snapshot shape.
- **Tier 1 live**: smoke check — endpoint returns 200, has all 10 tiles, `overall_status` valid.
- **Stress test**: `/v1/connections` p95 ≤100ms under 88-parallel-request load.
- **Regression gate**: all existing endpoints and tests unchanged.

## Visual Design — Hybrid (v3 spine + v4 banner + runbook-in-drawer)

The UI is **not** designed from scratch. It is ported from the Claude Design deliverable bundled at:

```
docs/plans/assets/2026-04-23-connections-truzenai/project/
```

Three variants were produced: `v2` (grouped/swim-lanes), `v3` (severity triage queue), `v4` (incident cockpit). We ship a **hybrid**:

### Base layout = `connections-v3.jsx`

Port `project/overview/connections-v3.jsx` and `connections-v3.css` **verbatim** as the page spine:
- Severity-ranked triage queue (reds expanded, ambers one-liners, greens collapsed strip)
- `V3_CONSEQUENCE` copy table — ship as-is
- 3-column count header (Down / Degraded / Healthy)
- Events stream with `V3EventRow`
- `V3Panel` tile-detail slide-over with `CxJsonView`

### Additions from `connections-v4.jsx`

Graft two pieces from v4 on top of the v3 spine:
1. **Banner stats strip** (the six-number row from v4's cockpit banner: `N DOWN · N DEGRADED · N HEALTHY | N SESSIONS HIT · N EXECUTIONS HIT · N REQUESTS HIT`). Render pinned at the top of the page whenever `overall_status != "green"`. Lift `V4Stat` component + banner container styles verbatim.
2. **Incident headline block**: when `counts.red >= 1`, render a compact red banner showing the incident driver (`◆ ACTIVE INCIDENT · started <ago> · <plane> plane`, headline, "OPEN PROBE DETAIL →" CTA). Lift `v4-banner` markup + styles from v4; drop the 3-column cockpit body.
3. **Runbook section inside the tile-detail drawer** (`V3Panel`): append a new block below `CxJsonView` that renders v4's `V4Runbook` component verbatim when a runbook entry exists for `tile.id`, otherwise shows a muted "no runbook yet" line. Reuse v4's `V4_RUNBOOK` object as the curation source — ship with the 3 seed entries (`walacor_delivery`, `auth`, `providers`); other subsystems gracefully render the empty state.

### Explicitly dropped from the bundle

- v4 3-column cockpit (`v4-cockpit`, `v4-col-stream`, `v4-col-context`, `v4-col-runbook`)
- v4 "Recent changes" lane (`V4Changes`, `RECENT_CHANGES` mock) — no data source today
- v4 amber degradation board (`v4-amber-board`, `V4AmberCard`) — v3 triage queue covers amber already
- v4 subsystem checklist footer (`v4-footer`, `v4-checklist`) — v3 green strip covers the same role
- All of v2 (swim-lane grouping, `V2View`, `V2EventRow`, `V2Panel`)

### Shared components from the bundle (port verbatim)

From `project/overview/connections-shared.jsx` — port as a single `ConnectionsShared.jsx` helper module:
- Status helpers: `cxStatusLabel`, `cxStatusClass`, `cxPillClass`, `cxSeverityClass`, `cxCountsByStatus`, `cxGroupSummary` (unused but keeps the module intact)
- Time helpers: `cxAgo`, `cxFmtTime`, `cxShortId`
- Reusable UI: `CxCopyBtn`, `CxJsonView`, `CxScenarioPicker` (only during dev; strip before merging)
- Shared data: port `project/overview/connections-data.js` to `data/connectionsMocks.js` — used only by Storybook/dev; production reads from the live API

### Strict port rules

1. **Do not reinvent, restyle, or "improve" any component that exists in the bundle.** Copy the JSX and CSS class names verbatim. The only allowed edits are:
   - Replace the inline mock-data wiring (`window.ConnectionsMocks`, `useState(() => scenarios.amber)`) with a `useConnections()` hook that calls `getConnections()` every `ttl_seconds`.
   - Remove the `CxScenarioPicker` from the intro block (dev-only).
   - Wire `navigate('sessions', { q: session_id })` to the real router (push `?view=sessions&session_id=<id>`).
   - Adjust import paths.
2. **CSS tokens:** bundle CSS uses `--gold`, `--green`, `--amber`, `--red`, `--text-primary`, `--mono`, etc. These come from the existing dashboard `styles.css`. Do not redefine them. Do not introduce new tokens. If a style in the bundle references a token that doesn't exist in the live dashboard, stop and report — don't invent a fallback.
3. **File mapping:**
   - `project/overview/connections-v3.jsx` → `src/gateway/lineage/dashboard/src/views/Connections.jsx` (with the v4 banner + runbook grafts)
   - `project/overview/connections-v3.css` + relevant `connections-v4.css` selectors (banner, V4Stat, runbook) → `src/gateway/lineage/dashboard/src/styles/connections.css`
   - `project/overview/connections-shared.jsx` → `src/gateway/lineage/dashboard/src/components/ConnectionsShared.jsx`
4. **If any grafted v4 piece conflicts with a v3 style**, preserve v3 and open an issue — do not silently merge.

## Claude Design handoff — scope lock

**DO build:**
- `src/gateway/lineage/dashboard/src/views/Connections.jsx` (ported from `connections-v3.jsx`)
- `src/gateway/lineage/dashboard/src/components/ConnectionsShared.jsx` (ported from `connections-shared.jsx`)
- `src/gateway/lineage/dashboard/src/styles/connections.css` (merged from `connections-v3.css` + v4 banner/runbook selectors)
- Nav entry in `App.jsx` for the `/connections` route (single line)
- `api.js`: one new helper `getConnections()` calling `/v1/connections`

**DO NOT TOUCH:**
- `Overview.jsx`, `Sessions.jsx`, `Timeline.jsx`, `Attempts.jsx`, `Compliance.jsx`, `Playground.jsx`, `Intelligence.jsx`, `Control.jsx`, `Execution.jsx`
- `main.jsx`, `ErrorBoundary`, routing except the single new route
- Any existing CSS file in `src/styles/` (control.css, exec-drawer.css, etc.)
- Any existing api.js helper
- Any backend file — if backend gaps surface, stop and report, do not invent endpoints

**DO NOT REINVENT:**
- Any JSX in the TruzenAI bundle. Port verbatim, rewire data source, move on.
- Any CSS class names in the bundle. Keep `cx-*`, `v3-*`, `v4-*` prefixes as-is.
- Any copy in `V3_CONSEQUENCE` or `V4_RUNBOOK`. That's curation work, not code.
- Any color or token value. Reuse existing dashboard tokens.

**Rules of Hooks:** per project convention, every `useMemo`/`useState`/`useEffect`/`useCallback` must sit BEFORE any early `return`. Use `Overview.jsx` as the reference pattern. Violating this produces React Error #310 and a fully blank dashboard.

**Behavior notes:**
- Poll every `ttl_seconds` (3s) — not faster.
- Tiles rendered in the fixed order from the contract (providers → intelligence_worker).
- `status:"unknown"` renders grey, not red.
- Events newest-first; clicking an event with `session_id` navigates to `?view=sessions&session_id=<id>`; without `session_id` the row is non-clickable.
- Tile drill-in: clicking a tile opens the v3 slide-over with raw `detail` JSON + runbook block.

## Open questions

None blocking. Implementation plan to follow via `writing-plans` skill.
