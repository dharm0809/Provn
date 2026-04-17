# Test Coverage Plan — Walacor Gateway

**Created:** 2026-04-17
**Status:** In progress. Phase A next.
**Context:** After the torture test surfaced 5 real bugs (orchestrator `body_dict`, intelligence worker prompt templates, `upsert_attestation` idempotency, missing session-chain serialization, dotenv test leakage — all fixed), we need to expand coverage from the narrow "base torture test" to the full feature surface.

> **⚠️ SWITCH TO OPUS 4.7 BEFORE PHASE F.** Composition / kitchen-sink tests may expose subtle interaction bugs that benefit from deeper reasoning. Phases A–D are Sonnet-appropriate.

---

## Approach — property-based first, examples as scaffolding

Earlier draft of this plan proposed one torture test per feature. That approach is **too narrow** — it only catches bugs in code paths I hand-wrote. The real plan:

1. **Property-based torture tests** (primary). For each phase, define the **invariant** the gateway must uphold, then let `hypothesis` generate arbitrary valid/invalid inputs and drive thousands of trials. Every test is a STATEMENT, not an example.

2. **Chaos scenarios** (secondary). Inject random failures — kill workers, corrupt DB, flip policies mid-workload, fail provider HTTP — and assert the gateway degrades gracefully (fail-closed where required, fail-open where documented).

3. **Example-based torture tests** (scaffolding only). Hand-written examples to make debugging easier when a property test finds a counter-example. Written AFTER the property test, not before.

Dependencies to add to `pyproject.toml [dev]`:
- `hypothesis>=6.100` — property-based testing
- `pytest-xdist>=3.5` — parallel test execution (property tests are slow)

---

## Invariants the gateway must uphold

Every phase below is really one or more of these invariants applied to a feature area. If any of these are violated, the gateway has a bug — regardless of feature config.

| # | Invariant | Phase |
|---|-----------|-------|
| I1 | **Completeness**: every request that reaches the app produces exactly one `gateway_attempts` row | A, B |
| I2 | **Session chain**: per session_id, sequence_number is contiguous; every `record_hash` recomputes; `previous_record_hash` links form a valid chain from genesis | A, B, C |
| I3 | **Policy enforcement**: request matches a deny rule ⇒ 403 + `denied_policy` disposition + NO execution record written | A |
| I4 | **Content enforcement**: response contains blocked category ⇒ 403 + `denied_content` disposition + content NOT stored | A |
| I5 | **Budget enforcement**: sum of tokens billed ≤ configured cap | A, C |
| I6 | **Audit completeness**: for any execution, `tool_events` count ≥ tools invoked; input/output hashes match their contents | B |
| I7 | **Dual-write**: when both Walacor and WAL configured, every record lands in both | B |
| I8 | **No 500s**: any valid HTTP method returns a response in {2xx, 4xx} — never 5xx except explicitly simulated | All |
| I9 | **Redis parity**: Redis-backed trackers produce the same chain/budget math as in-memory trackers | C |
| I10 | **No memory leak**: buffer sizes stay bounded under sustained load | C, F |

---

## Phase A — Governance correctness

**Claim we want to be able to make:** *"If you write a policy that denies X, X is denied. If you mark a content category as BLOCK, that content never reaches the model or the user."*

### A1 — Property: policy DENY is enforced for all matching requests
**File:** `tests/integration/test_property_policy_enforcement.py`
**Hypothesis strategy:** generate arbitrary `Policy(rule_field, operator, pattern)` + arbitrary `Request(model_id, headers, tenant)`.
**Property:** `request_matches_policy(req, policy) ↔ response.status_code == 403 AND attempts_row.disposition == 'denied_policy'`.
**Additional assertions:** no execution record for denied requests, metric counter increments correctly.

### A2 — Property: content-analyzer BLOCK prevents content leakage
**File:** `tests/integration/test_property_content_block.py`
**Strategy:** generate arbitrary request payloads, some containing patterns from `category_pattern_library` (PII, toxic, credit card, etc.). Seed content_policies with action=BLOCK for each category.
**Property:** `payload_matches_block_category(payload) ↔ response.status_code == 403 AND content_NOT_in_execution_record`.
**Edge cases Hypothesis will find:** Unicode obfuscation, base64-wrapped, multi-language PII, encoding edge cases.

### A3 — Property: streaming BLOCK aborts stream AND writes audit
**File:** `tests/integration/test_property_streaming_block.py`
**Strategy:** MockTransport yields a configurable sequence of SSE chunks, one of which contains blocked content at a generated position.
**Property:** `any_chunk_contains_blocked_content(stream) ⇒ stream_aborted BEFORE that chunk reaches client AND execution_record written WITH content_analysis.blocked=True`.

