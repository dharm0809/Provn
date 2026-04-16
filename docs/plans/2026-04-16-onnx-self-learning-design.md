# ONNX Self-Learning Loop — Design

**Date:** 2026-04-16
**Scope:** Layer 1 (Self-Learning) of the Adaptive Gateway Intelligence program
**Status:** Approved design; implementation plan to follow

---

## 1. Context

The Walacor Gateway currently ships with three ONNX models:

| Model | Location | Purpose |
|---|---|---|
| Intent Classifier | `src/gateway/classifier/model.onnx` | Two-tier request type classification: `normal / rag / reasoning / system_task / web_search / mcp_tools` |
| Schema Mapper | `src/gateway/schema/schema_mapper.onnx` | Maps any provider's response JSON to a canonical schema via value-aware field classification |
| Safety Classifier | `src/gateway/content/safety_classifier.onnx` | Lightweight content-safety classification across 8 categories |

All three are fail-open, statically shipped in the repo, and loaded at startup. Their verdicts are logged into audit records but do not drive routing decisions.

### Gaps this design addresses

1. **Dead-ended distillation loop.** `intelligence/worker.py` buffers high-confidence LLM reclassifications, but `get_distillation_buffer()` is never called and no retraining pipeline exists.
2. **SchemaMapper overflow unused.** `schema_mapper_overflow_keys` is captured in response metadata and never exported.
3. **Safety disagreements uncaptured.** SafetyClassifier and Llama Guard both run on the same text, but their disagreements (which are the most valuable training signal) are not logged as such.
4. **No model versioning, A/B, or shadow mode.** Models are all-or-nothing swaps via code change.
5. **Hardcoded thresholds.** 0.95 / 0.70 confidence gates, 3σ anomaly bound. Deferred in this phase except for per-model confidence calibration, which is folded into training output.

### Session intent

Make the Gateway's intelligence layer self-improving without compromising its observer identity. The user explicitly chose:

- **A — Self-learning:** primary goal (this design)
- **B — Self-acting:** deferred to a separate design; verdicts will become inputs to the policy engine, never drive action unilaterally
- **C — Self-tuning thresholds:** rejected except for per-model confidence calibration (auditable, published)

---

## 2. Approach

Chosen from three proposed options: **Option 2 — In-Gateway Closed Loop with Shadow Mode.**

- Collect verdicts from production inference.
- Harvest ground-truth signals asynchronously from existing audit artifacts.
- Periodically retrain candidate models in-process.
- Validate via shadow mode on live traffic.
- Promote via dashboard; human-in-loop by default, `auto_promote=true` opt-in per model.

**Rejected alternatives:**

- **Option 1 (Offline retraining with manual model copy):** too slow, too manual, no closed loop.
- **Option 3 (External Intelligence Service):** overkill; deferred until multi-instance deployments require it.

### Architectural principle

> **ONNX produces verdicts; policies decide actions.**

The intelligence layer observes and improves itself; any effect on traffic is mediated by operator-configured policy rules, which are declarative and auditable. This preserves the Gateway's observer identity and keeps all "acting" behavior explicit, reversible, and operator-controlled.

---

## 3. Architecture — five components

### 3.1 Verdict Log

- SQLite table `onnx_verdicts` in a new `intelligence.db` (sibling to `control.db`).
- Columns: `id`, `model_name`, `input_hash` (SHA3-256 of canonical input), `input_features_json`, `prediction`, `confidence`, `request_id`, `timestamp`, `divergence_signal`, `divergence_source`.
- 30-day TTL via a background sweeper.
- Indexes on `(model_name, timestamp)` and `(divergence_signal, model_name)` for training queries.

### 3.2 Feedback Harvesters

One per model, running asynchronously post-response. Each consumes from an async queue populated by the response pipeline's `finally` block (same pattern as completeness middleware) and writes `divergence_signal` + `divergence_source` back onto the original verdict row via `UPDATE`.

**Intent Harvester**
- Primary signal: next-turn contradiction — user's follow-up contradicts the classification (e.g., classified `normal`, next turn asks to search).
- Secondary signal: was the classified intent actually acted on? (web_search classification → did a tool actually fire?)
- Tertiary signal (sampled ~1%): call a teacher LLM to relabel low-confidence verdicts.

