# Connections Page Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship a live `/v1/connections` endpoint and a new `Connections` dashboard view that surfaces silent failures across 10 gateway subsystems — green/amber/red tiles over an events stream, with a v4-style incident banner when anything is red.

**Architecture:** One new FastAPI endpoint aggregates existing probes (`/health`, `/v1/readiness`, `DefaultResourceMonitor`) plus 5 new bounded-deque instrumentations (Walacor delivery, analyzer fail-opens, tool-loop exceptions, stream interruptions, intelligence worker state). No new storage. Singleflight + 3s TTL cache. Frontend is a verbatim port of `connections-v3.jsx` from the TruzenAI bundle (already sitting in `docs/plans/assets/2026-04-23-connections-truzenai/`), with v4's banner and runbook grafted in.

**Tech Stack:** FastAPI, pytest/anyio, React 18 (existing dashboard), vanilla CSS tokens from `control.css`.

**Design spec:** `docs/plans/2026-04-23-connections-page-design.md`
**Port source:** `docs/plans/assets/2026-04-23-connections-truzenai/project/`

---

## Phase 0 — Orientation (read-only)

### Task 0.1: Re-read the design doc and the pre-ported Connections.jsx

**Files:**
- Read: `docs/plans/2026-04-23-connections-page-design.md`
- Read: `docs/plans/assets/2026-04-23-connections-truzenai/project/src/gateway/lineage/dashboard/src/views/Connections.jsx`
- Read: `docs/plans/assets/2026-04-23-connections-truzenai/project/src/gateway/lineage/dashboard/src/styles/connections.css`
- Read: `docs/plans/assets/2026-04-23-connections-truzenai/project/overview/connections-v4.jsx` (for banner + runbook grafts)
- Read: `src/gateway/lineage/dashboard/src/views/Overview.jsx` (hooks-before-return reference)

No code changes. Goal: confirm you understand (a) the envelope JSON shape, (b) what the port already has, (c) what v4 pieces still need grafting, (d) where hooks must live.

---

## Phase 1 — Backend instrumentation (5 bounded deques)

Each of these 5 tasks is independent and commits separately. Pattern: add a private `collections.deque(maxlen=N)` + a public `snapshot()` accessor. No behavior change.

### Task 1.1: Walacor delivery deque

**Files:**
- Modify: `src/gateway/walacor/client.py`
- Test: `tests/unit/walacor/test_client_delivery_log.py` (new)

**Step 1.1.1 — Write the failing test:**

```python
# tests/unit/walacor/test_client_delivery_log.py
import pytest
from gateway.walacor.client import WalacorClient

@pytest.mark.anyio
async def test_delivery_snapshot_empty_when_no_activity(anyio_backend):
    client = WalacorClient(base_url="http://localhost:9999", api_key="x")
    snap = client.delivery_snapshot()
    assert snap == {
        "success_rate_60s": 1.0,
        "pending_writes": 0,
        "last_failure": None,
        "last_success_ts": None,
        "time_since_last_success_s": None,
    }

@pytest.mark.anyio
async def test_delivery_snapshot_records_outcomes(anyio_backend):
    client = WalacorClient(base_url="http://localhost:9999", api_key="x")
    client._record_delivery("submit_execution", ok=True, detail=None)
    client._record_delivery("submit_execution", ok=False, detail="HTTP 502")
    client._record_delivery("submit_execution", ok=True, detail=None)
    snap = client.delivery_snapshot()
    assert snap["success_rate_60s"] == pytest.approx(2/3, rel=1e-3)
    assert snap["last_failure"]["detail"] == "HTTP 502"
    assert snap["last_success_ts"] is not None
```

**Step 1.1.2 — Run test to verify it fails:**

```
pytest tests/unit/walacor/test_client_delivery_log.py -v
```
Expected: FAIL — `AttributeError: '_record_delivery'` or `'delivery_snapshot'`.

**Step 1.1.3 — Implement in `src/gateway/walacor/client.py`:**

In `WalacorClient.__init__`, add:
```python
from collections import deque
from time import time as _now
self._delivery_log: deque = deque(maxlen=100)  # (ts, op, ok, detail)
```

Add method:
```python
def _record_delivery(self, op: str, *, ok: bool, detail: str | None) -> None:
    self._delivery_log.append((_now(), op, ok, detail))

def delivery_snapshot(self) -> dict:
    now = _now()
    recent = [e for e in self._delivery_log if now - e[0] <= 60.0]
    if not recent:
        return {"success_rate_60s": 1.0, "pending_writes": 0, "last_failure": None,
                "last_success_ts": None, "time_since_last_success_s": None}
    oks = [e for e in recent if e[2]]
    fails = [e for e in recent if not e[2]]
    last_success = max((e[0] for e in self._delivery_log if e[2]), default=None)
    last_failure = next(((e[0], e[1], e[3]) for e in reversed(self._delivery_log) if not e[2]), None)
    return {
        "success_rate_60s": len(oks) / len(recent),
        "pending_writes": 0,  # filled by caller from /health storage block
        "last_failure": {"ts": _iso(last_failure[0]), "op": last_failure[1], "detail": last_failure[2]} if last_failure else None,
        "last_success_ts": _iso(last_success) if last_success else None,
        "time_since_last_success_s": (now - last_success) if last_success else None,
    }
```

