# Performance Optimization Implementation Plan

> **Status:** Historical planning artifact. The Phase 13 SHA3 Merkle chain referenced inside was superseded by an ID-pointer chain backed by Walacor-issued `DH`.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Optimize the pure Python gateway for 10-100 req/s on m6a.xlarge (4 vCPU, 16GB, CPU-only Ollama).

**Architecture:** 7 targeted fixes across the hot path — WAL commit batching, bounded caches, cache TTL, configurable timeouts, O(1) eviction, eliminated double-parsing, and connection pool tuning. No behavioral changes.

**Tech Stack:** Python 3.12, cachetools (LRUCache/TTLCache), collections.OrderedDict, SQLite WAL mode.

**Design doc:** `docs/plans/2026-03-16-performance-optimization-design.md`

---

### Task 1: WAL Commit Batching

**Files:**
- Modify: `src/gateway/wal/writer.py:141-156` (writer loop)
- Modify: `src/gateway/wal/writer.py:161-208` (remove per-write commits)
- Test: `tests/unit/test_wal_batch.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_wal_batch.py
"""Tests for WAL writer batch commit behavior."""
import sqlite3
import tempfile
import time
from pathlib import Path

from gateway.wal.writer import WALWriter


def test_batch_commit_groups_writes(tmp_path):
    """Multiple enqueued writes should result in fewer commits than writes."""
    db_path = str(tmp_path / "test.db")
    writer = WALWriter(db_path)
    writer.start()

    # Enqueue 20 writes rapidly
    for i in range(20):
        writer.enqueue_write_execution({
            "execution_id": f"exec-{i}",
            "session_id": "s1",
            "model_attestation_id": "m1",
            "timestamp": "2026-01-01T00:00:00Z",
        })

    # Give writer thread time to process
    time.sleep(0.2)
    writer.stop()

    # Verify all 20 records were written
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM wal_records").fetchone()[0]
    conn.close()
    assert count == 20


def test_batch_commit_single_write(tmp_path):
    """A single enqueued write should still be committed."""
    db_path = str(tmp_path / "test.db")
    writer = WALWriter(db_path)
    writer.start()

    writer.enqueue_write_execution({
        "execution_id": "exec-solo",
        "session_id": "s1",
        "model_attestation_id": "m1",
        "timestamp": "2026-01-01T00:00:00Z",
    })

    time.sleep(0.2)
    writer.stop()

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM wal_records").fetchone()[0]
    conn.close()
    assert count == 1
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_wal_batch.py -v`

**Step 3: Implement batch commit in writer loop**

Replace `_writer_loop` in `src/gateway/wal/writer.py:141-156` with:

```python
_BATCH_MAX = 50
_BATCH_TIMEOUT = 0.01  # 10ms

def _writer_loop(self) -> None:
    """Process write operations in batches — commit once per batch."""
    conn = self._ensure_thread_conn()
    while True:
        batch: list[tuple] = []
        # Block until at least one item arrives
        try:
            item = self._queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if item is None:  # sentinel — graceful exit
            break
        batch.append(item)

        # Drain queue up to BATCH_MAX or BATCH_TIMEOUT
        deadline = time.monotonic() + self._BATCH_TIMEOUT
        while len(batch) < self._BATCH_MAX:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:  # sentinel
                # Execute what we have, then exit
                self._execute_batch(conn, batch)
                return
            batch.append(item)

        self._execute_batch(conn, batch)

def _execute_batch(self, conn: sqlite3.Connection, batch: list[tuple]) -> None:
    """Execute all writes in batch, then commit once."""
    for fn, args in batch:
        try:
            fn(conn, *args)
        except Exception:
            logger.error("WAL batch write error", exc_info=True)
    try:
        conn.commit()
    except Exception:
        logger.error("WAL batch commit error", exc_info=True)
```

Remove `conn.commit()` from each of the 3 `_do_write_*` methods (lines 174, 197, 207).

Add `import time` to the imports at the top.

**Step 4: Run test to verify it passes**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_wal_batch.py -v`
Expected: 2 PASS

**Step 5: Run full test suite**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/ -v --timeout=30`
Expected: All existing tests still pass.