### A4 — Property: budget cap is never exceeded
**File:** `tests/integration/test_property_budget_enforcement.py`
**Strategy:** generate arbitrary budget configs + arbitrary request sequences.
**Property:** `sum(total_tokens over all successful executions for tenant) ≤ budget.max_tokens`. Also: once cap is reached, subsequent requests return 429 until period tick.

### A5 — Chaos: policy changes mid-workload
**File:** `tests/integration/test_chaos_policy_hot_reload.py`
**Scenario:** fire 200 concurrent requests; at random mid-points, update/delete/recreate policies via the control plane API. Randomly toggle enforcement_level between "blocking" and "logging".
**Invariants:** no 500s, disposition matches policy state at the time of the request (approximate — within the sync window), completeness holds.

**Phase A deliverable:** 4 property tests + 1 chaos test ⇒ covers invariants I1, I3, I4, I5, I8. Estimated 15–18 hours.

---

## Phase B — Audit completeness

**Claim we want to make:** *"For any production request, the audit trail is complete, accurate, and cryptographically verifiable — tamper-evident back to genesis."*

### B1 — Property: Merkle chain is always valid
**File:** `tests/integration/test_property_session_chain.py`
**Strategy:** generate arbitrary workloads (mix of sessions, models, tenants, concurrent across sessions, sequential within sessions).
**Property:** for every session, after the workload drains, `verify_chain(session_id).valid == True`. AND per-session `sequence_number` forms contiguous `0..N-1` without gaps. AND `record_hash` recomputes via the canonical formula. (Note: the just-landed per-session lock makes this invariant robust to in-session concurrency too — the property test should drive concurrent same-session traffic to verify.)

### B2 — Property: tool-event audit captures full interaction
**File:** `tests/integration/test_property_tool_audit.py`
**Strategy:** MockTransport simulates arbitrary tool-call sequences (0-N tool calls per turn, up to 5 iterations). Tool outputs contain arbitrary content including edge cases.
**Property:** per execution, `tool_events.count ≥ tool_calls_simulated`. Each tool_event has `input_hash == SHA3_512(serialized_input)` and `output_hash == SHA3_512(serialized_output)`. `input_data` field contains the actual arguments (not just the hash).

### B3 — Property: dual-write parity (Walacor + WAL)
**File:** `tests/integration/test_property_dual_write.py`
**Strategy:** generate arbitrary workloads; both Walacor-mock and local WAL enabled.
**Property:** `count(records in Walacor-mock) == count(records in WAL)` AND for every `execution_id`, the record JSON matches byte-for-byte between the two stores.

### B4 — Chaos: Walacor backend unreliability
**File:** `tests/integration/test_chaos_walacor_failures.py`
**Scenario:** MockTransport for Walacor randomly returns 500/timeout/500/success (configurable failure rate). Fire workload.
**Invariants:** local WAL never loses records (fail-open on Walacor failure), attempts still logged, delivery worker retries failed writes and eventually succeeds, no 500s leak to client.

**Phase B deliverable:** 3 property tests + 1 chaos test ⇒ covers I1, I2, I6, I7. Estimated 16–20 hours.

---

## Phase C — Multi-replica + scale

**Claim:** *"The gateway scales horizontally without losing chain integrity, budget tracking accuracy, or rate-limit correctness."*

### C1 — Property: Redis-backed tracker parity with in-memory
**File:** `tests/integration/test_property_redis_parity.py`
**Strategy:** run the same arbitrary workload twice — once with `SessionChainTracker` (in-memory) and once with `RedisSessionChainTracker` (fakeredis). Also run the same workload with both budget tracker variants.
**Property:** for every session, the final chain state is identical between the two runs. Same for budget totals.

### C2 — Chaos: multi-worker state divergence
**File:** `tests/integration/test_chaos_multi_worker.py`
**Scenario:** spin up 3 gateway processes sharing one fakeredis backend. Interleave requests across workers for the same session.
**Invariants:** chain remains valid across workers (Redis is the source of truth), budget never over-spent, no duplicate sequence_numbers.

### C3 — Property: rate limiter respects caps
**File:** `tests/integration/test_property_rate_limit.py`
**Strategy:** generate arbitrary rate-limit configs + arbitrary request patterns with Hypothesis' stateful rule-based state machine. Time is controlled via monkeypatched clock.
**Property:** request count per sliding window never exceeds the cap. Sibling scopes (different tenant) don't share limits.

### C4 — Chaos: adaptive concurrency under random provider latency
**File:** `tests/integration/test_chaos_adaptive_concurrency.py`
**Scenario:** MockTransport returns responses with randomly distributed latencies (mix of 10ms, 500ms, 5s, timeout). Fire sustained workload.
**Invariants:** the limiter's `limit` tracks ~p95 latency, no cascading failures, one slow provider doesn't starve others.