Add a tiny `_iso(ts)` helper above the class (or reuse one if it already exists). Then sprinkle `self._record_delivery(...)` calls at the 4 existing outcome sites found during analysis: `_submit` success path, `_submit` exception path, `write_attempt` exception, `write_execution` exception. Each call passes the operation name + ok flag + detail string.

**Step 1.1.4 — Run tests:**

```
pytest tests/unit/walacor/test_client_delivery_log.py -v
pytest tests/unit/walacor/ -v  # regression
```
Expected: PASS, no existing tests broken.

**Step 1.1.5 — Commit:**

```bash
git add src/gateway/walacor/client.py tests/unit/walacor/test_client_delivery_log.py
git commit -m "feat(walacor): record delivery outcomes for connections endpoint"
```

---

### Task 1.2: Analyzer fail-open deque (shared mixin)

**Files:**
- Modify: `src/gateway/content/base.py`
- Modify: `src/gateway/content/llama_guard.py`
- Modify: `src/gateway/content/presidio_pii.py`
- Modify: `src/gateway/content/safety_classifier.py`
- Modify: `src/gateway/content/prompt_guard.py`
- Test: `tests/unit/content/test_analyzer_fail_open_log.py` (new)

**Step 1.2.1 — Write the failing test:**

```python
# tests/unit/content/test_analyzer_fail_open_log.py
from gateway.content.llama_guard import LlamaGuardAnalyzer

def test_analyzer_fail_open_snapshot_empty():
    a = LlamaGuardAnalyzer(enabled=False)
    assert a.fail_open_snapshot() == {"fail_opens_60s": 0, "last_fail_open": None}

def test_analyzer_fail_open_snapshot_records():
    a = LlamaGuardAnalyzer(enabled=False)
    a._record_fail_open("timeout")
    a._record_fail_open("connection refused")
    snap = a.fail_open_snapshot()
    assert snap["fail_opens_60s"] == 2
    assert snap["last_fail_open"]["reason"] == "connection refused"
```

**Step 1.2.2 — Run, expect FAIL.**

**Step 1.2.3 — Implement in `src/gateway/content/base.py`:**

Add to `ContentAnalyzer` base class:
```python
from collections import deque
from time import time as _now

class ContentAnalyzer:
    def __init__(self, *args, **kwargs):
        # ... existing init ...
        self._fail_open_log: deque = deque(maxlen=50)

    def _record_fail_open(self, reason: str) -> None:
        self._fail_open_log.append((_now(), reason))

    def fail_open_snapshot(self) -> dict:
        now = _now()
        recent = [e for e in self._fail_open_log if now - e[0] <= 60.0]
        last = self._fail_open_log[-1] if self._fail_open_log else None
        return {
            "fail_opens_60s": len(recent),
            "last_fail_open": {"ts": _iso(last[0]), "reason": last[1]} if last else None,
        }
```

Then in each of the 4 analyzer files, find the existing `logger.warning("...fail-open...")` sites and add one line above each: `self._record_fail_open("<reason string>")`. Reasons to use:
- `llama_guard.py` — `"ollama_unavailable"`, `"timeout"`
- `presidio_pii.py` — `"timeout"`, `"unavailable"`
- `safety_classifier.py` — `"timeout"`, `"unavailable"`
- `prompt_guard.py` — `"timeout"`, `"unavailable"`

**Step 1.2.4 — Run tests + regression:**

```
pytest tests/unit/content/ -v
```
Expected: PASS.

**Step 1.2.5 — Commit:**

```bash
git add src/gateway/content/ tests/unit/content/test_analyzer_fail_open_log.py
git commit -m "feat(content): analyzer fail-open deque for connections endpoint"
```

---

### Task 1.3: Tool-loop swallowed-exception deque

**Files:**
- Modify: `src/gateway/pipeline/tool_executor.py`
- Test: `tests/unit/pipeline/test_tool_executor_exception_log.py` (new)

**Step 1.3.1 — Write test:**

```python
from gateway.pipeline.tool_executor import (
    record_tool_exception, tool_exceptions_snapshot, _tool_exception_log,
)

def setup_function():
    _tool_exception_log.clear()

def test_snapshot_empty():
    assert tool_exceptions_snapshot() == {"exceptions_60s": 0, "last_exception": None}

def test_snapshot_records():
    record_tool_exception(tool="web_search", error="timeout after 10000ms")
    snap = tool_exceptions_snapshot()
    assert snap["exceptions_60s"] == 1
    assert snap["last_exception"]["tool"] == "web_search"
```