**Step 6: Commit**

```bash
git add src/gateway/wal/writer.py tests/unit/test_wal_batch.py
git commit -m "perf: WAL commit batching — group writes in 10ms/50-item windows"
```

---

### Task 2: Bounded LRU on Model Capabilities and Concurrency Limiters

**Files:**
- Modify: `src/gateway/pipeline/orchestrator.py:61` (`_concurrency_limiters`)
- Modify: `src/gateway/pipeline/orchestrator.py:85` (`_model_capabilities`)
- Test: `tests/unit/test_bounded_caches.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_bounded_caches.py
"""Tests for bounded LRU caches on model capabilities and concurrency limiters."""
from cachetools import LRUCache


def test_model_capabilities_bounded():
    """Model capabilities cache should evict oldest entries at capacity."""
    cache = LRUCache(maxsize=3)
    cache["a"] = {"supports_tools": True}
    cache["b"] = {"supports_tools": False}
    cache["c"] = {"supports_tools": True}
    cache["d"] = {"supports_tools": False}  # evicts "a"
    assert "a" not in cache
    assert len(cache) == 3


def test_concurrency_limiters_bounded():
    """Concurrency limiters cache should evict oldest entries at capacity."""
    cache = LRUCache(maxsize=2)
    cache["provider-a"] = "limiter-a"
    cache["provider-b"] = "limiter-b"
    cache["provider-c"] = "limiter-c"  # evicts "provider-a"
    assert "provider-a" not in cache
    assert len(cache) == 2
```

**Step 2: Run test to verify it passes (these test cachetools behavior)**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_bounded_caches.py -v`
Expected: PASS (validates our assumptions about LRUCache)

**Step 3: Replace unbounded dicts in orchestrator.py**

In `src/gateway/pipeline/orchestrator.py`, add import:
```python
from cachetools import LRUCache
```

Replace line 61:
```python
# Before
_concurrency_limiters: dict[str, ConcurrencyLimiter] = {}
# After
_concurrency_limiters: LRUCache = LRUCache(maxsize=100)
```

Replace line 85:
```python
# Before
_model_capabilities: dict[str, dict[str, bool]] = {}
# After
_model_capabilities: LRUCache = LRUCache(maxsize=500)
```

**Step 4: Run full test suite**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/ -v --timeout=30`
Expected: All pass. LRUCache is dict-compatible (supports `get()`, `[]`, `in`, iteration).

**Step 5: Commit**

```bash
git add src/gateway/pipeline/orchestrator.py tests/unit/test_bounded_caches.py
git commit -m "perf: bound model_capabilities (500) and concurrency_limiters (100) with LRU"
```

---

### Task 3: Content Analysis Cache TTL

**Files:**
- Modify: `src/gateway/pipeline/response_evaluator.py:1-11,48`
- Test: `tests/unit/test_analysis_cache_ttl.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_analysis_cache_ttl.py
"""Test that content analysis cache entries expire after TTL."""
import time
from cachetools import TTLCache


def test_ttl_cache_expires():
    """Entries should disappear after TTL."""
    cache = TTLCache(maxsize=100, ttl=0.1)  # 100ms TTL for test
    cache["key1"] = [{"verdict": "pass"}]
    assert "key1" in cache
    time.sleep(0.15)
    assert "key1" not in cache


def test_ttl_cache_clear():
    """Manual clear should still work."""
    cache = TTLCache(maxsize=100, ttl=60)
    cache["key1"] = [{"verdict": "pass"}]
    cache.clear()
    assert "key1" not in cache
```

**Step 2: Run test to verify it passes (validates TTLCache behavior)**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_analysis_cache_ttl.py -v`
Expected: PASS

**Step 3: Replace LRUCache with TTLCache in response_evaluator.py**

In `src/gateway/pipeline/response_evaluator.py`, change import (line ~11):
```python
# Before
from cachetools import LRUCache
# After
from cachetools import TTLCache
```

Replace line 48:
```python
# Before
_analysis_cache: LRUCache = LRUCache(maxsize=5000)
# After
_ANALYSIS_CACHE_TTL = 60  # seconds
_analysis_cache: TTLCache = TTLCache(maxsize=5000, ttl=_ANALYSIS_CACHE_TTL)
```

**Step 4: Run full test suite**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/ -v --timeout=30`
Expected: All pass.

