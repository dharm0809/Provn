# Intelligence Loop Hardening — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the architectural gaps in the ONNX self-learning loop so a regressed model can't stay live unnoticed, hot-path inference can't stall a request, and silent failures (missing files, leaked sessions, lost candidates) become loud.

**Architecture:** Three layers of fix, ordered by risk × isolation:
- **Phase 1 — Hot-path safety net.** Wrap inference in `asyncio.timeout`, surface model-file health in the dashboard, evict stale `InferenceSession` on reload. Small, isolated, ships in a day.
- **Phase 2 — Close the feedback loop.** Rolling-accuracy drift monitor that triggers retrain on regression, post-promotion validation that auto-rolls back when live performance drops below the previous-version baseline, populate `divergence_signal` from real outcomes so the gate can be retrained against ground truth.
- **Phase 3 — Hygiene + tests.** Concurrent-train lock, dashboard health status field, end-to-end test that exercises train → shadow → promote → reload → regress → rollback in a single test.

**Tech Stack:** Python 3.12, `onnxruntime`, `asyncio`, SQLite (intelligence DB at `data/intelligence.db`-ish path — verify), pytest with `@pytest.mark.anyio`. Existing modules under `src/gateway/intelligence/`.

**Honesty caveat:** Line numbers below come from an explorer agent's read, not a fresh audit. **Every task starts with a "verify shape" read step** before editing — do not trust the snippets blindly.

---

## Verification pass (2026-04-26) — plan amendments

A second-pass code audit verified each load-bearing claim in this plan against actual source. Key deltas vs. the original draft:

| Original claim | Actual finding | Plan impact |
|---|---|---|
| Harvesters in `intelligence/harvesters/` are mostly stubs | **All three (`intent.py`, `safety.py`, `schema_mapper.py`) are fully wired** with real divergence-signal logic. `base.py` is the framework. | **Task 2.3 collapses.** Work is now "verify orchestrator feeds signals at right pipeline points + add coverage metric," not "build harvesters." |
| `force_cycle()` race is a Phase 3 hygiene issue | `worker.py:134–141` `force_cycle()` has **no lock**. Two concurrent calls write the same candidate dir → live bug under load. | **Promoted to Phase 1 as Task 1.4.** |
| `registry.rollback(model)` may not exist; add it | **Already exists** at `registry.py:255–273`, but takes `(model, archived_filename)`, not `(model)`. | Task 2.2 keeps current API; validator must look up the previous archived filename before calling. |
| `IntelligenceDB.accuracy_in_window(model, start, end)` exists | **Does NOT exist.** No rolling-accuracy helper today. | **New Task 2.0** added: ship the DB helper before drift monitor. |
| `list_production_models` has `status`/`error` fields to dashboard-render | **Does NOT exist.** Returns `{model_name, path, size_bytes, mtime, generation, last_promotion}` only. | Task 1.2 explicitly adds the fields as part of the change. |
| Schema mapper transforms request fields BEFORE policy → structural concern | **FALSE.** Schema mapper runs post-response (`mapper.py:2258`), policy is pre-inference (`orchestrator.py:985`). | Concern dropped from the narrative. The mapper is observer-only and post-hoc. No structural fix needed. |
| `update_divergence_signal()` DB wrapper method | Does NOT exist. Harvesters use raw SQL UPDATEs (`schema_mapper.py:76–82`). | Decision deferred to Task 2.3: stick with raw SQL (current pattern) unless we want consistency. Not a blocker. |

**Confirmed TRUE without amendment:**
- All three hot-path inference call sites (`intent.py:214`, `safety_classifier.py:276`, `mapper.py:264`) are sync `session.run()` with no timeout.
- Pipeline callers are async — `asyncio.timeout` wrap is feasible.
- `ShadowRunner._sessions` (`shadow.py:61`) is keyed `(model, version)` and never evicted.
- `reload.py:44–92` is the right place to call eviction.
- `_should_trigger` (`worker.py:291–298`) gates retrain on `divergence_signal IS NOT NULL` count + 1h `poll_interval_s` (`worker.py:76`).
- Observer-first invariant **HELD** — `safety_classifier.py:345–356` explicitly documents observer-only with comment, and pre-inference policy precedes any ONNX-driven mutation.
- `verdict.divergence_signal` exists (`types.py:30`) and column is in `db.py:27`.
- `lifecycle_events_mirror` (`db.py:56–68`) supports rollback events.

The amendments below replace the affected task bodies. Unaffected tasks are unchanged.

---

## Pre-flight (once, before any task)

**Step 0.1: Pull the current state of the intelligence subsystem into your head.**

Run, in order:
```bash
ls src/gateway/intelligence/
ls src/gateway/intelligence/distillation/
ls src/gateway/intelligence/harvesters/
ls src/gateway/intelligence/sanity_tests/
ls tests/unit/intelligence/ 2>/dev/null || ls tests/unit/ | grep -i intelligence
```