**Step 1.3.2 — Run, expect FAIL.**

**Step 1.3.3 — Implement** at top of `src/gateway/pipeline/tool_executor.py`:

```python
from collections import deque
from time import time as _now

_tool_exception_log: deque = deque(maxlen=50)

def record_tool_exception(*, tool: str, error: str) -> None:
    _tool_exception_log.append((_now(), tool, error))

def tool_exceptions_snapshot() -> dict:
    now = _now()
    recent = [e for e in _tool_exception_log if now - e[0] <= 60.0]
    last = _tool_exception_log[-1] if _tool_exception_log else None
    return {
        "exceptions_60s": len(recent),
        "last_exception": {"ts": _iso(last[0]), "tool": last[1], "error": last[2]} if last else None,
    }
```

Then at each of the existing `logger.warning(...)` sites that swallows an exception (lines 89, 103, 125, 139, 150, 283, 303, 325, 331, 423, 428, 450 per analysis), add `record_tool_exception(tool=<known_tool_name_or_"unknown">, error=str(exc))` on the line above the warning. For sites that don't have a local `exc` variable, pass `error="<short static reason>"` instead.

**Step 1.3.4 — Run, commit:**

```bash
pytest tests/unit/pipeline/test_tool_executor_exception_log.py -v
git add src/gateway/pipeline/tool_executor.py tests/unit/pipeline/test_tool_executor_exception_log.py
git commit -m "feat(tool-executor): record swallowed exceptions for connections endpoint"
```

---

### Task 1.4: Stream interruption deque

**Files:**
- Modify: `src/gateway/pipeline/forwarder.py`
- Test: `tests/unit/pipeline/test_forwarder_interruption_log.py` (new)

**Step 1.4.1 — Write test (same shape as Task 1.3):**

```python
from gateway.pipeline.forwarder import (
    record_stream_interruption, stream_interruptions_snapshot, _stream_interruption_log,
)

def setup_function():
    _stream_interruption_log.clear()

def test_empty():
    assert stream_interruptions_snapshot() == {"interruptions_60s": 0, "last_interruption": None}

def test_records():
    record_stream_interruption(provider="ollama", detail="client disconnect")
    snap = stream_interruptions_snapshot()
    assert snap["interruptions_60s"] == 1
    assert snap["last_interruption"]["provider"] == "ollama"
```

**Step 1.4.2 — Run, expect FAIL.**

**Step 1.4.3 — Implement** at top of `src/gateway/pipeline/forwarder.py`:

```python
from collections import deque
from time import time as _now

_stream_interruption_log: deque = deque(maxlen=50)

def record_stream_interruption(*, provider: str, detail: str) -> None:
    _stream_interruption_log.append((_now(), provider, detail))

def stream_interruptions_snapshot() -> dict:
    now = _now()
    recent = [e for e in _stream_interruption_log if now - e[0] <= 60.0]
    last = _stream_interruption_log[-1] if _stream_interruption_log else None
    return {
        "interruptions_60s": len(recent),
        "last_interruption": {"ts": _iso(last[0]), "provider": last[1], "detail": last[2]} if last else None,
    }
```

Then at the existing `except BaseException` block in `stream_with_tee` (lines 434–440 per analysis), add one line above the `logger.warning(...)`: `record_stream_interruption(provider=provider, detail=str(exc) or type(exc).__name__)`. Also at the background-task exception catch (lines 452–459).

**Step 1.4.4 — Run, commit:**

```bash
pytest tests/unit/pipeline/test_forwarder_interruption_log.py -v
git add src/gateway/pipeline/forwarder.py tests/unit/pipeline/test_forwarder_interruption_log.py
git commit -m "feat(forwarder): record stream interruptions for connections endpoint"
```

---

### Task 1.5: Intelligence worker snapshot

**Files:**
- Modify: `src/gateway/intelligence/worker.py`
- Test: `tests/unit/intelligence/test_worker_snapshot.py` (new)

**Step 1.5.1 — Write test:**

```python
from gateway.intelligence.worker import IntelligenceWorker

def test_snapshot_defaults():
    w = IntelligenceWorker()
    snap = w.snapshot()
    assert snap["running"] is False  # not started
    assert snap["queue_depth"] == 0
    assert snap["last_error"] is None

def test_snapshot_records_last_error():
    w = IntelligenceWorker()
    w._record_error("KeyError on _CLASSIFY_PROMPT")
    snap = w.snapshot()
    assert snap["last_error"]["detail"] == "KeyError on _CLASSIFY_PROMPT"
```

**Step 1.5.2 — Run, expect FAIL.**

**Step 1.5.3 — Implement in `src/gateway/intelligence/worker.py`:**

In `IntelligenceWorker.__init__`:
```python
self._last_error: tuple[float, str] | None = None
```