**Step 5: Commit**

```bash
git add src/gateway/pipeline/response_evaluator.py tests/unit/test_analysis_cache_ttl.py
git commit -m "perf: add 60s TTL to content analysis cache — prevents stale verdicts"
```

---

### Task 4: Completeness Middleware Configurable Timeout

**Files:**
- Modify: `src/gateway/config.py:~390` (add field)
- Modify: `src/gateway/middleware/completeness.py:49,61,65`
- Test: `tests/unit/test_completeness_timeout.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_completeness_timeout.py
"""Test that completeness middleware uses configurable timeout."""
from gateway.config import GatewaySettings


def test_default_completeness_timeout():
    """Default completeness timeout should be 2.0 seconds."""
    settings = GatewaySettings(
        gateway_tenant_id="test",
        gateway_api_keys=["k"],
    )
    assert settings.completeness_timeout == 2.0
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_completeness_timeout.py -v`
Expected: FAIL — `completeness_timeout` field does not exist yet.

**Step 3: Add config field and update middleware**

In `src/gateway/config.py`, after the `provider_timeout`/`provider_connect_timeout` fields (~line 392), add:
```python
    completeness_timeout: float = Field(
        default=2.0,
        description="Timeout in seconds for completeness middleware storage writes",
    )
```

In `src/gateway/middleware/completeness.py`, replace the hardcoded timeout:
```python
# Line 49: replace timeout=5.0
timeout=settings.completeness_timeout,

# Line 64-65: replace the log message
except asyncio.TimeoutError:
    logger.warning("write_attempt timed out after %.1fs — skipping", settings.completeness_timeout)
```

Move `settings = get_settings()` from line 36 to before the `try` block at line 48 (it's already there — just ensure it's available for the timeout value).

**Step 4: Run test to verify it passes**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_completeness_timeout.py -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/ -v --timeout=30`
Expected: All pass.

**Step 6: Commit**

```bash
git add src/gateway/config.py src/gateway/middleware/completeness.py tests/unit/test_completeness_timeout.py
git commit -m "perf: configurable completeness timeout — default 2s (was hardcoded 5s)"
```

---

### Task 5: Session Chain OrderedDict Eviction

**Files:**
- Modify: `src/gateway/pipeline/session_chain.py:1-77`
- Test: `tests/unit/test_session_chain_eviction.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_session_chain_eviction.py
"""Tests for O(1) session chain eviction via OrderedDict."""
import asyncio
from datetime import datetime, timezone

import pytest

from gateway.pipeline.session_chain import SessionChainTracker


@pytest.fixture
def anyio_backend():
    return ["asyncio"]


@pytest.mark.anyio
async def test_eviction_removes_oldest():
    """When over capacity, oldest (least recently used) session is evicted."""
    tracker = SessionChainTracker(max_sessions=3, ttl_seconds=3600)
    for i in range(4):
        await tracker.update(f"s{i}", i, f"hash{i}")

    # s0 should be evicted (oldest)
    assert tracker.active_session_count() == 3
    seq, prev = await tracker.next_chain_values("s0")
    assert seq == 0  # genesis — s0 was evicted, starts fresh


@pytest.mark.anyio
async def test_access_refreshes_lru_order():
    """Accessing a session should move it to end, preventing eviction."""
    tracker = SessionChainTracker(max_sessions=3, ttl_seconds=3600)
    await tracker.update("s0", 0, "h0")
    await tracker.update("s1", 0, "h1")
    await tracker.update("s2", 0, "h2")

    # Access s0 — moves it to end
    await tracker.next_chain_values("s0")

    # Add s3 — should evict s1 (now oldest), not s0
    await tracker.update("s3", 0, "h3")

    assert tracker.active_session_count() == 3
    # s0 should still exist
    seq, _ = await tracker.next_chain_values("s0")
    assert seq == 1  # has state, not evicted