**Phase C deliverable:** 2 property tests + 2 chaos tests ⇒ covers I2, I5, I8, I9. Estimated 18–22 hours.

---

## Phase D — Supporting features (batched)

Cache, hedging, LB, OTel, prompt guard, DLP — **one combined property test** rather than six. Each feature has a small, well-defined invariant; a parametrized property test covers them without six separate files.

### D1 — Parametrized property: each supporting feature's advertised behavior holds
**File:** `tests/integration/test_property_supporting_features.py`
**Strategy:** for each feature, a sub-property:
- Semantic cache: `identical_request_within_ttl ⇒ cache_hit AND no_provider_call`
- Hedging: `slow_primary ⇒ hedged_request_wins_and_loser_cancelled`
- LB weighted routing: over N requests, provider distribution matches weights (within stat. tolerance)
- OTel: `request_with_tracer_enabled ⇒ exactly_one_span_per_request`
- Prompt guard: `known_jailbreak_pattern ⇒ blocked`
- DLP: `protected_pattern ⇒ blocked`

**Phase D deliverable:** 1 parametrized property test. Estimated 8–10 hours (less than the 12h I originally budgeted for six separate tests, higher coverage).

---

## Phase F — Kitchen-sink composition (switch to Opus 4.7 here)

**Claim:** *"Every feature works when enabled together, not just in isolation."*

### F1 — Stateful property test with Hypothesis `RuleBasedStateMachine`
**File:** `tests/integration/test_stateful_gateway.py`
**Approach:** model the gateway as a state machine. Rules: `arrive_request`, `update_policy`, `revoke_attestation`, `tick_time`, `kill_worker`, etc. Hypothesis generates arbitrary sequences of these rules across ALL features enabled.
**Invariants (checked after every rule):** all ten listed at top. Any violation reproduces with a minimized counter-example.

**Phase F deliverable:** 1 stateful test. Estimated 12–15 hours (debugging counter-examples is slow but findings are high-value).

---

## Phase E — Production-tier EC2 (after F is green)

Run the existing `tests/production/run_all_tiers.sh` against the patched build on EC2. No new tests — this phase is about exercising the property/chaos tests we built in A-F on real infrastructure.

### E1 — Real-provider smoke (Ollama + OpenAI/Anthropic if keys available)
Run a subset of property tests (e.g. `test_property_session_chain.py`) with real upstream instead of MockTransport.

### E2 — tier1-7 gauntlet
Existing suite.

### E3 — 24-hour soak
Run `test_stateful_gateway.py` for 24 hours. Validates no memory leaks, no drift in chain integrity.

**Phase E deliverable:** green tier1-7 + soak. Estimated 3–4 EC2 hours + triage.

---

## Total effort (revised)

| Phase | Property tests | Chaos tests | Example tests | Effort |
|-------|----------------|-------------|---------------|--------|
| A | 4 | 1 | as debugging scaffolding | 15-18 h |
| B | 3 | 1 | as needed | 16-20 h |
| C | 2 | 2 | as needed | 18-22 h |
| D | 1 (parametrized) | 0 | as needed | 8-10 h |
| F | 1 stateful | 0 | n/a (the state machine IS the test) | 12-15 h |
| E | 0 (reuses A–F) | 0 | 0 | 3-4 h EC2 |
| **Total** | **11** | **4** | **ad-hoc** | **~70 h** |

Slightly lower than the original 67h estimate despite being more rigorous — the property tests collapse many hand-written examples into single statements.

---

## Why this is better than feature-by-feature

| Feature-focused (original) | Property + chaos (revised) |
|---------------------------|----------------------------|
| 20 test files, one per feature | 11 property tests, 4 chaos tests |
| Tests only what I thought to write | Tests what Hypothesis generates — often finds edge cases I'd never write |
| A single test = a single example | A single test = a statement about behavior ("FOR ALL inputs…") |
| Interaction bugs need Phase F to catch | Stateful test catches them as part of the property run |
| Each test owns one invariant | Each test verifies an invariant against 1000s of generated inputs |

---

## What's still out of scope (acknowledged)

1. **Adversarial security review / pentest** — needs external reviewer
2. **Sustained 1000+ QPS benchmarks** — capacity planning, separate workstream
3. **Multi-tenant noisy-neighbor isolation** — needs real multi-tenant deploy
4. **Version-to-version upgrade paths** — post-v1
5. **Cross-region / geo-distribution** — post-v1

These are deliberately deferred and documented so they don't get forgotten.

---

## Immediate next action

Execute Phase A1 — property-based policy enforcement test. Sonnet 4.6 is appropriate.

**Remind me to switch to Opus 4.7 before Phase F.**