Add methods:
```python
def _record_error(self, detail: str) -> None:
    self._last_error = (_now(), detail)

def snapshot(self) -> dict:
    running = bool(getattr(self, "_task", None) and not self._task.done())
    q = getattr(self, "_queue", None)
    queue_depth = q.qsize() if q else 0
    # oldest_job_age: peek self._queue if it stores IntelligenceJob (has enqueued_at)
    oldest = 0.0  # leave at 0.0 — queue is not iterable without consuming
    return {
        "running": running,
        "queue_depth": queue_depth,
        "oldest_job_age_s": oldest,
        "last_error": {"ts": _iso(self._last_error[0]), "detail": self._last_error[1]} if self._last_error else None,
    }
```

Then at every `except Exception` catch in the worker's run loop (find `logger.warning` / `logger.error` sites), add `self._record_error(f"{type(exc).__name__}: {exc}")` above it.

**Step 1.5.4 — Run, commit:**

```bash
pytest tests/unit/intelligence/test_worker_snapshot.py -v
git add src/gateway/intelligence/worker.py tests/unit/intelligence/test_worker_snapshot.py
git commit -m "feat(intelligence): worker snapshot for connections endpoint"
```

---

## Phase 2 — Supporting helpers

### Task 2.1: `DefaultResourceMonitor.snapshot()`

**Files:**
- Modify: `src/gateway/adaptive/resource_monitor.py`
- Test: `tests/unit/adaptive/test_resource_monitor_snapshot.py` (new)

**Step 2.1.1 — Write test:**

```python
from gateway.adaptive.resource_monitor import DefaultResourceMonitor

def test_snapshot_empty():
    m = DefaultResourceMonitor()
    assert m.snapshot() == {"providers": {}}

def test_snapshot_records_provider_state():
    m = DefaultResourceMonitor()
    m.record_outcome("ollama", ok=True)
    m.record_outcome("ollama", ok=False)
    snap = m.snapshot()
    assert "ollama" in snap["providers"]
    p = snap["providers"]["ollama"]
    assert "error_rate_60s" in p and "cooldown_until" in p and "last_error" in p
```

**Step 2.1.2 — Run, expect FAIL.**

**Step 2.1.3 — Implement:** add a `snapshot(self) -> dict` that iterates `self._provider_results` keys and for each composes `error_rate_60s` (reuse internal logic), `cooldown_until` (compute from `get_provider_cooldown`), `last_error` (track via new `_last_error: dict[str, str]` populated when `record_outcome(..., ok=False, error=...)` is called — extend `record_outcome` signature to accept an optional `error: str | None = None` kwarg).

**Step 2.1.4 — Run + regression:**

```
pytest tests/unit/adaptive/ -v
```

**Step 2.1.5 — Commit:**

```bash
git add src/gateway/adaptive/resource_monitor.py tests/unit/adaptive/test_resource_monitor_snapshot.py
git commit -m "feat(adaptive): ResourceMonitor.snapshot() for connections endpoint"
```

---

### Task 2.2: `get_attempts()` disposition filter

**Files:**
- Modify: `src/gateway/lineage/walacor_reader.py:379-438` (`get_attempts`)
- Modify: `src/gateway/lineage/reader.py` (mirror if the local reader has a parallel method)
- Test: `tests/unit/test_lineage_reader.py` (extend)

**Step 2.2.1 — Write test:**

```python
def test_get_attempts_disposition_filter(tmp_path):
    reader = LineageReader(wal_path=str(tmp_path / "wal.db"))
    # assume helper to seed rows with known dispositions
    # then:
    rows = reader.get_attempts(disposition="readiness_degraded")
    assert all(r["disposition"] == "readiness_degraded" for r in rows)
```

**Step 2.2.2 — Run, expect FAIL** (kwarg not supported).

**Step 2.2.3 — Implement:** add `disposition: str | None = None` kwarg; when set, add `WHERE disposition = ?` to the SQL (local reader) or `{"$match": {"disposition": disposition}}` stage to the aggregation pipeline (Walacor reader). Keep existing behavior when kwarg is None.

**Step 2.2.4 — Run + regression, commit:**

```bash
pytest tests/unit/test_lineage_reader.py -v
git add src/gateway/lineage/ tests/unit/test_lineage_reader.py
git commit -m "feat(lineage): get_attempts disposition filter for connections endpoint"
```

---

## Phase 3 — `/v1/connections` endpoint

### Task 3.1: Create the connections module skeleton

**Files:**
- Create: `src/gateway/connections/__init__.py` (empty)
- Create: `src/gateway/connections/api.py`
- Create: `src/gateway/connections/builder.py`
- Test: `tests/unit/connections/__init__.py` (empty)
- Test: `tests/unit/connections/test_api.py` (new)

**Step 3.1.1 — Write the failing shape test:**