```

**Step 2: Run test to verify behavior (may pass or fail depending on current eviction)**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_session_chain_eviction.py -v`

**Step 3: Rewrite SessionChainTracker with OrderedDict**

Replace `src/gateway/pipeline/session_chain.py:1-79` with:

```python
"""Phase 13: Merkle chain for session conversation integrity (G5)."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from gateway.core import compute_sha3_512_string

logger = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 128
# Exported alias used by Redis tracker
GENESIS_HASH = _GENESIS_HASH


@dataclass
class SessionState:
    session_id: str
    last_sequence_number: int
    last_record_hash: str
    last_activity: datetime


class SessionChainTracker:
    """
    Thread-safe in-memory Merkle chain tracker.
    Maintains (sequence_number, previous_record_hash) for each active session.
    Sessions are evicted after ttl_seconds of inactivity or when over max_sessions.
    Uses OrderedDict for O(1) LRU eviction.
    """

    def __init__(self, max_sessions: int = 10_000, ttl_seconds: int = 3600) -> None:
        self._max = max_sessions
        self._ttl = ttl_seconds
        self._sessions: OrderedDict[str, SessionState] = OrderedDict()
        self._lock = asyncio.Lock()

    async def next_chain_values(self, session_id: str) -> tuple[int, str]:
        """
        Return (sequence_number, previous_record_hash) for the next record in this session.
        First record in a new session returns (0, GENESIS_HASH).
        """
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return 0, _GENESIS_HASH
            # Touch: move to end so it's not evicted
            self._sessions.move_to_end(session_id)
            return state.last_sequence_number + 1, state.last_record_hash

    async def update(self, session_id: str, sequence_number: int, record_hash: str) -> None:
        """Record the chain state after a WAL write. Evicts stale sessions when over limit."""
        now = datetime.now(timezone.utc)
        async with self._lock:
            self._sessions[session_id] = SessionState(
                session_id=session_id,
                last_sequence_number=sequence_number,
                last_record_hash=record_hash,
                last_activity=now,
            )
            self._sessions.move_to_end(session_id)
            if len(self._sessions) > self._max:
                self._evict_locked(now)

    def _evict_locked(self, now: datetime) -> None:
        """Remove sessions inactive beyond TTL. Then pop oldest if still over limit."""
        cutoff = now.timestamp() - self._ttl
        to_delete = [
            sid for sid, s in self._sessions.items()
            if s.last_activity.timestamp() < cutoff
        ]
        for sid in to_delete:
            del self._sessions[sid]
        # O(1) eviction: pop from front of OrderedDict
        while len(self._sessions) > self._max:
            self._sessions.popitem(last=False)

    def active_session_count(self) -> int:
        return len(self._sessions)
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_session_chain_eviction.py -v`
Expected: 2 PASS

**Step 5: Run full test suite**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/ -v --timeout=30`
Expected: All pass.

**Step 6: Commit**

```bash
git add src/gateway/pipeline/session_chain.py tests/unit/test_session_chain_eviction.py
git commit -m "perf: O(1) session chain eviction via OrderedDict"
```

---

### Task 6: Request Body Double-Parse Elimination

**Files:**
- Modify: `src/gateway/adapters/ollama.py:117-122`
- Modify: `src/gateway/adapters/openai.py:279-284`
- Modify: `src/gateway/adapters/anthropic.py:145-150`
- Modify: `src/gateway/adapters/huggingface.py:28-33`
- Modify: `src/gateway/adapters/generic.py:110-115`
- Test: `tests/unit/test_body_cache_reuse.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_body_cache_reuse.py
"""Test that adapters reuse cached _parsed_body from _peek_model_id."""
import gateway.util.json_utils as json
from unittest.mock import AsyncMock, MagicMock