Read these files end-to-end before writing any code:
- `src/gateway/intelligence/shadow.py`
- `src/gateway/intelligence/shadow_gate.py`
- `src/gateway/intelligence/registry.py`
- `src/gateway/intelligence/reload.py`
- `src/gateway/intelligence/distillation/worker.py`
- `src/gateway/intelligence/api.py`
- `src/gateway/intelligence/types.py`
- `src/gateway/intelligence/verdict_buffer.py`
- `src/gateway/intelligence/verdict_flush.py`
- `src/gateway/classifier/intent.py`
- `src/gateway/content/safety_classifier.py`
- `src/gateway/schema/mapper.py`

**Step 0.2: Verify the test layout.**

```bash
find tests -name "test_*intelligence*" -o -name "test_onnx*" -o -name "test_shadow*" -o -name "test_distillation*"
```

Confirm `@pytest.mark.anyio` is the project convention (it is per CLAUDE.md). Set up the worktree if you're following parallel-session execution.

**Step 0.3: Capture a baseline.**

```bash
pytest tests/unit/ -k intelligence -v 2>&1 | tee /tmp/baseline.log
```

Note the pass count. Phases 1 and 2 must not regress this number.

---

## Phase 1 — Hot-path safety net

Three independent tasks. Can ship in any order, but doing 1.1 first is recommended (smallest blast radius, immediate user benefit).

### Task 1.1: Wrap hot-path ONNX inference in `asyncio.timeout`

**Why:** A slow or malformed candidate model can hang `InferenceSession.run` indefinitely. Today there is no timeout on the request hot path. A user request stalls until the underlying httpx timeout fires (seconds), and even then the inference thread keeps running.

**Files (verify before editing):**
- Modify: `src/gateway/classifier/intent.py` — `_tier2_onnx` (≈ line 202)
- Modify: `src/gateway/content/safety_classifier.py` — `verify` method
- Modify: `src/gateway/schema/mapper.py` — main inference call site
- Test: `tests/unit/intelligence/test_inference_timeout.py` (new)

**Step 1: Read the three call sites and confirm current shape**

```bash
grep -n "InferenceSession.run\|self\._onnx_session\.run\|self\._session\.run" src/gateway/classifier/intent.py src/gateway/content/safety_classifier.py src/gateway/schema/mapper.py
```

Record actual function signatures. If any call site is *already* async or already wrapped, skip that file.

**Step 2: Write the failing test**

Create `tests/unit/intelligence/test_inference_timeout.py`:

```python
import asyncio
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_intent_classifier_falls_back_when_onnx_hangs():
    """If ONNX inference exceeds the timeout, classifier returns the Tier-1 deterministic result, not a hung future."""
    from gateway.classifier.intent import IntentClassifier

    classifier = IntentClassifier()
    # Replace the loaded session with one whose run() blocks forever.
    blocking_session = MagicMock()
    blocking_session.run.side_effect = lambda *a, **kw: __import__("time").sleep(10)
    classifier._onnx_session = blocking_session

    result = await classifier.classify_async("hello world")
    # Expect Tier-1 fallback result, not a timeout exception bubbling up.
    assert result.tier == 1
    assert result.fallback_reason == "onnx_timeout"
```

(If `classify_async` doesn't exist yet, this test is the spec for adding it. If only sync `classify` exists, write a similar test against an async wrapper you'll add.)

**Step 3: Run it — must FAIL**

```bash
pytest tests/unit/intelligence/test_inference_timeout.py -v
```

Expected: failure (no `classify_async`, or no `fallback_reason`, or it hangs).

**Step 4: Add the timeout wrapper**

In `intent.py`, add a config field `onnx_inference_timeout_ms: int = 100` to `Settings` (or wherever IntentClassifier reads config). Then:

```python
async def classify_async(self, text: str) -> IntentResult:
    tier1 = self._tier1_rules(text)
    if tier1.confidence >= self._tier1_confidence_floor:
        return tier1

    try:
        async with asyncio.timeout(self._timeout_s):
            tier2 = await asyncio.to_thread(self._tier2_onnx, text)
        return tier2
    except (asyncio.TimeoutError, Exception) as exc:
        self._record_fallback(exc)
        return tier1.with_fallback_reason(
            "onnx_timeout" if isinstance(exc, asyncio.TimeoutError) else "onnx_error"
        )
```

Mirror the pattern in `safety_classifier.verify` and `schema/mapper.py`. Keep timeouts configurable per model — safety can be slower than intent.

**Step 5: Wire callers to the async variant**

`grep -rn "classifier.classify(" src/gateway/` — every sync caller becomes `await classifier.classify_async(...)`. The pipeline is already async, so this is mechanical.

**Step 6: Run the test — must PASS**

```bash
pytest tests/unit/intelligence/test_inference_timeout.py -v
```

**Step 7: Run the broader suite — no regressions**

```bash
pytest tests/unit/ -k intelligence -v
```

**Step 8: Add a Prometheus counter**

In `src/gateway/intelligence/_metrics.py` (or wherever the `prometheus_client` registry is exposed; verify):

```python
ONNX_INFERENCE_TIMEOUT_TOTAL = Counter(
    "walacor_onnx_inference_timeout_total",
    "Hot-path ONNX inference exceeded its timeout and fell back to Tier-1 rules",
    ["model"],
)
```