```python
# tests/unit/connections/test_api.py
import pytest
from httpx import AsyncClient, ASGITransport
from gateway.main import app  # or wherever ASGI app is built

@pytest.mark.anyio
async def test_connections_endpoint_returns_envelope(anyio_backend):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/v1/connections", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["ttl_seconds"] == 3
    assert body["overall_status"] in ("green", "amber", "red")
    assert len(body["tiles"]) == 10
    tile_ids = [t["id"] for t in body["tiles"]]
    assert tile_ids == [
        "providers", "walacor_delivery", "analyzers", "tool_loop",
        "model_capabilities", "control_plane", "auth", "readiness",
        "streaming", "intelligence_worker",
    ]
    assert isinstance(body["events"], list)
    assert len(body["events"]) <= 50
```

**Step 3.1.2 — Run, expect FAIL** (404 or no route).

**Step 3.1.3 — Implement** `src/gateway/connections/builder.py` — one function per tile, each returning `{"id","status","headline","subline","last_change_ts","detail"}` per the design spec. Fail-open: wrap each in try/except that logs + returns `status:"unknown"`. See design doc §Tiles for the exact detail shape and status thresholds for every tile.

Then `src/gateway/connections/api.py`:
```python
import asyncio, time
from fastapi import APIRouter, Request
from .builder import build_all_tiles, build_events, compute_rollup

router = APIRouter()

_CACHE: dict = {"snapshot": None, "ts": 0.0}
_LOCK = asyncio.Lock()

@router.get("/v1/connections")
async def connections_endpoint(request: Request):
    now = time.time()
    if _CACHE["snapshot"] and now - _CACHE["ts"] < 3.0:
        return _CACHE["snapshot"]
    async with _LOCK:
        if _CACHE["snapshot"] and time.time() - _CACHE["ts"] < 3.0:
            return _CACHE["snapshot"]
        ctx = request.app.state.pipeline_context
        tiles = await build_all_tiles(ctx)
        events = await build_events(ctx)
        overall = compute_rollup(tiles)
        snapshot = {
            "generated_at": _iso(time.time()),
            "ttl_seconds": 3,
            "overall_status": overall,
            "tiles": tiles,
            "events": events,
        }
        _CACHE["snapshot"] = snapshot
        _CACHE["ts"] = time.time()
        return snapshot
```

Mount the router in `src/gateway/main.py` alongside other `/v1/*` routers (after the control-plane router mount). Mounting must happen under the existing `api_key_middleware`.

**Step 3.1.4 — Run the test, ensure PASS:**

```
pytest tests/unit/connections/test_api.py -v
```

**Step 3.1.5 — Commit:**

```bash
git add src/gateway/connections/ src/gateway/main.py tests/unit/connections/
git commit -m "feat(connections): /v1/connections endpoint skeleton"
```

---

### Task 3.2: Builders for each tile (one sub-task per tile)

For each of the 10 tiles, write: (a) a unit test asserting shape + one status threshold case, (b) implement the builder in `builder.py`, (c) run + commit.

Group into 2 commits of 5 builders each to stay bite-sized:

**Step 3.2.1 — Infrastructure-side tiles (providers, walacor_delivery, analyzers, tool_loop, streaming):**

Build each, write tests like:

```python
@pytest.mark.anyio
async def test_providers_tile_amber_when_elevated_error_rate(anyio_backend, fake_ctx):
    fake_ctx.resource_monitor = FakeMonitor({"ollama": {"error_rate_60s": 0.25, "cooldown_until": None, "last_error": None}})
    tile = await build_providers_tile(fake_ctx)
    assert tile["status"] == "amber"
```

Implement pulling from `ctx.walacor_client.delivery_snapshot()`, each analyzer's `fail_open_snapshot()`, the module-level `tool_exceptions_snapshot()` / `stream_interruptions_snapshot()`, and `ctx.resource_monitor.snapshot()`. Apply the thresholds from the design doc literally — no interpretation.

Commit: `feat(connections): infrastructure-side tile builders`

**Step 3.2.2 — Governance-side tiles (model_capabilities, control_plane, auth, readiness, intelligence_worker):**

Same pattern. Pull from: `_model_capabilities` (import from orchestrator), `ctx.policy_cache` + sync task handle, readiness rollup from `readiness.runner.compute()`, `ctx.intelligence_worker.snapshot()`, `ctx.auth_state` (or a new tiny `auth_snapshot()` helper if nothing exists).

Commit: `feat(connections): governance-side tile builders`

---

### Task 3.3: Events stream merger

**Files:**
- Modify: `src/gateway/connections/builder.py` (add `build_events`)
- Test: `tests/unit/connections/test_events.py` (new)

**Step 3.3.1 — Test:**

```python
@pytest.mark.anyio
async def test_events_sorted_newest_first_capped_50(anyio_backend, fake_ctx):
    fake_ctx.walacor_client._delivery_log.extend([(now-i, "submit", False, "x") for i in range(60)])
    events = await build_events(fake_ctx)
    assert len(events) <= 50
    assert all(events[i]["ts"] >= events[i+1]["ts"] for i in range(len(events)-1))
```