def _make_request(body_dict: dict) -> MagicMock:
    """Create a mock Request with _parsed_body set (simulating _peek_model_id)."""
    raw = json.dumps(body_dict).encode()
    request = MagicMock()
    request.body = AsyncMock(return_value=raw)
    request.state._parsed_body = body_dict
    request.headers = {}
    return request


def test_parsed_body_is_reused():
    """When _parsed_body is set, json.loads should not be called again."""
    body = {"model": "qwen3:4b", "messages": [{"role": "user", "content": "hi"}]}
    request = _make_request(body)

    cached = getattr(request.state, "_parsed_body", None)
    assert cached is not None
    assert cached["model"] == "qwen3:4b"
```

**Step 2: Run test to verify it passes**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/test_body_cache_reuse.py -v`
Expected: PASS

**Step 3: Update all 5 adapters**

In each adapter's `parse_request()`, replace the body parsing block:

**Before** (same pattern in all 5):
```python
body_bytes = await request.body()
try:
    data = json.loads(body_bytes.decode("utf-8"))
except json.JSONDecodeError:
    raise ValueError("Invalid JSON body")
```

**After**:
```python
data = getattr(request.state, "_parsed_body", None)
if data is None:
    body_bytes = await request.body()
    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON body")
```

Apply to:
- `src/gateway/adapters/ollama.py:118-122`
- `src/gateway/adapters/openai.py:280-284`
- `src/gateway/adapters/anthropic.py:146-150`
- `src/gateway/adapters/huggingface.py:29-33`
- `src/gateway/adapters/generic.py:111-115`

**Step 4: Run full test suite**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/ -v --timeout=30`
Expected: All pass.

**Step 5: Commit**

```bash
git add src/gateway/adapters/ollama.py src/gateway/adapters/openai.py src/gateway/adapters/anthropic.py src/gateway/adapters/huggingface.py src/gateway/adapters/generic.py tests/unit/test_body_cache_reuse.py
git commit -m "perf: reuse cached _parsed_body in adapters — skip redundant JSON parse"
```

---

### Task 7: Connection Pool Tuning Docs

**Files:**
- Modify: `.env.example`

**Step 1: Add m6a.xlarge tuning section to .env.example**

Append to the HTTP/connection section of `.env.example`:

```env
# ── Connection pool tuning ──────────────────────────────────
# Defaults are tuned for multi-provider cloud deployments.
# For single-provider local Ollama on m6a.xlarge (CPU), recommended:
#   WALACOR_PROVIDER_CONNECT_TIMEOUT=3.0   # fail fast (local connects in <10ms)
#   WALACOR_HTTP_POOL_MAX_CONNECTIONS=50    # saves memory vs default 100
#   WALACOR_HTTP_POOL_MAX_KEEPALIVE=10     # fewer idle connections
#   WALACOR_HTTP_KEEPALIVE_EXPIRY=60       # reuse connections longer
#   WALACOR_PROVIDER_TIMEOUT=90.0          # CPU inference takes 30-60s for 8B models
WALACOR_PROVIDER_TIMEOUT=60.0
WALACOR_PROVIDER_CONNECT_TIMEOUT=10.0
WALACOR_HTTP_POOL_MAX_CONNECTIONS=100
WALACOR_HTTP_POOL_MAX_KEEPALIVE=20
WALACOR_HTTP_KEEPALIVE_EXPIRY=30
```

**Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: add connection pool tuning guidance for m6a.xlarge deployments"
```

---

### Task 8: Final Verification

**Step 1: Run full test suite**

Run: `cd /Users/dharmpratapsingh/Walcor/Gateway && python -m pytest tests/unit/ -v --timeout=30`
Expected: All existing + new tests pass. No regressions.

**Step 2: Rebuild Docker image**

Run: `cd /Users/dharmpratapsingh/Walcor && docker compose -f Gateway/deploy/docker-compose.yml build gateway`
Expected: Build succeeds with all new code.

**Step 3: Smoke test**

Run: `cd /Users/dharmpratapsingh/Walcor && docker compose -f Gateway/deploy/docker-compose.yml --profile ollama up -d`
Then: `curl http://localhost:8002/health`
Expected: 200 OK with healthy status.