Increment in the except branch above. Surface in `/v1/connections` analyzers tile if not already.

**Step 9: Commit**

```bash
git add src/gateway/classifier/intent.py src/gateway/content/safety_classifier.py src/gateway/schema/mapper.py tests/unit/intelligence/test_inference_timeout.py src/gateway/intelligence/_metrics.py src/gateway/config.py
git commit -m "fix(intelligence): timeout hot-path ONNX inference, fall back to Tier-1 rules"
```

---

### Task 1.2: Surface missing model files in the dashboard + readiness

**Why:** Today, `list_production_models` (`api.py:94–104`) returns `size_bytes: 0` if a `.onnx` file is missing. No error flag. Dashboard renders "0 bytes" and operators never notice. Next request silently falls back to Tier-1.

**Files (verify):**
- Modify: `src/gateway/intelligence/api.py` — `list_production_models`
- Modify: `src/gateway/lineage/dashboard/src/views/Intelligence.jsx` — model row renderer
- Modify: `src/gateway/readiness/feature.py` (or wherever feature checks live) — add a check
- Test: `tests/unit/intelligence/test_api_list_models.py`

**Step 1: Verify the API shape**

```bash
grep -n "list_production_models\|def list_models" src/gateway/intelligence/api.py
```

Read the function. Confirm it stat()s the file and returns size only.

**Step 2: Failing test**

```python
async def test_list_production_models_flags_missing_file(tmp_path):
    """A registered model whose .onnx file was deleted returns status='missing', not size_bytes=0."""
    from gateway.intelligence.api import list_production_models
    from gateway.intelligence.registry import Registry

    reg = Registry(root=tmp_path)
    reg.register("intent", version="v1", path=tmp_path / "intent" / "missing.onnx")
    # File intentionally not created.

    result = await list_production_models(registry=reg)
    intent_row = next(r for r in result if r["model"] == "intent")
    assert intent_row["status"] == "missing"
    assert "missing" in (intent_row.get("error") or "").lower()
```

**Step 3: Run, expect FAIL**