**Step 3.3.2 — Implement** — drain each deque into `Event` dicts, sort by ts desc, slice [:50]. No storage, no dedup. Each source adds `subsystem` string matching the tile id.

**Step 3.3.3 — Commit:**

```bash
git add src/gateway/connections/builder.py tests/unit/connections/test_events.py
git commit -m "feat(connections): event stream merger"
```

---

### Task 3.4: Fail-open behavior test

**Files:**
- Test: `tests/unit/connections/test_fail_open.py` (new)

**Step 3.4.1 — Test:**

```python
@pytest.mark.anyio
async def test_endpoint_never_5xx_when_probe_raises(anyio_backend, monkeypatch):
    monkeypatch.setattr("gateway.connections.builder.build_providers_tile",
                        lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
    r = await client.get("/v1/connections", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    providers = next(t for t in r.json()["tiles"] if t["id"] == "providers")
    assert providers["status"] == "unknown"
```

**Step 3.4.2 — Implement** — wrap each `build_*_tile` call in `builder.build_all_tiles` with try/except → unknown tile + log at WARN.

**Step 3.4.3 — Commit:**

```bash
git add src/gateway/connections/builder.py tests/unit/connections/test_fail_open.py
git commit -m "feat(connections): fail-open when individual probe raises"
```

---

### Task 3.5: Config flag + rollback

**Files:**
- Modify: `src/gateway/config.py`
- Modify: `src/gateway/connections/api.py`
- Test: `tests/unit/connections/test_api.py` (extend)

**Step 3.5.1 — Test:**

```python
@pytest.mark.anyio
async def test_returns_503_when_disabled(anyio_backend, monkeypatch):
    monkeypatch.setenv("WALACOR_CONNECTIONS_ENABLED", "false")
    get_settings.cache_clear()
    r = await client.get("/v1/connections", headers={"X-API-Key": "test-key"})
    assert r.status_code == 503
```

**Step 3.5.2 — Implement:** add `connections_enabled: bool = True` in `config.py`. In the endpoint, early-return `HTTPException(503)` when disabled.

**Step 3.5.3 — Commit:**

```bash
git add src/gateway/config.py src/gateway/connections/api.py tests/unit/connections/test_api.py
git commit -m "feat(connections): WALACOR_CONNECTIONS_ENABLED rollback flag"
```

---

## Phase 4 — Frontend port

### Task 4.1: Copy the pre-ported files into the live dashboard

**Files:**
- Create: `src/gateway/lineage/dashboard/src/views/Connections.jsx` (copy from bundle)
- Create: `src/gateway/lineage/dashboard/src/styles/connections.css` (copy from bundle)
- Create: `src/gateway/lineage/dashboard/src/components/JsonView.jsx` (copy from bundle if not already present)
- Create: `src/gateway/lineage/dashboard/src/components/CopyBtn.jsx` (copy from bundle if not already present)

**Step 4.1.1 — Check if components already exist:**

```bash
ls src/gateway/lineage/dashboard/src/components/
```

If `JsonView.jsx` / `CopyBtn.jsx` already exist from the Phase 26 seal drawer, skip them. Otherwise copy verbatim from `docs/plans/assets/2026-04-23-connections-truzenai/project/src/gateway/lineage/dashboard/src/components/`.

**Step 4.1.2 — Copy:**

```bash
cp "docs/plans/assets/2026-04-23-connections-truzenai/project/src/gateway/lineage/dashboard/src/views/Connections.jsx" \
   src/gateway/lineage/dashboard/src/views/Connections.jsx
cp "docs/plans/assets/2026-04-23-connections-truzenai/project/src/gateway/lineage/dashboard/src/styles/connections.css" \
   src/gateway/lineage/dashboard/src/styles/connections.css
```

**Step 4.1.3 — Commit:**

```bash
git add src/gateway/lineage/dashboard/src/views/Connections.jsx src/gateway/lineage/dashboard/src/styles/connections.css src/gateway/lineage/dashboard/src/components/
git commit -m "feat(dashboard): port Connections view + styles from TruzenAI bundle"
```

---

### Task 4.2: Wire `getConnections()` api helper

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/api.js`

**Step 4.2.1 — Read current api.js to find the `fetchJSON` helper and existing patterns.**

**Step 4.2.2 — Add at the appropriate spot (near other lineage helpers):**

```javascript
export async function getConnections() {
  return fetchJSON(`${API}/connections`);
}
```

**Step 4.2.3 — Commit:**

```bash
git add src/gateway/lineage/dashboard/src/api.js
git commit -m "feat(dashboard): getConnections() api helper"
```

---

### Task 4.3: Replace mock wiring with real API call in Connections.jsx

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Connections.jsx`

**Step 4.3.1 — In `Connections.jsx`:**

