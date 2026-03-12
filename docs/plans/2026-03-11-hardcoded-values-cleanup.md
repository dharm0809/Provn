# Hardcoded Values Cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract hardcoded magic values into config fields (Tier 1) and named constants (Tier 2) without changing behavior.

**Architecture:** Tier 1 adds 10 new `config.py` fields with defaults matching current hardcoded values. Tier 2 extracts inline literals to module-level constants. Zero behavioral change — all defaults match current values exactly.

**Tech Stack:** Python, pydantic-settings, pytest

---

### Task 1: Add Tier 1 config fields to config.py

**Files:**
- Modify: `src/gateway/config.py`

Add 10 new fields after the existing server section (~line 283):

```python
# Network tuning
provider_timeout: float = Field(default=60.0, description="Provider HTTP request timeout in seconds")
provider_connect_timeout: float = Field(default=10.0, description="Provider connection timeout in seconds")
provider_max_connections: int = Field(default=200, description="Max concurrent provider connections")
provider_max_keepalive: int = Field(default=50, description="Max keepalive provider connections")
sse_keepalive_interval: float = Field(default=15.0, description="SSE keepalive ping interval in seconds")

# Resilience tuning
delivery_batch_size: int = Field(default=50, description="WAL delivery batch size per cycle")
circuit_breaker_fail_max: int = Field(default=5, description="Failures before circuit opens")
circuit_breaker_reset_timeout: float = Field(default=30.0, description="Seconds before circuit half-open retry")
retry_max_attempts: int = Field(default=3, description="Max forward retry attempts on transient errors")
disk_degraded_threshold: float = Field(default=0.8, description="WAL disk usage threshold (0-1) for degraded status")
```

### Task 2: Wire config fields into main.py (httpx client)

**Files:**
- Modify: `src/gateway/main.py:565-567`

Replace hardcoded httpx.Timeout/Limits with settings values:

```python
ctx.http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(settings.provider_timeout, connect=settings.provider_connect_timeout),
    limits=httpx.Limits(max_connections=settings.provider_max_connections, max_keepalive_connections=settings.provider_max_keepalive),
    http2=True,
)
```

### Task 3: Wire config into forwarder.py (fallback client + keepalive)

**Files:**
- Modify: `src/gateway/pipeline/forwarder.py:37,49,126`

Replace hardcoded 60.0 and 15.0:

```python
# Line 37: keepalive
async def sse_keepalive_generator(interval_seconds: float | None = None):
    if interval_seconds is None:
        interval_seconds = get_settings().sse_keepalive_interval
    ...

# Lines 49, 126: fallback client timeout
settings = get_settings()
return httpx.AsyncClient(timeout=settings.provider_timeout)
```

### Task 4: Wire config into delivery_worker.py

**Files:**
- Modify: `src/gateway/wal/delivery_worker.py:28`

Read batch_size from settings:

```python
def __init__(self, wal: WALWriter) -> None:
    ...
    self._batch_size = get_settings().delivery_batch_size
```

### Task 5: Wire config into circuit.py and main.py (circuit breaker init)

**Files:**
- Modify: `src/gateway/main.py` (_init_load_balancer)
- Modify: `src/gateway/routing/circuit.py:51`

Pass settings values to CircuitBreakerRegistry:

```python
# main.py _init_load_balancer
ctx.circuit_breakers = CircuitBreakerRegistry(
    fail_max=settings.circuit_breaker_fail_max,
    reset_timeout=settings.circuit_breaker_reset_timeout,
)
```

### Task 6: Wire config into retry.py

**Files:**
- Modify: `src/gateway/routing/retry.py:33`
- Modify: caller in orchestrator.py

Pass settings.retry_max_attempts to forward_with_retry calls.

### Task 7: Wire config into health.py (disk threshold)

**Files:**
- Modify: `src/gateway/health.py:74`

Replace hardcoded `0.8` and `80`:

```python
settings = get_settings()
degraded_threshold = settings.disk_degraded_threshold
...
elif pending > high_water * degraded_threshold or disk_pct >= degraded_threshold * 100:
    payload["status"] = "degraded"
```

### Task 8: Tier 2 — Named constants in session_chain.py, budget_tracker.py, delivery_worker.py

Extract inline literals to module-level constants:
- `session_chain.py`: GENESIS_HASH already done (line 14-16) ✓
- `budget_tracker.py`: `_REDIS_PREFIX = "gateway:budget:"`, `_UNLIMITED_SENTINEL = -1`, `_TTL_BUFFER = 3600`
- `delivery_worker.py`: `_DELIVERY_PATH = "/v1/gateway/executions"`, `_PURGE_CYCLE = 60`, `_INITIAL_BACKOFF = 1.0`, `_MAX_BACKOFF = 60.0`

### Task 9: Tier 2 — Named constants in lineage reader/api, sync_client

- `lineage/api.py`: `_MAX_SESSION_LIMIT = 200`, `_MAX_ATTEMPT_LIMIT = 500`, `_DEFAULT_SESSION_LIMIT = 50`, `_DEFAULT_ATTEMPT_LIMIT = 100`
- `lineage/reader.py`: import GENESIS_HASH from session_chain
- `sync_client.py`: `_ATTESTATION_FETCH_LIMIT = 1000`, `_POLICY_FETCH_LIMIT = 500`

### Task 10: Update .env.example with new config fields

**Files:**
- Modify: `.env.example`

Add the 10 new WALACOR_ env vars with comments.

### Task 11: Run tests and verify

Run: `pytest tests/unit/ tests/compliance/ -q`
Expected: Same pass count as before (320 pass), no regressions.
