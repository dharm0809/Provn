# Walacor backend ticket — prod tenant not anchoring submitted records

**Reported by:** Gateway team (Dharmpratap)
**Date:** 2026-05-11
**Walacor server:** `http://3.238.12.86/api`
**Affected tenant / ETIds:** `walacor-prod` / `9000031` (gateway_executions), `9000032` (gateway_attempts), `9000033` (gateway_tool_events)
**Gateway identity:** `gw-ed63c0a71eb9`
**Severity:** **Customer-blocking integrity gap** — auditable signatures present, but no blockchain anchor proof produced. Readiness check `INT-04` red.

## Summary
The gateway-side flow works end to end against this backend:
1. `POST /auth/login` — succeeds (`api_token` returned, refreshes proactively)
2. `POST /envelopes/submit` for ETIds 9000031/32/33 — returns 200, records persist
3. Records are queryable via `POST /query/getcomplex` — full envelope metadata returned

**However:** the three on-chain proof fields (`BlockId`, `TransId`, `DH`) **stay null indefinitely** on every record. The anchor/seal worker isn't producing the cryptographic checkpoint the gateway's `G2 — Full-fidelity Audit` guarantee depends on.

This is the same symptom CLAUDE.md documents for the legacy sandbox (`sandbox.walacor.com`) — but now reproduced on the prod backend.

## Concrete evidence
Direct query result against `http://3.238.12.86/api/query/getcomplex` for `ETId=9000031`, captured 2026-05-11 17:01 UTC (gateway team):

| row | EId | execution_id | BlockId | TransId | DH |
|---|---|---|---|---|---|
| 0 | `0600387c-64df-41f9-b786-105a6e7d285b` | `4e2d02ff-014c-4de0-8c8a-754794aada87` | **null** | **null** | **null** |
| 1 | `f13a6d1c-023f-450c-be37-74f0007c5a24` | `38c45e54-101a-47a8-b7b3-e46b0f29edfb` | **null** | **null** | **null** |
| 2 | `83bd797e-392e-425f-982e-19b220468ae7` | `66dd7b46-cdc7-4794-b0d3-0fa4fc0b609d` | **null** | **null** | **null** |

Same pattern across `ETId=9000032` (attempts). All other envelope fields populated correctly: `CreatedAt`, `UpdatedAt`, `SV=2`, `UID`, `ORGId`, plus the entire gateway field set (`execution_id`, `record_signature`, `record_id`, `sequence_number`, etc.).

## Gateway-side confirmation
- `/v1/connections` `walacor_delivery` tile: **green**, `success_rate_60s: 1.0`, last_success ~5s ago. No write failures.
- `/health`: storage backend `walacor`, executions/attempts ETIds confirmed. WAL pending=76 (records the gateway is *waiting on anchor confirmation* for, oldest ~48 days).
- `/v1/readiness` checks:
  - `DEP-01` (Walacor auth): **green** — auth succeeds
  - `DEP-02` (Walacor query): **green** — query succeeds
  - `INT-04` (anchoring active): **red** — "0/12 recent records anchored (0%)"
  - `INT-05` (anchor round-trip): amber — blocked by INT-04

## Workaround search — exhausted on gateway side
We probed the backend for any client-callable anchor trigger before sending this:
- `GET /envelopes/anchor` exists but returns `400 "Envelope does not exists"`
  for every parameter shape we tried (`EId`, `eid`, `id`, `envelope_id`,
  `envelopeId`, `UID`, `uid`, `ETId`; ETId=50 system, ETId=9000031 data;
  query string, header, body). Path-style (`/envelopes/<eid>/anchor`,
  `/envelopes/anchor/<eid>`, `/envelopes/anchor/all`, `/envelopes/anchor/batch`)
  all 404.
- `POST /envelopes/anchor` is 404 outright.
- Other anchor-shaped endpoints (`/anchor`, `/seal`, `/admin/anchor`,
  `/jobs/anchor`, `/blockchain/anchor`, `/envelopes/submitAndAnchor`,
  `/envelopes/seal`, `/envelopes/finalize`) — all 404.
- `POST /envelopes/submit` with `Anchor: true` / `Seal: true` / `finalize: true`
  flags — flags are silently ignored (request only fails schema validation).
- The Walacor admin SPA at the root URL has no anchor UI; its JS bundle
  references only `submit`, `hashes`, `graphData`, `filehashes` for envelopes.

Conclusion: anchoring on this backend has no API surface a client can
invoke. The fix has to happen inside your ops layer.

## What we need from Walacor backend team
1. Confirm whether ETIds 9000031/32/33 on tenant `walacor-prod` are configured for blockchain anchoring (or whether anchoring is opt-in per-ETId / per-tenant).
2. If anchoring is configured: check the anchor-worker / cron status — is it running, behind, error-looping? Last successful anchor batch timestamp would be useful.
3. If it requires a different submit endpoint or flag (e.g. `submitAndAnchor` vs `submit`), please confirm the contract — gateway currently uses the standard `POST /envelopes/submit` with `{"Data": records}` body, `ETId` header.
4. ETA for backfilling anchors on the 76 pending records, or guidance on whether those should be considered lost-to-anchor (we'll mark them with a separate provenance tag if so).
5. (If applicable) a backend CLI / internal command we can run via `kubectl exec` or an admin SSH to kick the anchor worker manually while the root cause is investigated.

## Reproduction
With Walacor admin creds, anyone can reproduce in 60 seconds:
```bash
# (from any host with httpx / curl)
TOKEN=$(curl -sX POST http://3.238.12.86/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"userName":"<user>","password":"<pass>"}' | jq -r .api_token)

curl -sX POST http://3.238.12.86/api/query/getcomplex \
  -H "Authorization: $TOKEN" -H "ETId: 9000031" -H "Content-Type: application/json" \
  -d '[{"$match":{}},{"$sort":{"_id":-1}},{"$limit":3}]' | jq '.[].BlockId, .[].TransId, .[].DH'
# Expected: blockchain identifiers. Actual: null, null, null
```

## Gateway-side cross-check (not the issue, just for completeness)
- Ed25519 record signing is functional on gateway side as of today's install: `INT-01` green, `INT-03` shows 12/12 signatures verify. Gateway can produce the canonical-ID signature; this ticket is only about the backend-side blockchain anchor that maps `record → BlockId/TransId/DH`.

## Contact
- Gateway team: `vagheladharmpratap@gmail.com` (Dharmpratap)
- Gateway repo: `dharm0809/Provn`, branch `prod/v2-baselines`
- Live gateway: `http://54.236.73.245:8100` (admin key on request)