1. Uncomment `import { getConnections } from '../api';` at the top.
2. Locate the `useState(() => scenarios.<something>)` / `buildMocks()` wiring inside `function Connections({ navigate })`.
3. Replace with real polling — add a `useConnections` hook at the top of the component (hooks-before-return!):

```javascript
const [snapshot, setSnapshot] = useState(null);
const [loading, setLoading] = useState(true);
const [error, setError] = useState(null);

useEffect(() => {
  let cancelled = false;
  async function poll() {
    try {
      const data = await getConnections();
      if (!cancelled) { setSnapshot(data); setError(null); setLoading(false); }
    } catch (e) {
      if (!cancelled) { setError(e.message || 'probe failed'); setLoading(false); }
    }
  }
  poll();
  const id = setInterval(poll, POLL_MS);
  return () => { cancelled = true; clearInterval(id); };
}, []);
```

4. Delete `buildMocks`, `scenarios`, `setScenario`, and any `<CxScenarioPicker />` JSX.
5. Verify `Intro` still receives `{snapshot, loading, error}` (no scenario props).

**Step 4.3.2 — Rebuild & smoke:**

```bash
cd src/gateway/lineage/dashboard && npm run build
```

Check that no references to `ConnectionsMocks`, `scenarios`, `CxScenarioPicker` remain.

**Step 4.3.3 — Commit:**

```bash
git add src/gateway/lineage/dashboard/src/views/Connections.jsx src/gateway/lineage/static/
git commit -m "feat(dashboard): Connections view polls /v1/connections live"
```

---

### Task 4.4: Wire session-navigation

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Connections.jsx`

**Step 4.4.1 — Locate `onEventClick` — currently `navigate('sessions', { q: ev.session_id })`.**

If the live dashboard uses URL params instead of a navigate function, replace with:

```javascript
const onEventClick = useCallback((ev) => {
  if (ev?.session_id) {
    window.location.hash = `#/sessions?session_id=${encodeURIComponent(ev.session_id)}`;
  }
}, []);
```

Confirm against the pattern already used in `Sessions.jsx` / `Timeline.jsx`. Do not invent a new scheme — match what exists.

**Step 4.4.2 — Commit:**

```bash
git add src/gateway/lineage/dashboard/src/views/Connections.jsx
git commit -m "feat(dashboard): wire event-row click to session deep link"
```

---

### Task 4.5: Graft v4 banner stats strip

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Connections.jsx`
- Modify: `src/gateway/lineage/dashboard/src/styles/connections.css`

**Step 4.5.1 — Copy the `V4Stat` component and `v4-banner-stats` markup from `docs/plans/assets/2026-04-23-connections-truzenai/project/overview/connections-v4.jsx:345-395` into `Connections.jsx` (near the existing helper components). Copy the corresponding `.v4-banner`, `.v4-banner-stats`, `.v4-stat*` CSS rules from `connections-v4.css` into the bottom of `connections.css`. Verbatim — do not rename classes.**

**Step 4.5.2 — In the main `Connections` component's JSX, render the stats strip above the triage queue when `snapshot.overall_status !== 'green'`:**

```jsx
{snapshot && snapshot.overall_status !== 'green' && (
  <div className="v4-banner-stats-standalone">
    <V4Stat n={counts.red} label="DOWN" tone="red" />
    <V4Stat n={counts.amber} label="DEGRADED" tone="amber" />
    <V4Stat n={counts.green} label="HEALTHY" tone="green" />
    <span className="v4-banner-sep" />
    <V4Stat n={blastRadius.sessions.length} label="SESSIONS HIT" tone="neutral" />
    <V4Stat n={blastRadius.executions.length} label="EXECUTIONS HIT" tone="neutral" />
    <V4Stat n={blastRadius.requests.length} label="REQUESTS HIT" tone="neutral" />
  </div>
)}
```

Compute `counts` and `blastRadius` from `tilesInOrder` and `events` using the same logic as in v4.jsx (copy verbatim).

**Step 4.5.3 — Build, visually sanity-check, commit:**

```bash
cd src/gateway/lineage/dashboard && npm run build
git add src/gateway/lineage/dashboard/src/
git commit -m "feat(dashboard): graft v4 banner stats strip for amber/red states"
```

---

### Task 4.6: Graft v4 incident headline (red-only)

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Connections.jsx`
- Modify: `src/gateway/lineage/dashboard/src/styles/connections.css`

**Step 4.6.1 — Copy from `connections-v4.jsx:149-195` (the `{primary && (<div className="v4-banner ...">...</div>)}` block, minus the `v4-banner-stats` which we did in 4.5). Render only when `counts.red >= 1`. Compute `primary = reds[0]` as in v4. Copy corresponding CSS selectors (`.v4-banner`, `.v4-banner-bar`, `.v4-banner-main`, `.v4-banner-eyebrow`, `.v4-banner-title`, `.v4-banner-sub`, `.v4-banner-side`, `.v4-banner-cta`) verbatim into `connections.css`.**

**Step 4.6.2 — Build, commit:**

```bash
cd src/gateway/lineage/dashboard && npm run build
git add src/gateway/lineage/dashboard/src/
git commit -m "feat(dashboard): graft v4 red-incident banner"
```

---

### Task 4.7: Graft runbook block into tile-detail drawer

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/views/Connections.jsx`
- Modify: `src/gateway/lineage/dashboard/src/styles/connections.css`

