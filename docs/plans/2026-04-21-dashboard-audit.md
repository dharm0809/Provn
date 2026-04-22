# Dashboard UI Data-Fetching / API Audit — 2026-04-21

**Scope:** React dashboard at `src/gateway/lineage/dashboard/src/` and its connections
to `/health`, `/metrics`, `/v1/lineage/*`, `/v1/control/*`.

**Method:** Explore subagent pass, then manual verification. Several subagent
claims turned out to be false positives (marked NOT-A-BUG below) so every
finding here has been eyeballed against the real code.

---

## Verified real issues (to fix)

### R1 — Compliance.jsx is 100% hardcoded (`views/Compliance.jsx`)
**Severity:** major (page shows fake data)
- Framework scores (`{score: 88, grade: 'B', gaps: 2}` etc.) are literal constants.
- "1,842 / 1,842 VERIFIED", "depth 247,019 records", `sha256:4a7b…e01c` are
  literal text, not fetched.
- No API calls anywhere in the file.
- The "COMING NEXT" eyebrow signals it's an intentional stub, but the user
  asked for a full audit: this needs to either (a) be wired to real data, or
  (b) be clearly labelled as a demo so users don't read fake scores as truth.
- **Fix (chosen):** keep the stub layout but label it clearly as "preview —
  not yet connected" and blank out the misleading hard-coded numbers, so
  nobody mistakes the demo for real compliance data.

### R2 — Playground.jsx is 100% hardcoded (`views/Playground.jsx`)
**Severity:** major (Send button is a no-op)
- `<button>▶ Send</button>` has no `onClick`.
- Response paragraphs and governance readout (`exec_8a91b4c2e5f0`, `att_3f7de1a8`,
  `342ms`, etc.) are literal JSX, not fetched.
- Same "COMING NEXT" eyebrow as R1.
- **Fix (chosen):** same as R1 — mark as preview, disable the Send button so
  the UI doesn't pretend to work.

### R3 — ModelsView fetches `/health` for data it never renders (`views/Control.jsx:82-90`)
**Severity:** minor (wasted fetch, dead state)
- `load()` calls `Promise.all([api.getAttestations(), api.getHealth()])`.
- `setModelCaps(health.model_capabilities || {})` sets state that isn't read
  anywhere in ModelsView (grep confirms: `modelCaps` appears only on
  declaration line 71).
- Every re-auth / re-load fans out an extra `/health` request for nothing.
- The real consumer of `model_capabilities` is `StatusView` (line 648), which
  receives `health` via props and doesn't need ModelsView's copy.
- **Fix:** drop `modelCaps` state and the `getHealth()` call.

### R4 — Timeline.jsx uses wrong length for `isLast` (`views/Timeline.jsx:114`)
**Severity:** minor (cosmetic: extra connector line under last user record)
- Iterates `userRecords` (line 109) but uses `records.length - 1` for `isLast`.
- When a session has any `system_task` records, the last *user* record sees
  `i < records.length - 1`, so `isLast=false`, and the chain connector line
  is drawn past the bottom of the visible chain.
- **Fix:** `isLast = i === userRecords.length - 1`.

### R5 — `api.js` doesn't URL-encode `range` (`api.js:62-67`)
**Severity:** minor (defensive hygiene)
- `getTokenLatency` and `getThroughputHistory` interpolate `range` into the
  URL as a raw string. The backend allow-lists the value, so today it's only
  ever `'1h' | '24h' | '7d' | '30d'`, but the rest of the file uses
  `URLSearchParams` (e.g. lines 30-36) and should be consistent.
- **Fix:** use `URLSearchParams` for both.

### R6 — Control CRUD buttons swallow non-AUTH errors silently (`views/Control.jsx:88, 131, 139, 149, 163, 251, 252, 318, 342, 375, 469, 491, 522, 622`)
**Severity:** minor (UX — user can't tell if action succeeded)
- Every `catch (e) { if (e.message === 'AUTH') refresh(); }` block drops
  network, 4xx, 5xx errors on the floor. The user clicks Revoke/Approve/
  Remove/Delete, nothing visually happens on error, they assume success.
- Intelligence.jsx already handles this correctly via `toast.show(...)` —
  same pattern should apply.
- **Fix:** add a lightweight inline error banner to ModelsView/PoliciesView/
  BudgetsView that surfaces the error message.

### R7 — Sessions.jsx React.memo comparator tests non-existent fields (`views/Sessions.jsx:180-181`)
**Severity:** minor (possible stale rows; hard to observe)
- The comparator compares `a.tool_names === b.tool_names` and
  `a.tool_details === b.tool_details`, but the row actually renders
  `(s.tools || []).map(...)` (line 153). The backend `list_sessions`
  response doesn't include `tool_names`/`tool_details` — both sides are
  `undefined` on every poll, so the comparator returns "unchanged" and the
  row never updates when `tools` changes.
- In practice `tools` rarely changes mid-session, so visible impact is
  minimal, but the logic is wrong.
- **Fix:** compare a JSON-stringified snapshot of `s.tools` (or drop the
  comparison and rely on `last_activity` as the canary — any session that
  gains a tool also gets a new activity timestamp).

---

## NOT-A-BUG (Explore subagent false positives)

Recorded so future audits don't re-flag these.

- **Overview.jsx "hook violation after early return"** — the subagent
  misread the file. All hooks (lines 178-337) are above `if (loading)
  return <Skeleton />` (line 339). This matches the reference pattern
  called out in CLAUDE.md.
- **Execution.jsx "te.sources missing Array.isArray guard"** — the guard
  exists (line 303 and 306). Same for `toolAnalysis`.
- **Execution.jsx "record wrapper will break if backend returns flat"** —
  backend explicitly returns `{record, tool_events, ...convenience}`
  (`lineage/api.py:222-229`), frontend reads `data.record` correctly.
- **Intelligence.jsx "response shape mismatch"** — every consumer uses
  `res?.field || res || []` fallback, already robust.
- **api.js query injection on range** — range is not user input and is
  allow-listed server-side. Still worth encoding for hygiene (R5), but
  not a CVE.
- **Cost endpoint unused** — `/v1/lineage/cost` is not in `api.py`'s
  active route list right now; the agent misread a stale comment.

---

## Fix ordering

1. R3 — drop dead `/health` fetch in ModelsView (trivial).
2. R4 — Timeline `isLast` off-by-one (trivial).
3. R5 — `URLSearchParams` for `range` (trivial).
4. R6 — inline error banner in Control sub-views (small).
5. R7 — SessionListRow comparator (trivial).
6. R1/R2 — label stubs clearly, disable fake Send button (small).

No dashboard rebuild required for JS-only changes to take effect on a dev
server; on EC2 a `npm run build` inside `src/gateway/lineage/dashboard/` is
needed. The backend serves whatever is in `src/gateway/lineage/static/` via
`StaticFiles`.
