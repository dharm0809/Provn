# Performance Optimization Design — Pure Python Gateway

**Date**: 2026-03-16
**Target**: m6a.xlarge (4 vCPU, 16GB RAM, CPU-only), 10-100 req/s
**Scope**: 7 fixes across 10 files, no behavioral changes

---

## 1. WAL Commit Batching

**File**: `src/gateway/wal/writer.py`

**Problem**: Each enqueued write calls `conn.commit()` immediately. At 100 req/s = 100+ fsync/s.

**Fix**: Writer thread drains queue in batches — up to 50 items or 10ms elapsed — then issues a single `conn.commit()`.

- Batch window: 10ms (configurable)
- Max batch size: 50
- If only 1 item arrives, commits after 10ms timeout
- Same `synchronous=NORMAL` + WAL mode guarantees

**Gain**: ~10x reduction in commit overhead at 100 req/s.

---

## 2. Unbounded Dicts → Bounded LRU

**File**: `src/gateway/pipeline/orchestrator.py`

**Problem**: `_model_capabilities` and `_concurrency_limiters` grow without bounds.

**Fix**:
- `_model_capabilities`: `LRUCache(maxsize=500)` — evicted entries re-discovered via retry
- `_concurrency_limiters`: `LRUCache(maxsize=100)` — evicted limiters reset to defaults

**Gain**: Bounded memory. O(1) lookup preserved.

---

## 3. Content Analysis Cache TTL

**File**: `src/gateway/pipeline/response_evaluator.py`

**Problem**: `LRUCache(maxsize=5000)` has no TTL. Stale results persist after policy changes.

**Fix**: Replace with `TTLCache(maxsize=5000, ttl=60)` from cachetools. `clear_analysis_cache()` still works for manual invalidation.

**Gain**: Correctness — stale verdicts expire within 60s of policy changes.

---

## 4. Completeness Middleware Timeout

**Files**: `src/gateway/middleware/completeness.py`, `src/gateway/config.py`

**Problem**: Hardcoded 5s timeout on storage writes. Slow storage backs up requests.

**Fix**: Add `completeness_timeout: float = 2.0` to config. Use in middleware instead of hardcoded 5.0.

**Gain**: Tail latency reduced by 3s on slow storage events.

---

## 5. Session Chain OrderedDict Eviction

**File**: `src/gateway/pipeline/session_chain.py`

**Problem**: Eviction calls `min()` over all 10k sessions — O(n) scan.

**Fix**: Replace `dict` with `OrderedDict`. `move_to_end()` on access, `popitem(last=False)` to evict. TTL sweep unchanged.

**Gain**: Eviction O(n) → O(1).

---

## 6. Request Body Double-Parse Elimination

**Files**: 5 adapters — `ollama.py`, `openai.py`, `anthropic.py`, `huggingface.py`, `generic.py`

**Problem**: `_peek_model_id()` parses and caches body on `request.state._parsed_body`. All adapters ignore the cache and re-parse via `json.loads(await request.body())`.

**Fix**: Each adapter checks `getattr(request.state, "_parsed_body", None)` before parsing. Falls back to `json.loads()` if not cached.

**Gain**: ~1-3ms saved per request (skip one JSON parse of full body).

---

## 7. Connection Pool Tuning for m6a.xlarge

**File**: `.env.example`

**Problem**: Defaults tuned for cloud multi-provider. Single local Ollama on CPU needs different settings.

**Fix**: Document recommended config:
```
WALACOR_PROVIDER_CONNECT_TIMEOUT=3.0
WALACOR_HTTP_POOL_MAX_CONNECTIONS=50
WALACOR_HTTP_POOL_MAX_KEEPALIVE=10
WALACOR_HTTP_KEEPALIVE_EXPIRY=60
WALACOR_PROVIDER_TIMEOUT=90.0
```

**Gain**: Faster failure detection (3s vs 10s), less memory, better connection reuse.