**Step 4.7.1 — Copy from `connections-v4.jsx:19-45` the `V4_RUNBOOK` object verbatim. Copy the `V4Runbook` component from lines 444-470 verbatim. Copy `.v4-runbook*` CSS selectors from `connections-v4.css` verbatim into `connections.css`.**

**Step 4.7.2 — In `TilePanel` (the slide-over), render the runbook block below the existing `<JsonView />`:**

```jsx
{V4_RUNBOOK[tile.id] ? (
  <div className="cx-panel-body">
    <p className="cx-panel-body-label">◇ Runbook</p>
    <V4Runbook runbook={V4_RUNBOOK[tile.id]} />
  </div>
) : (
  <div className="cx-panel-body">
    <p className="cx-panel-body-label">◇ Runbook</p>
    <p className="cx-runbook-empty">No curated runbook for this subsystem yet.</p>
  </div>
)}
```

**Step 4.7.3 — Build, commit:**

```bash
cd src/gateway/lineage/dashboard && npm run build
git add src/gateway/lineage/dashboard/src/
git commit -m "feat(dashboard): runbook block in tile-detail drawer"
```

---

### Task 4.8: Nav entry in App.jsx

**Files:**
- Modify: `src/gateway/lineage/dashboard/src/App.jsx`

**Step 4.8.1 — Read `App.jsx` to understand the route/tab registration pattern (look for how `Control`, `Intelligence`, `Overview` are registered).**

**Step 4.8.2 — Add one entry for Connections, matching the existing pattern verbatim. Lazy-load the view component. Add the nav tab label.**

**Step 4.8.3 — Rebuild, commit:**

```bash
cd src/gateway/lineage/dashboard && npm run build
git add src/gateway/lineage/dashboard/src/App.jsx src/gateway/lineage/static/
git commit -m "feat(dashboard): Connections nav entry"
```

---

## Phase 5 — Integration & regression

### Task 5.1: Tier 1 live smoke check

**Files:**
- Modify: `tests/production/tier1_live.py`

**Step 5.1.1 — Add a test function:**

```python
def test_connections_endpoint(gateway_url, api_key):
    r = requests.get(f"{gateway_url}/v1/connections", headers={"X-API-Key": api_key}, timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["overall_status"] in ("green", "amber", "red")
    assert len(body["tiles"]) == 10
    assert "events" in body
```

**Step 5.1.2 — Commit:**

```bash
git add tests/production/tier1_live.py
git commit -m "test(tier1): smoke /v1/connections"
```

---

### Task 5.2: Full regression gate

**Step 5.2.1 — Run the full unit suite:**

```bash
pytest -q
```

Expected: all existing tests still pass, plus all new ones. If any existing test fails, diagnose; do not modify it to "fit" — restore previous behavior instead.

**Step 5.2.2 — If all green, final commit only if there's anything uncommitted; otherwise this task is a no-op checkpoint.**

---

### Task 5.3: Manual browser check against local gateway

**Step 5.3.1 — Start the gateway locally with mocks as needed. Open `http://localhost:8000/lineage/#/connections`. Confirm:**

- 10 tiles render in the fixed order
- Events stream populates (will be empty on a fresh start; induce a tool error or disable an analyzer to force one)
- Tile click → slide-over with JSON + runbook-or-empty
- Event click with session_id → navigates to Sessions
- No React #310 in console
- No 500 in the network tab for `/v1/connections`

**Step 5.3.2 — If any regressions, file them as follow-up tasks; do not silently patch.**

---

### Task 5.4: Update memory + push

**Files:**
- Modify: `CLAUDE.md` — add a one-paragraph note under a new "## Phase 27: Connections" section summarizing the endpoint + the ring-buffer pattern, mirroring the style of existing phase notes.

**Step 5.4.1 — Commit & push:**

```bash
git add CLAUDE.md
git commit -m "docs(claude-md): Phase 27 Connections summary"
git push origin feature/phase25-onnx-self-learning
```

---

## Out of scope (follow-up)

- Recent-changes feed (git/deploy log integration)
- Runbooks for all 10 subsystems (currently 3 seed entries)
- Historical persistence of silent-failure events (current design is live-only by intent)
- Alert/notification hooks (e.g. Slack webhook on red transitions)

---

Plan complete and saved to `docs/plans/2026-04-23-connections.md`. Two execution options:

**1. Subagent-Driven (this session)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Parallel Session (separate)** — open a new session with `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