**Step 4: Implement (note: `status` and `error` are NEW fields — add to the existing return shape, don't replace it)**

The current `list_production_models` returns `{model_name, path, size_bytes, mtime, generation, last_promotion}`. Add two new keys, keep all existing keys (dashboard JSX may already consume them):

```python
async def list_production_models(registry: Registry) -> list[dict]:
    rows = []
    for model_name, version, path in registry.iter_production():
        # ... existing extraction of mtime, generation, last_promotion ...
        try:
            stat = path.stat()
            status = "loaded"
            size = stat.st_size
            error = None
        except FileNotFoundError:
            status = "missing"
            size = 0
            error = f"file not found: {path}"
        except PermissionError as exc:
            status = "unreadable"
            size = 0
            error = str(exc)
        rows.append({
            "model_name": model_name,        # existing key — keep
            "path": str(path),               # existing
            "size_bytes": size,              # existing
            "mtime": ...,                    # existing — keep
            "generation": ...,               # existing — keep
            "last_promotion": ...,           # existing — keep
            "status": status,                # NEW
            "error": error,                  # NEW
        })
    return rows
```

Update any TypedDict / Pydantic response schema next to the function. Search for callers (`grep -rn "list_production_models"`) and confirm none break on the new keys (additive change is safe).

**Step 5: Run test — PASS**

**Step 6: Wire to readiness**

Add a check `INT-09: production_models_present` (or next free ID — verify) under integrity. Severity = `int` (these are local invariants the gateway can assert). Red if any registered model has `status != "loaded"`. Test in `tests/unit/readiness/test_integrity.py`.

**Step 7: Wire dashboard**

In `Intelligence.jsx`, render a status pill next to each model: green="loaded", red="missing", amber="unreadable". `status === "missing"` rows should also show the path so the operator can fix it.

**Step 8: Commit**

```bash
git commit -m "feat(intelligence): surface missing/unreadable model files in api + readiness + dashboard"
```

---

### Task 1.3: Evict stale `InferenceSession` after promotion

**Why:** `ShadowRunner._sessions: dict[tuple[str, str], Any]` (`shadow.py:61`) keeps every session ever loaded. Each promotion adds a new entry; old ones are never freed until process restart. With frequent shadow churn, this leaks RAM.

**Files (verify):**
- Modify: `src/gateway/intelligence/shadow.py` — `_sessions` dict + reload path
- Modify: `src/gateway/intelligence/reload.py` — call site after generation bump
- Test: `tests/unit/intelligence/test_session_eviction.py`

**Step 1: Read shadow.py:60–95 + reload.py:44–92.** Confirm `_sessions` keying scheme. Likely `(model_name, version)` so an in-flight shadow run on an old version doesn't crash mid-call.

**Step 2: Failing test**

```python
async def test_shadow_runner_evicts_old_session_on_reload(tmp_path):
    runner = ShadowRunner(...)
    runner._load_session("intent", "v1", path=...)
    runner._load_session("intent", "v2", path=...)
    assert len(runner._sessions) == 2

    runner.evict_old_sessions(model="intent", current_version="v2")
    assert ("intent", "v1") not in runner._sessions
    assert ("intent", "v2") in runner._sessions
```

**Step 3: Implement**

```python
def evict_old_sessions(self, model: str, current_version: str) -> int:
    """Drop all cached sessions for `model` whose version != current_version. Returns count evicted."""
    stale = [(m, v) for (m, v) in self._sessions if m == model and v != current_version]
    for key in stale:
        sess = self._sessions.pop(key, None)
        # onnxruntime InferenceSession has no explicit close(); drop reference and GC handles it.
        del sess
    return len(stale)
```

**Step 4: Wire the caller**

In `reload.py`, after a successful generation bump:

```python
runner.evict_old_sessions(model=name, current_version=new_version)
```

**Caveat to handle:** If a shadow run is *currently iterating* with the old version, eviction mid-flight breaks it. Guard with a small in-flight counter or move eviction to "after N seconds of no shadow calls on the old version." Decide based on what shadow.py's call shape allows. The simple version (evict immediately) is acceptable if shadow runs are short-lived (< 1s).

**Step 5: Test, commit**

```bash
git commit -m "fix(intelligence): evict stale InferenceSession after promotion to bound memory"
```

---

### Task 1.4: Lock concurrent training (promoted from Phase 3)

**Why:** `worker.py:134–141` `force_cycle()` has no lock guard. Two concurrent calls (manual dashboard force-retrain + scheduled poll, or two tabs hitting the API) both enter `_run_cycle()`, both call trainers, both write candidates with timestamp-based versions to the same dir. Second one silently overwrites the first or they corrupt each other's output. **This is a live bug under load**, not a hygiene concern — promoting to Phase 1.

**Files (verify):**
- Modify: `src/gateway/intelligence/distillation/worker.py` — `__init__` adds locks, `force_cycle` + `_run_cycle` acquire
- Test: `tests/unit/intelligence/test_concurrent_train.py` (new)

**Step 1: Read `worker.py:130–230` and confirm the cycle entry points (force_cycle, scheduled poll loop, anything else).**

**Step 2: Failing test**

```python
@pytest.mark.anyio
async def test_concurrent_force_cycle_serializes_per_model(intelligence_db, fake_trainer):
    """Two simultaneous force_cycle('intent') calls must not run trainers concurrently."""
    worker = DistillationWorker(db=intelligence_db, trainer=fake_trainer, ...)
    fake_trainer.delay_s = 0.5  # so both calls overlap

    results = await asyncio.gather(
        worker.force_cycle(model="intent"),
        worker.force_cycle(model="intent"),
        return_exceptions=False,
    )
    # Exactly one cycle should have run; the other should report "already_running".
    statuses = sorted(r.status for r in results)
    assert statuses == ["already_running", "completed"]
    assert fake_trainer.invocation_count == 1
```

**Step 3: Implement**

```python
class DistillationWorker:
    def __init__(self, ...):
        ...
        self._cycle_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, model: str) -> asyncio.Lock:
        lock = self._cycle_locks.get(model)
        if lock is None:
            lock = asyncio.Lock()
            self._cycle_locks[model] = lock
        return lock

    async def force_cycle(self, *, model: str | None = None, reason: str = "manual") -> CycleResult:
        target = model or self._default_model
        lock = self._lock_for(target)
        if lock.locked():
            return CycleResult(status="already_running", model=target, reason=reason)
        async with lock:
            return await self._run_cycle(model=target, reason=reason)
```

The scheduled poll loop must use the same `_lock_for()` pattern around its cycle invocation.

**Note:** locks are per-model so concurrent training on `intent` and `safety` is still fine — they're independent.

**Step 4: Test, commit**

```bash
git commit -m "fix(intelligence): per-model lock on force_cycle prevents concurrent trainer race"
```

---

**Phase 1 done.** You now have hot-path timeouts, missing-model visibility, bounded session memory, and serialized training. Run the full suite:

```bash
pytest tests/unit/ -v
```

Compare to baseline. Should be the same count + 3-5 new tests.

---

## Phase 2 — Close the feedback loop

This is the architectural fix. Tasks ordered: **2.0 ships the DB primitive both 2.1 and 2.2 depend on**, then 2.1 (drift monitor) → 2.2 (post-promotion validator) → 2.3 (harvester wiring verification & coverage metric).

### Task 2.0: Add `IntelligenceDB.accuracy_in_window` (prerequisite for 2.1 + 2.2)

**Why:** Both the drift monitor and the post-promotion validator need to ask "what's the accuracy of model M, version V, in time window [start, end]?" That helper does not exist today. Build it once, both consumers use it.

**Files:**
- Modify: `src/gateway/intelligence/db.py` — add method
- Test: `tests/unit/intelligence/test_db_accuracy_window.py` (new)

**Step 1: Read `db.py` end-to-end. Confirm:**
- `onnx_verdicts` table has `timestamp` column type (string ISO? int unix? — match the existing pattern)
- `model`, `version`, `prediction`, `divergence_signal` column names
- Whether the connection is sync sqlite3 or aiosqlite

**Step 2: Define the return shape**

```python
@dataclass(frozen=True)
class AccuracySnapshot:
    model: str
    version: str | None       # None = aggregate across versions
    sample_count: int
    accuracy: float           # 0.0 .. 1.0
    coverage: float           # fraction of samples with divergence_signal != null
    window_start: datetime
    window_end: datetime
```

**Step 3: Failing test**

```python
def test_accuracy_in_window_uses_divergence_signal_when_available(intelligence_db):
    db = intelligence_db
    # 10 verdicts: 8 agree with divergence_signal (correct), 2 disagree (wrong).
    seed_verdicts(db, model="intent", version="v1",
                  predictions=["A"]*10,
                  divergence_signals=["A"]*8 + ["B"]*2)
    snap = db.accuracy_in_window("intent", version="v1",
                                 start=NOW - timedelta(hours=1),
                                 end=NOW)
    assert snap.sample_count == 10
    assert snap.accuracy == pytest.approx(0.8)
    assert snap.coverage == 1.0


def test_accuracy_in_window_falls_back_to_tier1_agreement_when_signal_sparse(intelligence_db):
    """When < 30% of verdicts have a divergence_signal, accuracy is computed against tier1_label instead."""
    ...
```

**Step 4: Implement**

Definition of "correct":
1. If `divergence_signal IS NOT NULL` → correct iff `prediction == divergence_signal` (the harvester's ground-truth label).
2. Else fall back to `prediction == tier1_label` if tier1_label is available.
3. If neither available, exclude from sample count.

```python
def accuracy_in_window(
    self,
    model: str,
    *,
    version: str | None = None,
    start: datetime,
    end: datetime,
) -> AccuracySnapshot:
    where = ["model = ?", "timestamp >= ?", "timestamp < ?"]
    args = [model, start.isoformat(), end.isoformat()]
    if version is not None:
        where.append("version = ?")
        args.append(version)
    sql = f"""
        SELECT prediction, divergence_signal, tier1_label
        FROM onnx_verdicts
        WHERE {' AND '.join(where)}
    """
    rows = self._conn.execute(sql, args).fetchall()
    correct = 0
    counted = 0
    with_signal = 0
    for pred, sig, t1 in rows:
        if sig is not None:
            with_signal += 1
            counted += 1
            if pred == sig: correct += 1
        elif t1 is not None:
            counted += 1
            if pred == t1: correct += 1
    return AccuracySnapshot(
        model=model, version=version,
        sample_count=counted,
        accuracy=(correct / counted) if counted else 0.0,
        coverage=(with_signal / len(rows)) if rows else 0.0,
        window_start=start, window_end=end,
    )
```

If `tier1_label` doesn't exist as a column, drop that branch and only use `divergence_signal`. Drift monitor's threshold logic must then require `coverage >= 0.30` before trusting the snapshot — otherwise skip.

**Step 5: Test, commit.**

```bash
git commit -m "feat(intelligence): add IntelligenceDB.accuracy_in_window helper for drift + validation"
```

---

### Task 2.1: Rolling-accuracy drift monitor

**Why:** Today `distillation/worker.py:_should_trigger` (line 111) only fires retrain when `len(verdicts) >= min_divergences`. There is no monitor that says "production accuracy dropped 5% in the last 24h, retrain now." A regressed model sits live until the next 1h tick AND enough divergence rows accumulate.

**Approach:** New module `src/gateway/intelligence/drift_monitor.py` that runs as a periodic asyncio task. Reads `onnx_verdicts` for the last N hours, computes accuracy (or proxy: agreement with deterministic Tier-1 + post-hoc signal where available), compares to a rolling baseline, emits a `drift_detected` event when delta exceeds threshold.

**Files:**
- Create: `src/gateway/intelligence/drift_monitor.py`
- Modify: `src/gateway/intelligence/distillation/worker.py` — subscribe to drift events
- Modify: `src/gateway/main.py` — start the monitor task on startup
- Modify: `src/gateway/config.py` — `drift_window_hours`, `drift_accuracy_drop_threshold`, `drift_check_interval_s`
- Test: `tests/unit/intelligence/test_drift_monitor.py`

**Step 1: Define the signal contract**

In `drift_monitor.py`:

```python
@dataclass(frozen=True)
class DriftSignal:
    model: str
    window_hours: int
    baseline_accuracy: float
    current_accuracy: float
    delta: float
    sample_count: int
    detected_at: datetime
```

**Step 2: Failing test**

```python
async def test_drift_monitor_emits_signal_when_accuracy_drops(intelligence_db):
    """Inject 1000 verdicts where the last 200 have low accuracy. Monitor must emit a DriftSignal."""
    db = intelligence_db
    seed_verdicts(db, model="intent", count=800, accuracy=0.95)
    seed_verdicts(db, model="intent", count=200, accuracy=0.80, recent=True)

    monitor = DriftMonitor(db=db, window_hours=1, threshold=0.05)
    signals = []
    monitor.on_drift(signals.append)
    await monitor.check_once()

    assert len(signals) == 1
    assert signals[0].model == "intent"
    assert signals[0].delta >= 0.05
```

**Step 3: Implement**

```python
class DriftMonitor:
    def __init__(self, db: IntelligenceDB, *, window_hours: int, threshold: float, check_interval_s: int = 600):
        self._db = db
        self._window = timedelta(hours=window_hours)
        self._threshold = threshold
        self._interval = check_interval_s
        self._listeners: list[Callable[[DriftSignal], None]] = []
        self._task: asyncio.Task | None = None

    def on_drift(self, callback): self._listeners.append(callback)

    async def start(self):
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            try:
                await self.check_once()
            except Exception as exc:
                logger.exception("drift monitor cycle failed: %s", exc)
            await asyncio.sleep(self._interval)

    async def check_once(self):
        now = datetime.utcnow()
        for model in self._db.list_active_models():
            recent = self._db.accuracy_in_window(model, start=now - self._window, end=now)
            baseline = self._db.accuracy_in_window(model, start=now - self._window * 7, end=now - self._window)
            if recent.sample_count < 50 or baseline.sample_count < 50:
                continue  # not enough data
            delta = baseline.accuracy - recent.accuracy
            if delta >= self._threshold:
                signal = DriftSignal(
                    model=model,
                    window_hours=int(self._window.total_seconds() / 3600),
                    baseline_accuracy=baseline.accuracy,
                    current_accuracy=recent.accuracy,
                    delta=delta,
                    sample_count=recent.sample_count,
                    detected_at=now,
                )
                for cb in self._listeners:
                    try: cb(signal)
                    except Exception: logger.exception("drift listener failed")
```

**Note on "accuracy"**: until 2.3 lands, "accuracy" here means "agreement rate with a chosen ground-truth proxy" — could be Tier-1 deterministic where available, or the harvester signal once 2.3 wires it. **Document this clearly in the module docstring** and in the test.

**Step 4: Wire the listener**

In `distillation/worker.py`, register a callback that forces a cycle:

```python
def attach_drift_monitor(self, monitor: DriftMonitor):
    monitor.on_drift(lambda sig: self.force_cycle(model=sig.model, reason="drift"))
```

`force_cycle` should set a flag the main loop reads on the next tick. Don't block the drift-monitor callback on training.

**Step 5: Test full chain**

```python
async def test_drift_signal_triggers_distillation_cycle(intelligence_db):
    monitor = DriftMonitor(...)
    worker = DistillationWorker(...)
    worker.attach_drift_monitor(monitor)

    seed_drift(intelligence_db, "intent")
    await monitor.check_once()

    assert worker.next_cycle_reason == "drift"
```

**Step 6: Start in main.py, expose in /v1/readiness**

Add a check `FEAT-DRIFT-01: drift_monitor_running` — green when the task exists and last check ran within `2 * check_interval_s`. Catch a stuck monitor.

**Step 7: Commit**

```bash
git commit -m "feat(intelligence): rolling-accuracy drift monitor triggers distillation on regression"
```

---

### Task 2.2: Post-promotion validation + auto-rollback

**Why:** When a candidate promotes, `shadow_gate` says "shadow accuracy ≥ production accuracy on validation set." But validation sample distribution may not match production. A model can pass the gate, promote, and immediately regress in production. There is no edge in today's loop that says "the candidate that promoted last hour is now performing worse than the version it replaced — roll it back."

**Approach:** A `PostPromotionValidator` task runs every `validation_interval_s` (default 600). For each model promoted within the last `validation_window_h` (default 24): compute live accuracy of the new version, compare to archived previous version's last-N-hours accuracy. If new version is worse by >= `rollback_threshold`, call `registry.rollback(model)`.

**Files:**
- Create: `src/gateway/intelligence/post_promotion_validator.py`
- Modify: `src/gateway/intelligence/registry.py` — add `rollback(model)` if not present (verify; the explorer found `rollback` is exposed in api.py:27 but didn't confirm registry-level support)
- Modify: `src/gateway/main.py` — start the validator task
- Test: `tests/unit/intelligence/test_post_promotion_validation.py`

**Step 1: Confirm `registry.rollback` API shape**

`registry.py:255–273` already implements `async def rollback(self, model: str, archived_filename: str)`. **It takes the archived filename, not just the model.** This means the validator must look up which archived version it wants to roll back to before calling. Read those lines to confirm the exact signature and what it expects in `archived_filename`.

If you want a `rollback(model)`-only convenience that picks "the most recent archived version that isn't the current production," add it as a thin wrapper around the existing method — don't replace the explicit-filename API, some callers (the API endpoint) may rely on it.

**Step 2: Failing test for the validator**

```python
async def test_post_promotion_validator_rolls_back_regressed_candidate(intelligence_db, registry):
    # Promote v2 over v1 1 hour ago. Seed verdicts where v2 is 10% worse than v1's pre-promotion baseline.
    registry.promote("intent", "v2")
    seed_verdicts(intelligence_db, model="intent", version="v2", accuracy=0.80, count=500, recent=True)
    archive_baseline(intelligence_db, model="intent", version="v1", accuracy=0.92, count=500)

    validator = PostPromotionValidator(db=intelligence_db, registry=registry, threshold=0.05)
    await validator.check_once()

    assert registry.current("intent") == "v1"  # rolled back
    events = intelligence_db.lifecycle_events(model="intent")
    assert any(e.kind == "rollback" and e.reason.startswith("regression") for e in events)
```

**Step 3: Implement**

```python
class PostPromotionValidator:
    def __init__(self, db, registry, *, threshold: float, window_h: int = 24, interval_s: int = 600, min_samples: int = 200):
        ...

    async def check_once(self):
        now = datetime.utcnow()
        for model, promoted_at, current_version, previous_version in self._db.recent_promotions(within=self._window):
            if promoted_at > now - timedelta(minutes=15):
                continue  # too soon to judge
            current_acc = self._db.accuracy_for_version(model, current_version, since=promoted_at)
            previous_acc = self._db.archived_accuracy(model, previous_version)
            if current_acc.sample_count < self._min_samples:
                continue
            if previous_acc.accuracy - current_acc.accuracy >= self._threshold:
                logger.warning("auto-rollback: %s v=%s regression %.3f → %.3f",
                               model, current_version, previous_acc.accuracy, current_acc.accuracy)
                self._registry.rollback(model, reason=f"regression delta={previous_acc.accuracy - current_acc.accuracy:.3f}")
                self._db.write_lifecycle_event(model=model, kind="rollback", reason=f"regression {current_version}→{previous_version}")
```

**Step 4: Guardrails**

- **Cooldown:** after a rollback, suppress further auto-rollback for that model for `cooldown_h` (default 12). Otherwise a flapping candidate causes thrash.
- **Manual override:** if `lifecycle_events_mirror` shows a manual `promote` with `force=true`, skip auto-rollback for that promotion.

**Step 5: Test the cooldown**

```python
async def test_validator_respects_cooldown_after_rollback(...):
    ...
    await validator.check_once()  # rolls back
    seed_more_regression(...)
    await validator.check_once()  # must NOT roll back again within cooldown
    assert registry.current("intent") == "v1"
    assert count_rollback_events(...) == 1
```

**Step 6: Wire to dashboard**

Surface in `Intelligence.jsx` under each model: "Last rollback: 2h ago — reason: regression 0.92 → 0.80". Same data also goes into `/v1/connections.intelligence_worker` tile so on-call sees it.

**Step 7: Commit**

```bash
git commit -m "feat(intelligence): post-promotion validator with auto-rollback on regression"
```

---

### Task 2.3: Verify harvester wiring + ship signal-coverage metric

**Why (revised after verification):** The harvesters are NOT stubs. `intent.py`, `safety.py`, `schema_mapper.py` all have real divergence-signal logic — `IntentHarvester` tracks LRU sessions and samples teacher LLM labels, `SafetyHarvester` compares LlamaGuard vs ONNX-student and writes the teacher's category, `SchemaMapperHarvester` resolves overflow keys via fallback rules. The framework (`base.py`) defines `HarvesterRunner` as an async queue.

The real risk is **whether the orchestrator actually feeds signals into `HarvesterRunner` at the right pipeline points.** That's the one remaining uncertainty. Plus: we have no metric showing what fraction of verdicts ever receive a divergence_signal, so 2.1 + 2.2 might be operating on tiny coverage and we wouldn't know.

**Files (verify):**
- Read-only audit: `src/gateway/intelligence/harvesters/{base.py,intent.py,safety.py,schema_mapper.py}`
- Read-only audit: `src/gateway/pipeline/orchestrator.py` — find every place a `HarvesterRunner` is instantiated and a signal is enqueued
- Modify: `src/gateway/intelligence/db.py` — add `signal_coverage_ratio(model, window)` (or fold into `accuracy_in_window` — already returns `coverage`)
- Modify: `src/gateway/intelligence/_metrics.py` (or wherever Prometheus metrics live) — register the gauge
- Test: `tests/unit/intelligence/test_harvester_wiring.py`

**Step 1: Trace each harvester end-to-end.**

For each of `IntentHarvester`, `SafetyHarvester`, `SchemaMapperHarvester`:
- Find the `process()` (or equivalent) method.
- Find every call site in the pipeline that feeds it. `grep -rn "harvester_runner\|HarvesterRunner\|harvester_queue" src/gateway/`.
- Confirm the call site fires on every relevant request, not just a sampled subset (or document the sampling).
- Confirm the signal eventually reaches an UPDATE on `onnx_verdicts.divergence_signal`.

Write your findings as comments at the top of `test_harvester_wiring.py`. If any of the three harvesters has no call site, **STOP** and escalate — that's a missing-edge bug, not something to plaster over.

**Step 2: Add the signal-coverage gauge**

```python
# in _metrics.py
ONNX_SIGNAL_COVERAGE_RATIO = Gauge(
    "walacor_intelligence_signal_coverage_ratio",
    "Fraction of verdicts in the last hour that have a populated divergence_signal",
    ["model"],
)
```

Set it from a periodic task (every 60s) that queries `accuracy_in_window` (already returns `coverage`) and writes the value.

**Step 3: Wire test that confirms the harvester loop is closed end-to-end**

```python
@pytest.mark.anyio
async def test_intent_harvester_writes_divergence_signal_on_teacher_disagreement(intelligence_db, fake_pipeline):
    # Send a request through the pipeline, force the teacher to disagree with the student.
    await fake_pipeline.handle(prompt="...", expect_intent="A", teacher_label="B")
    await wait_for_harvester_drain(timeout=2.0)

    rows = intelligence_db.recent_verdicts(model="intent", limit=1)
    assert rows[0].divergence_signal == "B"
```

Repeat for safety and schema_mapper.

**Step 4: Add a readiness check `INT-INTEL-COVERAGE`**

Severity = `warn` (this is a quality signal, not a local invariant). Red if `signal_coverage_ratio < 0.10` over 24h on a model that has had >100 verdicts. Operator action: investigate why teachers aren't running / harvesters aren't draining.

**Step 5: Commit**

```bash
git commit -m "feat(intelligence): signal-coverage metric + harvester end-to-end test + readiness check"
```

**Note:** if Step 1 reveals a missing call site (a harvester that's never invoked), open a separate task for that fix — it's a one-off integration bug, not a planned design change.

---

**Phase 2 done.** Drift monitor catches regression early, post-promotion validator rolls back automatically, and both decisions use real ground truth from harvesters. The loop is now closed.

---

## Phase 3 — Hygiene + tests

Two short tasks (3.1 was promoted to Phase 1).

### Task 3.2: Dashboard health pill on every model

**Why:** Today the Intelligence tab shows model rows but no glanceable health. After 1.2 + 2.2 land, you have rich state — surface it. One row, one pill: green=loaded+no rollback, amber=recent rollback, red=missing/unreadable.

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Intelligence.jsx`
- Modify: `src/gateway/lineage/dashboard/src/styles/intelligence.css` (or wherever)
- No backend change needed if 1.2 and 2.2 already write the fields.

**Step 1: Verify the API response shape includes status + last_rollback fields.**

**Step 2: Render the pill, commit.**

(No new test — visual change, covered by existing dashboard tests if any. Otherwise, manual verification.)

---

### Task 3.3: End-to-end integration test for the full loop

**Why:** Today, unit tests stub the ONNX layer. There is no test that exercises train → shadow → gate → promote → reload → regress → rollback in one go. This is the single biggest test-coverage gap, and now that Phase 2 exists, this test pins down the architectural contract.

**Files:**
- Create: `tests/integration/test_intelligence_full_loop.py`

**Step 1: Decide on fixture model**

The simplest two-class sklearn model exported to ONNX. Fast to train (< 1s), small file. Commit a tiny generator script in `tests/fixtures/` so the test generates models on demand rather than committing binaries.

**Step 2: Write the test**

```python
@pytest.mark.anyio
async def test_full_intelligence_loop_with_regression_triggers_rollback(tmp_path, intelligence_db):
    """
    Train v1 (good), promote.
    Train v2 (intentionally worse — bias the training data).
    Shadow validates v2 narrowly passes the gate.
    Promote v2.
    Production traffic: v2 regresses.
    Drift monitor fires.
    Post-promotion validator rolls back to v1.
    Assert final registry state is v1, lifecycle events show promote→rollback.
    """
    # Setup
    registry = Registry(root=tmp_path)
    db = intelligence_db
    runner = ShadowRunner(...)
    monitor = DriftMonitor(...)
    validator = PostPromotionValidator(...)
    worker = DistillationWorker(...)

    # Stage 1: train + promote v1
    train_and_promote("intent", version="v1", quality="good", registry=registry, db=db)

    # Stage 2: train + promote v2 (regressed)
    train_and_promote("intent", version="v2", quality="bad", registry=registry, db=db)
    assert registry.current("intent") == "v2"

    # Stage 3: simulate production traffic where v2 underperforms
    inject_traffic(db, model="intent", version="v2", accuracy=0.75, samples=500)

    # Stage 4: run validators
    await monitor.check_once()
    await validator.check_once()

    # Stage 5: assert rollback
    assert registry.current("intent") == "v1"
    events = db.lifecycle_events(model="intent")
    kinds = [e.kind for e in events]
    assert kinds == ["promote", "promote", "rollback"]
```

**Step 3: Run, commit.**

```bash
git commit -m "test(intelligence): end-to-end loop test — promote regression triggers rollback"
```

---

## Done

Final verification:

```bash
pytest tests/ -v 2>&1 | tail -20
git log --oneline 30df3cc..HEAD
```

You should see ~10–14 commits, all green, no regression in the baseline pass count.

**Bonus follow-ups (not in this plan, name only):**
- Latency histogram per ONNX model on hot path (Prometheus Histogram, not just the timeout counter from Task 1.1).
- Distillation worker + rollback events should write to `walacor_writer` so they're in the audit trail too, not just SQLite.
- Run a backfill on `divergence_signal` for the last 30 days of verdicts so drift monitor has historical baseline immediately rather than building one over a week.
- ~~Schema-mapper-before-policy structural concern~~ — verified false in the 2026-04-26 audit; mapper is post-response, not pre-policy. Dropped.
