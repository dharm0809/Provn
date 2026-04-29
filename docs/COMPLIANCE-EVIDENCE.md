# Compliance evidence

The gateway is designed to support ATO packages with test-based evidence for:

- **G1 (Attestation gate):** Only attested, non-revoked models are forwarded; fail-closed when attestation cannot be verified.
- **G2 (Cryptographic recording):** Every execution is dual-written to the local WAL and the Walacor backend, which issues a tamper-evident `DH` (data hash) on ingest; WAL ensures no record loss across crashes.
- **G3 (Policy enforcement):** Pre-inference policy evaluation; blocked requests never reach the provider; policy version recorded; fail-closed when policy cache is stale.

## Running the compliance test suite

From the repo root:

```bash
PYTHONPATH=Gateway/src pytest Gateway/tests/compliance -v --tb=short
```

Tests under `Gateway/tests/compliance/` are structured to produce clear pass/fail outcomes and, where applicable, artifacts (e.g. WAL state, captured requests) that can be attached to an ATO package.

## Evidence mapping

| Guarantee | Test / check | Evidence |
|-----------|----------------|----------|
| G1 | Request with attested model → forwarded; unknown/revoked model → 403 | HTTP status and logs |
| G1 fail-closed | Control plane down, cache expired → request → 503, not forwarded | HTTP 503, no upstream call |
| G1 startup | Control plane down at startup → gateway does not start | Process exit / startup error |
| G2 durability | Kill gateway mid-request; restart; verify WAL record replayed to control plane | WAL file + control plane execution record |
| G2 hash-only | Capture traffic gateway → control plane; no plaintext prompt/response in body | Network capture / request body |
| G2 idempotent | Same execution_id POSTed twice → 409 on second | Control plane 409 response |
| G3 | Compliant prompt → forwarded; blocking policy violation → 403 | HTTP status |
| G3 fail-closed | Policy cache stale (control plane down past threshold) → 503 | HTTP 503 |
| G3 version | Every execution record includes correct policy_version | WAL and control plane payloads |

## Interpreting results

- All compliance tests should **pass** in a compliant deployment.
- Save test output and any generated artifacts (WAL dumps, request logs) for your ATO evidence package.
- For fail-closed tests, ensure the control plane (or mock) is actually unreachable so that the gateway’s 503/startup failure is exercised.