**SchemaMapper Harvester**
- Reads `schema_mapper_overflow_keys` from the response metadata.
- Each overflow key + its resolved canonical label (if discovered via fallback rules later) becomes a training candidate labeled "UNKNOWN → X".

**Safety Harvester**
- When SafetyClassifier and Llama Guard both ran on the same input, records disagreements.
- Llama Guard verdict acts as the teacher label; SafetyClassifier's verdict becomes the "needs correcting" signal.

### 3.3 Distillation Worker

- Background asyncio task.
- Schedule: nightly cron (configurable) **OR** triggered by a dashboard "Force retrain" button **OR** auto-triggered once `N` divergences are observed (default: 500).
- Training runs via `asyncio.to_thread()` / `ThreadPoolExecutor` to avoid blocking the event loop. Intent + Safety retrain in seconds; SchemaMapper in tens of seconds.
- Pipeline: query divergences → dedupe → class-balance → build dataset → SHA3-hash dataset → write `training_dataset_fingerprint` Walacor event → train sklearn model → export to `.onnx` via `skl2onnx` → place candidate in `candidates/` → write `candidate_created` Walacor event.
- Adversarial robustness: per-session contribution capped at 10% of the training set to prevent a single source from dominating.

### 3.4 Model Registry

- Directory layout under `{models_base}/`:
  - `production/{model_name}.onnx` — current live model
  - `candidates/{model_name}-{version}.onnx` — pending candidates
  - `archive/{model_name}-{version}.onnx` — retired models
