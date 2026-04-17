# Phase 25 Intelligence Layer — Audit Findings

**Branch:** `feature/phase25-onnx-self-learning` @ `7b48869`
**Audit date:** 2026-04-17
**Auditors:** concurrency-focused agent (completed); adversarial + resource + API-security agents rate-limited before writing chaos tests.

## Verified bugs (reproducible via code inspection)

### 🔴 C1 — ShadowRunner session-cache double-load race
`src/gateway/intelligence/shadow.py:55-72`

`get_session` is check-then-set on `self._sessions`. Callers wrap it in `asyncio.to_thread`, so two shadow tasks for the same `(model, version)` can both see `None`, both construct `InferenceSession`, and the second overwrites the first. The orphaned session lives on in ORT's arena — leaks under burst traffic.
**Fix:** guard with `threading.Lock` (not asyncio; call site is in a worker thread).

### 🔴 C2 — Promote idempotency is TOCTOU; returns 404 instead of 409
`src/gateway/intelligence/api.py:269-286`

`_last_promotion_per_model` is read BEFORE `registry.promote`, and the `lifecycle_events_mirror` row is written AFTER. Two simultaneous `POST /promote/intent/v1`: both see empty mirror → both call `registry.promote`. Registry's per-model lock serializes the rename; winner succeeds, loser raises `FileNotFoundError` → returns **404** ("candidate not found"), violating the plan's 409-idempotency guarantee.
**Fix:** after `registry.promote` raises `FileNotFoundError`, re-check mirror OR check whether `production/{model}.onnx` already matches the requested version and return 409.

### 🔴 C3 — Distillation trigger blocks event loop
`src/gateway/intelligence/distillation/worker.py:102-109, 288-297`

`run()` calls `self._should_trigger()` directly (line 108). `_should_trigger` does `sqlite3.connect` + `COUNT(*)` on `onnx_verdicts` — a full table scan on a table bounded only by the hourly retention sweep. Executes on the asyncio event loop thread, blocking every request during the scan.
**Fix:** wrap in `await asyncio.to_thread(self._should_trigger)`, matching `_last_snapshot_timestamp`.

### 🟡 I2 — `_retrain_tasks` dict race + unbounded under burst
`src/gateway/intelligence/api.py:452, 479-486`

`_reap_retrain_tasks()` runs AFTER inserting the new task. 100 concurrent `POST /retrain/intent` → each enters before any reap → dict spikes to 100+. Also, no per-model serialization: 100 overlapping `retrain_one("intent")` run in parallel and can race on candidate file creation (timestamp-second granularity may collide).
**Fix:** reap at entry, cap dict size (e.g. 100, reject with 429 beyond), and either serialize per-model retrains with a lock or make the candidate filename collision-safe (matches Task 10's archive naming).

## Reported but not re-verified (accept concurrency agent's assessment)

- **I1** — `maybe_reload` + ORT inference: latent race if any classifier moves inference to `to_thread`. Safe today, fragile.
- **I3** — `HarvesterRunner.stop()` doesn't cancel `_inflight` tasks (teacher-LLM calls abandoned but not cancelled).
- **I4** — Teacher-LLM calls share `ctx.http_client` pool; stalled Ollama can exhaust pool and stall main request path. Fix: dedicated `httpx.AsyncClient` or concurrency semaphore.
- **I5** — Flush worker drops a whole batch on SQLite-locked error (already acknowledged in module docstring as "known limitation"). Elevate to tracked issue.
- **I6** — `_init_intelligence` task-creation ordering: currently safe, fragile to future reordering.

## Coverage gaps (not tested)

The three stress-test agents (adversarial inputs, resource exhaustion, promotion API security) hit rate limits before producing test files. The detailed prompts are preserved; re-run them after budget resets and commit the resulting `tests/unit/test_phase25_chaos_{adversarial,resource,promotion_api}.py` files.

Scenarios we specifically wanted to exercise and did NOT:
- Unicode/NUL/control bytes through ModelVerdict → SQLite round-trip
- Extreme payload sizes (1-10 MB prompts) hitting harvesters
- Malformed LlamaGuard output parsing in SafetyHarvester
- Poisoned teacher-LLM responses
- Path-traversal on `model_name`/`version` HTTP params
- Concurrent 20x promote/rollback race (empirical, not just code-inspection)
- Flush worker under SQLite lock contention from all 5 writers
- Rollback with empty archive / corrupt archive file
- X-User-Id CRLF injection into lifecycle mirror

## Recommendation

Before merging to `main`:
1. Fix C1, C2, C3 (all small patches).
2. Fix I2 (reap-at-entry is one-line; per-model serialization is a small lock).
3. Re-run the three stress agents after budget resets; land the chaos test files.
4. Keep the existing tier6c suite for EC2 as the production smoke test.