- Atomic swap via `os.rename`.
- Per-model `asyncio.Lock` prevents concurrent promotions.
- On swap, registered `InferenceSession` consumers reload on the next inference call (session rebuild; old session GC'd).
- Rollback: promote an archived version back to production (one click from the dashboard).

### 3.5 Shadow Validator

- When a candidate exists for a model, each production inference mirrors to the candidate via thread-pool executor — **non-blocking**, never awaited for the user response.
- Both predictions are logged to a `shadow_comparisons` SQLite table.
- After the target sample size (default 1000): compute `candidate_accuracy`, `production_accuracy`, `disagreement_rate`, `candidate_error_rate`.
- Promotion gate: candidate accuracy > production by ≥ 2%, disagreement < 40%, candidate error rate < 5%, McNemar test `p < 0.05` on paired predictions.
- Write `shadow_validation_complete` Walacor event with metrics.
- If `auto_promote=true` AND gate passes → auto-promote. Else → appears in dashboard awaiting human approval.

---

## 4. Storage Split

| Data | Store | Retention |
|---|---|---|
| Verdict log (per-request predictions) | SQLite only | 30 days |
| Shadow comparisons | SQLite only | Cleared after promotion decision |
| Training dataset rows | SQLite | 90 days or until superseded |
| Training dataset fingerprint (SHA3 + row_ids) | Walacor | Immutable |
| Shadow validation summary | Walacor | Immutable |
| Model promotion event (approver, dataset hash, metrics) | Walacor + SQLite mirror for dashboard | Immutable on chain |

### Walacor ETId

Single new ETId `onnx_lifecycle_event` with an `event_type` discriminator:

- `training_dataset_fingerprint`
- `candidate_created`
- `shadow_validation_complete`
- `model_promoted`
- `model_rejected`

Follows the same dual-write pattern as execution records.

### Escalation path

If a high-assurance deployment ever demands it, flipping every verdict to also write to Walacor is a config flag — the dual-write wiring is already in place.

---

## 5. Dashboard — new "Intelligence" sub-tab

Joins Models / Policies / Budgets / Status under the existing Control tab; gated by the same API key auth; uses Phase 21 `CallerIdentity` for the `approver` field on promotions.

Operations:

- **Production models** — name, version, loaded timestamp, prediction count, trailing accuracy.
- **Candidates** — source summary ("Intent: 847 divergences harvested 2026-04-01 → 2026-04-15"), training dataset hash, created timestamp.
- **Shadow progress** — accuracy delta, disagreement rate, candidate error rate, samples collected / target.
- **Promote / Reject** buttons per candidate (confirmation modal).
- **Promotion history** — timeline with approver, metrics, fingerprint, one-click rollback.
- **Force retrain** — triggers the Distillation Worker immediately for a chosen model.
- **Verdict log inspector** — top divergence types, per-model counts, sample rows.

---

## 6. Data Flow

### Flow A — Verdict capture (hot path, zero added latency)

```
Request → ONNX inference (unchanged)
       → VerdictBuffer.record(ModelVerdict(...))   # fire-and-forget enqueue
       → returns immediately
Background VerdictFlushWorker:
  consume queue → batch 50–500 entries → single SQLite transaction every ~1s
```

- In-memory bounded deque (10k ceiling).
- Overflow drops oldest, increments `verdict_buffer_dropped_total` metric.
- Same pattern as the existing `WALWriter`.

### Flow B — Harvester (ground-truth enrichment, deferred async)

Runs post-response in a background task. Harvesters consume from an async queue populated by the response pipeline's `finally` block. Each harvester computes its signal and writes back to the original verdict row via `UPDATE onnx_verdicts SET divergence_signal=?, divergence_source=? WHERE id=?`.

### Flow C — Training → Shadow → Promotion (offline cadence)

```
Scheduler (nightly OR trigger) → wake Distillation Worker
  → Query verdict log for divergences since last successful training
  → Dedupe, class-balance, build dataset
  → SHA3 fingerprint → write training_dataset_fingerprint Walacor event
  → Train in thread-pool executor (sklearn)
  → Export .onnx via skl2onnx → candidates/
  → Write candidate_created Walacor event
  → Enable shadow mode

Shadow (on live traffic, non-blocking):
  → Each inference: production session + thread-pool candidate session in parallel
  → Log both predictions to shadow_comparisons
  → At target sample size: compute metrics, write shadow_validation_complete event
  → If auto_promote AND gate passes: auto-promote
  → Else: surface in dashboard for human approval

Promotion:
  → Acquire per-model asyncio.Lock
  → Write model_promoted Walacor event (MUST succeed before swap)
  → os.rename atomic swap (candidates/ → production/, production/ → archive/)
  → Signal InferenceSession consumers to reload
```

---

## 7. Error Handling

### Principles

1. **Inference path is sacred.** No intelligence-layer failure can add latency, block a request, or cause inference errors.
2. **Production model is immutable during training.** No mutation until a successful promotion event.
3. **Bad candidates cannot reach production.** Layered quality gates (load, offline sanity, live shadow, statistical significance, human approval).
4. **Audit chain is authoritative.** Lifecycle events — especially promotion — require successful Walacor write before taking effect.
5. **Human oversight is the default.** `auto_promote` is opt-in per model; un-approved candidates queue indefinitely.

### Failure mode matrix

**A — Hot path protection**
- Verdict buffer overflow → drop oldest, emit metric, alert on sustained drops.
- SQLite write failure → in-memory buffer, bounded retry, emit metric.
- Shadow inference exception → caught silently; candidate `inference_error_count` increments; >5% rate → auto-reject.
- Walacor unreachable during harvest → SQLite still writes; Walacor events buffer + retry.

**B — Training failures**
- sklearn / skl2onnx exception → log ERROR, no production change, dashboard shows `last_training_status: failed`.
- Empty or too-small dataset (< 100 divergences) → skip cycle, reschedule.
- Thread pool exhausted → queue and retry; never synchronously block the event loop.

**C — Candidate quality gates**
- **Gate 1 — Load:** `InferenceSession` + tensor shape match. Failure → candidate moved to `archive/failed/`, `model_rejected` event written.
- **Gate 2 — Offline sanity:** held-out test set (50 known-correct examples per class), ≥ 70% accuracy required.
- **Gate 3 — Live shadow:** ≥ 1000 samples, accuracy delta ≥ 2%, disagreement < 40%, error rate < 5%, McNemar p < 0.05.
- **Gate 4 — Human approval** unless `auto_promote=true`.

**D — Lifecycle event durability**
- Walacor write fails → retry with backoff; promotion swap blocked until confirmed. Dashboard shows "Awaiting audit chain confirmation — retrying…".
- Other lifecycle events → buffered retry queue (same pattern as execution record writes).

**E — Promotion atomicity**
- Per-model `asyncio.Lock` serializes promotions.
- Concurrent clicks → idempotency key on the promotion event prevents double-write.
- New candidate arriving mid-shadow → old candidate discarded, shadow restarts.
- Rollback → archive integrity check before swap; blocked if archived file missing.

**F — Adversarial robustness**
- Class balancing in dataset construction.
- Per-session contribution capped at 10% of training data.
- Dataset fingerprint on Walacor enables forensic review; rollback is one click.
- `auto_promote=false` default is the ultimate backstop.

---

## 8. Testing

**Chaos-style validation, not a pre-planned test matrix.** Once implemented:

- Exercise with weird real traffic and unusual provider responses.
- Inject adversarial patterns (flood one class, poison labels, inputs near decision boundary).
- Pull Walacor offline mid-promotion.
- Fill disk, corrupt candidate `.onnx`, kill gateway mid-training, kill it mid-shadow.
- Observe what breaks and harden iteratively.

Formal unit/integration test plan deferred until chaos findings inform what's actually worth covering.

---

## 9. Compliance Posture

- **EU AI Act Art. 12 (logging):** every production model change has training dataset fingerprint, shadow metrics, and named approver anchored to the Walacor chain.
- **Reproducibility:** SHA3 dataset fingerprint + row_ids enables full retrain verification ("prove model v3 was trained on exactly these rows").
- **Auditability:** `onnx_lifecycle_event` ETId provides a single queryable stream of model changes.
- **Rollback:** one-click return to any archived model version.
- **Human-in-loop default** aligns with Art. 14 (human oversight).

---

## 10. Configuration

New `WALACOR_*` env vars:

| Var | Default | Purpose |
|---|---|---|
| `WALACOR_INTELLIGENCE_ENABLED` | `true` | Master toggle for the self-learning loop |
| `WALACOR_INTELLIGENCE_DB_PATH` | `{wal_path}/intelligence.db` | SQLite path for verdicts + shadow logs |
| `WALACOR_ONNX_MODELS_BASE_PATH` | `src/gateway/models/` | Root for `production/` `candidates/` `archive/` |
| `WALACOR_VERDICT_RETENTION_DAYS` | `30` | TTL sweeper for verdict log |
| `WALACOR_DISTILLATION_SCHEDULE_CRON` | `0 2 * * *` | Nightly 2am |
| `WALACOR_DISTILLATION_MIN_DIVERGENCES` | `500` | Auto-trigger threshold |
| `WALACOR_SHADOW_SAMPLE_TARGET` | `1000` | Samples before promotion decision |
| `WALACOR_SHADOW_MIN_ACCURACY_DELTA` | `0.02` | 2% accuracy improvement required |
| `WALACOR_SHADOW_MAX_DISAGREEMENT` | `0.40` | Sanity gate — reject candidate if exceeded |
| `WALACOR_SHADOW_MAX_ERROR_RATE` | `0.05` | Reject candidate if inference errors exceed |
| `WALACOR_AUTO_PROMOTE_MODELS` | `""` | Comma-separated model names eligible for auto-promotion |
| `WALACOR_TEACHER_LLM_URL` | `""` | Optional LLM for Intent ground-truth sampling |
| `WALACOR_TEACHER_LLM_SAMPLE_RATE` | `0.01` | 1% sampling for low-confidence Intent verdicts |

---

## 11. Out of Scope (this phase)

- **Layer 2 — policy-engine extensions for Self-acting (B):** verdicts will become conditions available to policy rules; separate design doc.
- **Layer 3 / Option C (adaptive thresholds):** deferred except for per-model confidence calibration, which ships as a published table alongside each trained model.
- **Options 1 and 3 architectures:** explicitly rejected for this phase.
- **Batched ONNX inference across concurrent requests:** possible future optimization.
- **Multi-variate anomaly detection as an ONNX model:** possible future layer.
- **Consistency tracker as an ONNX embedding model:** possible future replacement for the current TF-IDF approach.

---

## 12. Follow-up

- Implementation plan via `superpowers:writing-plans`.
- Layer 2 design: "ONNX verdicts as policy conditions".
- Eventual per-model confidence calibration publishing format.
