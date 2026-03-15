# Gateway v2: Hybrid Architecture & Competitive Feature Roadmap

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform Walacor Gateway from a Python-only proxy into a high-performance hybrid (Go proxy + Python intelligence sidecar), while adding key competitive features that close the gap with Bifrost, LiteLLM, Kong, Cloudflare, and FireTail.

**Architecture:** Three-stage evolution — (A) Python quick wins for immediate throughput gains, (B) competitive feature parity while still in Python, (C) Go proxy layer introduction with Python sidecar for governance intelligence.

**Tech Stack:**
- Stage A: Python 3.12, orjson, uvloop, httpx
- Stage B: Python 3.12, Presidio, Redis, protobuf
- Stage C: Go 1.22+, gRPC, protobuf, Python sidecar

**Competitive research date:** 2026-03-14
**Competitors analyzed:** Bifrost (Go), LiteLLM (Python), Kong AI Gateway (Lua/Nginx), Cloudflare AI Gateway (SaaS), FireTail (SaaS)

---

## Stage A: Python Performance Quick Wins (1-2 days)

Immediate throughput improvements with minimal code changes. No architecture change needed.

---

### Task A.1: Replace `json` with `orjson`

**Why:** orjson is 3-10x faster than stdlib json for both parsing and serialization. Every request does multiple json.loads/json.dumps in adapters, orchestrator, and WAL writer.

**Files:**
- Modify: `pyproject.toml` (add `orjson>=3.9` to dependencies)
- Modify: `src/gateway/adapters/openai.py`
- Modify: `src/gateway/adapters/anthropic.py`
- Modify: `src/gateway/adapters/ollama.py`
- Modify: `src/gateway/adapters/generic.py`
- Modify: `src/gateway/adapters/huggingface.py`
- Modify: `src/gateway/pipeline/orchestrator.py`
- Modify: `src/gateway/pipeline/hasher.py`
- Modify: `src/gateway/wal/writer.py`
- Test: `tests/unit/test_orjson_compat.py`

**Step 1: Add orjson dependency**

```toml
# pyproject.toml — add to dependencies list
"orjson>=3.9",
```

**Step 2: Create a compatibility shim**

```python
# src/gateway/util/json_utils.py
"""JSON utilities — uses orjson when available, falls back to stdlib json."""
try:
    import orjson

    def loads(data: str | bytes) -> any:
        return orjson.loads(data)

    def dumps(obj: any) -> str:
        return orjson.dumps(obj).decode("utf-8")

    def dumps_bytes(obj: any) -> bytes:
        return orjson.dumps(obj)

except ImportError:
    import json

    def loads(data: str | bytes) -> any:
        return json.loads(data)

    def dumps(obj: any) -> str:
        return json.dumps(obj)

    def dumps_bytes(obj: any) -> bytes:
        return json.dumps(obj).encode("utf-8")
```

**Step 3: Replace json imports in hot-path modules**

Replace `import json` / `json.loads` / `json.dumps` with the shim in all adapter files, orchestrator, hasher, and WAL writer.

**Step 4: Test compatibility**

```python
# tests/unit/test_orjson_compat.py
from gateway.util.json_utils import loads, dumps, dumps_bytes

def test_roundtrip():
    data = {"model": "qwen3:1.7b", "messages": [{"role": "user", "content": "hello"}]}
    assert loads(dumps(data)) == data

def test_bytes_roundtrip():
    data = {"key": "value", "number": 42}
    assert loads(dumps_bytes(data)) == data

def test_unicode():
    data = {"text": "Hello 世界 🌍"}
    assert loads(dumps(data)) == data
```

**Step 5: Run full test suite**

```bash
pytest tests/unit/ -v
```

**Step 6: Commit**

```bash
git add -A && git commit -m "perf: replace stdlib json with orjson for 3-10x faster serialization"
```

---

### Task A.2: Enable uvloop for faster asyncio event loop

**Why:** uvloop is a drop-in replacement for asyncio's event loop, built on libuv (same as Node.js). 2-4x faster for I/O-bound workloads. Bifrost's Go advantage partly comes from its event loop — uvloop closes some of that gap.

**Files:**
- Modify: `pyproject.toml` (add `uvloop>=0.19` to dependencies)
- Modify: `src/gateway/main.py` (install uvloop at import time)

**Step 1: Add uvloop dependency**

```toml
# pyproject.toml — add to dependencies
"uvloop>=0.19; sys_platform != 'win32'",
```

**Step 2: Install uvloop in main.py**

```python
# src/gateway/main.py — add at top, after imports
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass  # Fallback to default asyncio event loop
```

**Step 3: Verify with benchmark**

```bash
# Before: measure baseline
python -c "import asyncio; print(type(asyncio.get_event_loop()))"
# After: should show uvloop.Loop
```

**Step 4: Run test suite**

```bash
pytest tests/unit/ -v
```

**Step 5: Commit**

```bash
git add -A && git commit -m "perf: enable uvloop for 2-4x faster asyncio event loop"
```

---

### Task A.3: Tune httpx connection pooling

**Why:** Default httpx creates new connections frequently. For proxying to Ollama/OpenAI, persistent connections with higher pool limits reduce TCP handshake overhead.

**Files:**
- Modify: `src/gateway/main.py` (httpx client initialization)
- Modify: `src/gateway/config.py` (add pool config fields)

**Step 1: Add config fields**

```python
# config.py — add to GatewaySettings
http_pool_max_connections: int = Field(default=100, description="Max HTTP connections in pool")
http_pool_max_keepalive: int = Field(default=20, description="Max keepalive connections per host")
http_keepalive_expiry: int = Field(default=30, description="Keepalive expiry in seconds")
```

**Step 2: Update httpx client creation**

```python
# main.py — update shared client creation
limits = httpx.Limits(
    max_connections=settings.http_pool_max_connections,
    max_keepalive_connections=settings.http_pool_max_keepalive,
    keepalive_expiry=settings.http_keepalive_expiry,
)
ctx.http_client = httpx.AsyncClient(timeout=settings.provider_timeout, limits=limits)
```

**Step 3: Commit**

```bash
git add -A && git commit -m "perf: tune httpx connection pooling for lower forwarding latency"
```

---

### Task A.4: Dedicated SQLite writer thread

**Why:** Currently `asyncio.to_thread` dispatches WAL writes to the default thread pool. Each write may land on a different thread, requiring `check_same_thread=False`. A dedicated writer thread with a queue eliminates thread creation overhead and provides natural write batching.

**Files:**
- Modify: `src/gateway/wal/writer.py`
- Modify: `src/gateway/storage/wal_backend.py`
- Test: `tests/unit/test_wal_writer.py`

**Step 1: Add background writer thread to WALWriter**

```python
# wal/writer.py — add threaded write queue
import queue
import threading

class WALWriter:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        """Start the background writer thread."""
        self._running = True
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()

    def _writer_loop(self) -> None:
        """Process writes from the queue in a single dedicated thread."""
        conn = self._ensure_conn()
        while self._running:
            try:
                item = self._queue.get(timeout=0.1)
                if item is None:
                    break
                op, args = item
                op(conn, *args)
            except queue.Empty:
                continue

    def stop(self) -> None:
        """Stop the writer thread gracefully."""
        self._running = False
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5.0)
```

**Step 2: Update wal_backend.py to use queue instead of to_thread**

The WALBackend methods become simple queue puts instead of `asyncio.to_thread` calls.

**Step 3: Test thread safety**

```bash
pytest tests/unit/test_wal_writer.py -v
```

**Step 4: Commit**

```bash
git add -A && git commit -m "perf: dedicated SQLite writer thread replaces thread pool dispatch"
```

---

## Stage B: Competitive Feature Parity (2-3 weeks)

Features inspired by competitors that strengthen our product while still in Python. Ordered by customer impact.

---

### Task B.1: PII Sanitization (Strip Before LLM, Restore After)

**Inspired by:** Kong AI Gateway 3.10 — PII sanitization with round-trip restoration

**Why:** We currently detect PII and warn/block. Kong strips PII before it reaches the LLM, then restores it in the response. This is the difference between "we found PII" and "we prevented PII from ever reaching the LLM" — critical for HIPAA and GDPR.

**Files:**
- Create: `src/gateway/content/pii_sanitizer.py`
- Modify: `src/gateway/pipeline/orchestrator.py` (pre-forward sanitization, post-response restoration)
- Modify: `src/gateway/config.py` (sanitization config)
- Test: `tests/unit/test_pii_sanitizer.py`

**Design:**

```
Request flow:
  User: "My SSN is 123-45-6789 and email is john@example.com"
       ↓ [PII Sanitizer — pre-forward]
  To LLM: "My SSN is [PII_SSN_1] and email is [PII_EMAIL_1]"
       ↓ [LLM responds]
  From LLM: "I see your SSN is [PII_SSN_1]. You should never share that."
       ↓ [PII Restorer — post-response]
  To User: "I see your SSN is 123-45-6789. You should never share that."

Audit trail stores:
  - Original prompt hash (with real PII)
  - Sanitized prompt (what the LLM actually saw)
  - Mapping table (ephemeral, per-request, never persisted)
```

**Config:**

```python
# config.py
pii_sanitization_enabled: bool = Field(default=False, description="Strip PII before sending to LLM, restore in response")
pii_sanitization_mode: str = Field(default="replace", description="replace=placeholder tokens, redact=remove entirely")
pii_sanitization_types: str = Field(default="ssn,credit_card,aws_access_key,api_key", description="Comma-separated PII types to sanitize")
```

**Step 1: Build sanitizer**

```python
# src/gateway/content/pii_sanitizer.py
import re
from dataclasses import dataclass, field

@dataclass
class SanitizationResult:
    sanitized_text: str
    mapping: dict[str, str]  # placeholder -> original value
    pii_count: int

@dataclass
class PIISanitizer:
    """Replaces PII with placeholders, tracks mapping for restoration."""
    patterns: dict[str, re.Pattern]

    def sanitize(self, text: str) -> SanitizationResult:
        mapping = {}
        counter = {}
        sanitized = text
        for pii_type, pattern in self.patterns.items():
            for match in pattern.finditer(sanitized):
                count = counter.get(pii_type, 0) + 1
                counter[pii_type] = count
                placeholder = f"[PII_{pii_type.upper()}_{count}]"
                mapping[placeholder] = match.group()
                sanitized = sanitized.replace(match.group(), placeholder, 1)
        return SanitizationResult(sanitized, mapping, len(mapping))

    def restore(self, text: str, mapping: dict[str, str]) -> str:
        restored = text
        for placeholder, original in mapping.items():
            restored = restored.replace(placeholder, original)
        return restored
```

**Step 2: Wire into orchestrator pre-forward and post-response**

**Step 3: Test round-trip**

```python
def test_sanitize_and_restore():
    sanitizer = PIISanitizer(patterns=DEFAULT_PATTERNS)
    result = sanitizer.sanitize("My SSN is 123-45-6789")
    assert "123-45-6789" not in result.sanitized_text
    assert "[PII_SSN_1]" in result.sanitized_text
    restored = sanitizer.restore("Your SSN [PII_SSN_1] is sensitive", result.mapping)
    assert "123-45-6789" in restored
```

**Step 4: Commit**

```bash
git commit -m "feat: PII sanitization — strip before LLM, restore after (HIPAA/GDPR)"
```

---

### Task B.2: Audit Log Export (S3, Splunk, SIEM)

**Inspired by:** LiteLLM (SIEM log shipping), Cloudflare (Logpush)

**Why:** Enterprise customers need to ship audit logs to their SIEM (Splunk, Datadog, Elastic) or object storage (S3). We currently only write to local WAL + Walacor backend. No export capability = no enterprise deal.

**Files:**
- Create: `src/gateway/export/__init__.py`
- Create: `src/gateway/export/base.py` (ABC)
- Create: `src/gateway/export/s3_exporter.py`
- Create: `src/gateway/export/webhook_exporter.py` (generic SIEM via HTTP POST)
- Create: `src/gateway/export/file_exporter.py` (JSONL file rotation)
- Modify: `src/gateway/config.py` (export config)
- Modify: `src/gateway/pipeline/orchestrator.py` (hook export after write)
- Test: `tests/unit/test_export.py`

**Design:**

```python
# src/gateway/export/base.py
from abc import ABC, abstractmethod

class AuditExporter(ABC):
    @abstractmethod
    async def export(self, record: dict) -> None:
        """Export one audit record to the destination."""

    @abstractmethod
    async def export_batch(self, records: list[dict]) -> None:
        """Export a batch of audit records."""

    @abstractmethod
    async def close(self) -> None:
        """Cleanup resources."""
```

**Exporters:**

| Exporter | Destination | Config |
|---|---|---|
| `S3Exporter` | AWS S3 bucket | `WALACOR_EXPORT_S3_BUCKET`, `WALACOR_EXPORT_S3_PREFIX`, `WALACOR_EXPORT_S3_REGION` |
| `WebhookExporter` | Any HTTP endpoint (Splunk HEC, Datadog, Elastic) | `WALACOR_EXPORT_WEBHOOK_URL`, `WALACOR_EXPORT_WEBHOOK_HEADERS` |
| `FileExporter` | Local JSONL with rotation | `WALACOR_EXPORT_FILE_PATH`, `WALACOR_EXPORT_FILE_MAX_SIZE_MB` |

**Config:**

```python
# config.py
export_enabled: bool = Field(default=False)
export_type: str = Field(default="file", description="file, s3, webhook")
export_batch_size: int = Field(default=50, description="Batch records before exporting")
export_flush_interval: int = Field(default=30, description="Max seconds between flushes")
export_s3_bucket: str = Field(default="")
export_s3_prefix: str = Field(default="walacor-audit/")
export_s3_region: str = Field(default="us-east-1")
export_webhook_url: str = Field(default="")
export_webhook_headers: str = Field(default="", description="JSON dict of extra headers")
export_file_path: str = Field(default="/var/walacor/export/audit.jsonl")
export_file_max_size_mb: int = Field(default=100)
```

**Step 1: Build base + file exporter**

**Step 2: Build webhook exporter (covers Splunk HEC, Datadog, Elastic)**

**Step 3: Build S3 exporter (optional dep: boto3)**

**Step 4: Hook into orchestrator after _build_and_write_record**

**Step 5: Test all three exporters**

**Step 6: Commit**

```bash
git commit -m "feat: audit log export — S3, webhook (SIEM), JSONL file with rotation"
```

---

### Task B.3: Pre-Built Policy Templates (OWASP, EU AI Act, HIPAA)

**Inspired by:** FireTail — ships OWASP AI policies out of the box

**Why:** Our default is pass-all policies. New users have to write policy rules from scratch. Shipping templates reduces time-to-value from days to minutes.

**Files:**
- Create: `src/gateway/control/templates/`
- Create: `src/gateway/control/templates/owasp_llm_top10.json`
- Create: `src/gateway/control/templates/eu_ai_act_baseline.json`
- Create: `src/gateway/control/templates/hipaa_baseline.json`
- Create: `src/gateway/control/templates/soc2_baseline.json`
- Modify: `src/gateway/control/api.py` (GET /v1/control/templates, POST /v1/control/templates/{name}/apply)
- Test: `tests/unit/test_policy_templates.py`

**Templates:**

```json
// owasp_llm_top10.json
{
  "name": "OWASP LLM Top 10",
  "description": "Baseline policies for OWASP Top 10 for LLM Applications (2025)",
  "version": "1.0",
  "policies": [
    {
      "name": "LLM01: Prompt Injection Detection",
      "scope": "pre_inference",
      "rules": [{"field": "prompt.text", "operator": "not_contains", "value": "ignore previous instructions"}],
      "action": "warn"
    },
    {
      "name": "LLM02: Insecure Output Handling",
      "scope": "post_inference",
      "rules": [{"field": "pii_detected", "operator": "equals", "value": false}],
      "action": "warn"
    },
    {
      "name": "LLM06: Sensitive Information Disclosure",
      "scope": "post_inference",
      "rules": [{"field": "toxicity_flagged", "operator": "equals", "value": false}],
      "action": "block"
    }
  ]
}
```

**API:**

```
GET  /v1/control/templates                    → list available templates
GET  /v1/control/templates/{name}             → view template details
POST /v1/control/templates/{name}/apply       → apply template (creates policies)
```

**Step 1: Create template JSON files**

**Step 2: Add template API endpoints**

**Step 3: Test template loading and application**

**Step 4: Commit**

```bash
git commit -m "feat: pre-built policy templates — OWASP LLM Top 10, EU AI Act, HIPAA, SOC 2"
```

---

### Task B.4: Semantic Caching

**Inspired by:** Bifrost — vector embedding cache, 70% cost reduction, 5ms cache hits

**Why:** Identical or semantically similar questions hit the LLM every time. A semantic cache returns cached responses for similar queries, saving cost and latency.

**Files:**
- Create: `src/gateway/cache/__init__.py`
- Create: `src/gateway/cache/semantic_cache.py`
- Modify: `src/gateway/pipeline/orchestrator.py` (cache check before forward, cache store after)
- Modify: `src/gateway/config.py` (cache config)
- Test: `tests/unit/test_semantic_cache.py`

**Design:**

```
Request flow:
  1. Hash prompt text → check exact-match cache (O(1), Redis/dict)
  2. If miss → generate embedding → cosine similarity search in vector store
  3. If similarity > threshold (default 0.95) → return cached response
  4. If miss → forward to LLM → store response + embedding in cache
```

**Two-tier caching:**

| Tier | Speed | Storage | When |
|---|---|---|---|
| Exact match | <1ms | Redis hash or in-memory dict | Same prompt text |
| Semantic match | ~60ms | Vector store (pgvector / in-memory FAISS) | Similar meaning |

**Config:**

```python
# config.py
semantic_cache_enabled: bool = Field(default=False)
semantic_cache_backend: str = Field(default="memory", description="memory, redis")
semantic_cache_ttl: int = Field(default=3600, description="Cache TTL in seconds")
semantic_cache_max_entries: int = Field(default=10000)
semantic_cache_similarity_threshold: float = Field(default=0.95, description="Cosine similarity threshold for cache hit")
semantic_cache_embedding_model: str = Field(default="", description="Ollama model for embeddings, empty=exact-match only")
```

**Phase 1 (exact match only — no embedding model needed):**

```python
# src/gateway/cache/semantic_cache.py
import hashlib
import time

class SemanticCache:
    def __init__(self, max_entries=10000, ttl=3600):
        self._cache: dict[str, tuple[bytes, float]] = {}  # hash -> (response_body, timestamp)
        self._max_entries = max_entries
        self._ttl = ttl

    def _hash_prompt(self, prompt: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()

    def get(self, prompt: str, model: str) -> bytes | None:
        key = self._hash_prompt(prompt, model)
        entry = self._cache.get(key)
        if entry is None:
            return None
        body, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            return None
        return body

    def put(self, prompt: str, model: str, response_body: bytes) -> None:
        if len(self._cache) >= self._max_entries:
            # Evict oldest
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        key = self._hash_prompt(prompt, model)
        self._cache[key] = (response_body, time.monotonic())
```

**Phase 2 (semantic — add embedding-based similarity later):**
Add Ollama embedding generation + FAISS/numpy cosine similarity when embedding model is configured.

**Step 1: Build exact-match cache**

**Step 2: Wire into orchestrator (check before forward, store after)**

**Step 3: Add cache hit/miss metrics (Prometheus counters)**

**Step 4: Test cache behavior**

**Step 5: Commit**

```bash
git commit -m "feat: semantic caching — exact-match tier, configurable TTL and max entries"
```

---

### Task B.5: Per-Key Guardrail and Policy Assignment

**Inspired by:** LiteLLM — different API keys have different guardrails active

**Why:** Multi-tenant deployments need different content policies per customer. A healthcare client needs strict PII blocking; a creative writing client needs relaxed toxicity thresholds. Currently our policies are global.

**Files:**
- Modify: `src/gateway/control/store.py` (add key-policy mapping table)
- Modify: `src/gateway/control/api.py` (CRUD for key-policy assignments)
- Modify: `src/gateway/pipeline/orchestrator.py` (resolve policies per key)
- Modify: `src/gateway/config.py` (per-key policy config)
- Test: `tests/unit/test_per_key_policies.py`

**Design:**

```sql
-- New table in control plane
CREATE TABLE IF NOT EXISTS key_policy_assignments (
    api_key_hash  TEXT NOT NULL,
    policy_id     TEXT NOT NULL,
    PRIMARY KEY (api_key_hash, policy_id)
);
```

**API:**

```
GET    /v1/control/keys/{key_hash}/policies           → list policies for key
PUT    /v1/control/keys/{key_hash}/policies            → set policies for key
DELETE /v1/control/keys/{key_hash}/policies/{policy_id} → remove policy from key
```

**Step 1: Add database table**

**Step 2: Add API endpoints**

**Step 3: Modify orchestrator to resolve per-key policies**

**Step 4: Test multi-tenant policy isolation**

**Step 5: Commit**

```bash
git commit -m "feat: per-key policy assignment — multi-tenant guardrail isolation"
```

---

### Task B.6: Token-Based Rate Limiting

**Inspired by:** Kong AI Gateway — rate limits by prompt/completion/total tokens, not just request count

**Why:** A 10-token request and a 100K-token request both count as "1 request" in our current budget system. Token-based rate limiting is more accurate and fair.

**Files:**
- Create: `src/gateway/middleware/token_rate_limiter.py`
- Modify: `src/gateway/config.py` (rate limit config)
- Modify: `src/gateway/main.py` (add middleware)
- Test: `tests/unit/test_token_rate_limiter.py`

**Design:**

```python
# Sliding window token rate limiter
# Key: (api_key or user_id, window_period)
# Value: tokens consumed in current window

# Config:
token_rate_limit_enabled: bool = False
token_rate_limit_window: int = 60  # seconds
token_rate_limit_max_tokens: int = 100000  # per window
token_rate_limit_scope: str = "user"  # user, key, tenant
```

**Step 1: Build sliding window rate limiter**

**Step 2: Add middleware that checks token consumption**

**Step 3: Return 429 with Retry-After header when exceeded**

**Step 4: Test rate limiting behavior**

**Step 5: Commit**

```bash
git commit -m "feat: token-based rate limiting — sliding window per user/key/tenant"
```

---

### Task B.7: Parallel Content Analysis (`during_call` mode)

**Inspired by:** LiteLLM — `during_call` guardrails run in parallel with LLM call

**Why:** Our content analysis (PII, toxicity, Llama Guard) currently runs sequentially after the LLM response. Running input analysis in parallel with the LLM call reduces total latency.

**Files:**
- Modify: `src/gateway/pipeline/orchestrator.py` (parallel analysis)
- Modify: `src/gateway/config.py` (parallel analysis config)
- Test: `tests/unit/test_parallel_analysis.py`

**Design:**

```python
# Current (sequential):
#   Forward to LLM → Wait for response → Run content analysis → Return
#   Total: LLM_time + analysis_time

# New (parallel input analysis):
#   Forward to LLM ─────────────────────┐
#   Analyze input (PII, toxicity) ──────┤ (parallel)
#                                       ↓
#   Both done → Run output analysis → Return
#   Total: max(LLM_time, input_analysis_time) + output_analysis_time
```

**Config:**

```python
content_analysis_parallel: bool = Field(default=True, description="Run input content analysis in parallel with LLM call")
```

**Step 1: Use asyncio.gather for parallel execution**

```python
# orchestrator.py — in non-streaming path
if settings.content_analysis_parallel:
    (http_response, model_response, _), input_analysis = await asyncio.gather(
        _forward_with_resilience(adapter, call, request),
        _analyze_input_async(call.prompt_text, settings, ctx),
    )
else:
    http_response, model_response, _ = await _forward_with_resilience(adapter, call, request)
    input_analysis = None
```

**Step 2: Merge input analysis results with post-inference analysis**

**Step 3: Test parallel execution timing**

**Step 4: Commit**

```bash
git commit -m "feat: parallel content analysis — input analysis runs during LLM call"
```

---

### Task B.8: DLP Data Classification

**Inspired by:** Cloudflare — DLP scanning for financial data, health records, secrets beyond PII

**Why:** Our PII detector catches SSN, credit cards, API keys. But it misses financial data (account numbers, routing numbers), health data (ICD codes, drug names), and code secrets (private keys, connection strings).

**Files:**
- Create: `src/gateway/content/dlp_classifier.py`
- Modify: `src/gateway/content/pii_detector.py` (extend patterns)
- Modify: `src/gateway/config.py` (DLP config)
- Test: `tests/unit/test_dlp_classifier.py`

**Categories to add:**

| Category | Examples | Action |
|---|---|---|
| Financial | Bank account numbers, routing numbers, SWIFT codes | WARN |
| Health (PHI) | ICD-10 codes, drug names with dosages, patient IDs | BLOCK (HIPAA) |
| Secrets | RSA private keys, connection strings, JWT tokens, .env contents | BLOCK |
| Infrastructure | IP addresses with ports, database URLs, AWS ARNs | WARN |

**Config:**

```python
dlp_enabled: bool = Field(default=False)
dlp_categories: str = Field(default="financial,health,secrets,infrastructure")
dlp_action_financial: str = Field(default="warn")
dlp_action_health: str = Field(default="block")
dlp_action_secrets: str = Field(default="block")
```

**Step 1: Build DLP classifier with regex patterns per category**

**Step 2: Integrate with existing content analysis pipeline**

**Step 3: Test detection accuracy**

**Step 4: Commit**

```bash
git commit -m "feat: DLP data classification — financial, health, secrets, infrastructure detection"
```

---

### Task B.9: A/B Model Testing

**Inspired by:** Cloudflare — dynamic routing with A/B testing per user segment

**Why:** When evaluating a new model (e.g., comparing qwen3:1.7b vs qwen3:4b), we need to split traffic and compare quality/cost/latency. Currently we route all traffic to one model.

**Files:**
- Create: `src/gateway/routing/ab_test.py`
- Modify: `src/gateway/pipeline/orchestrator.py` (A/B routing)
- Modify: `src/gateway/config.py` (A/B config)
- Modify: `src/gateway/lineage/api.py` (A/B comparison endpoint)
- Test: `tests/unit/test_ab_test.py`

**Design:**

```python
# Config: JSON-based A/B test definitions
# WALACOR_AB_TESTS_JSON='[{"name":"qwen-size-test","model_pattern":"qwen3:*","variants":[{"model":"qwen3:1.7b","weight":50},{"model":"qwen3:4b","weight":50}]}]'

@dataclass
class ABTest:
    name: str
    model_pattern: str  # fnmatch pattern on requested model
    variants: list[ABVariant]

@dataclass
class ABVariant:
    model: str
    weight: int  # percentage (weights must sum to 100)
```

**Step 1: Build A/B test router**

**Step 2: Store variant assignment in execution record metadata**

**Step 3: Add comparison endpoint to lineage API**

```
GET /v1/lineage/ab-tests/{name}/results → compare variants by latency, tokens, cost
```

**Step 4: Test variant distribution**

**Step 5: Commit**

```bash
git commit -m "feat: A/B model testing — traffic splitting with lineage comparison"
```

---

### Task B.10: MCP Tool Access Control Per Key

**Inspired by:** Bifrost — MCP tool filtering per virtual key with strict allow-lists

**Why:** We register tools globally. All users can access all tools. An admin should be able to restrict which tools (web_search, code_interpreter, etc.) each API key can use.

**Files:**
- Modify: `src/gateway/control/store.py` (key-tool mapping table)
- Modify: `src/gateway/control/api.py` (CRUD for key-tool assignments)
- Modify: `src/gateway/pipeline/orchestrator.py` (filter tools per key before injection)
- Test: `tests/unit/test_tool_access_control.py`

**Design:**

```sql
CREATE TABLE IF NOT EXISTS key_tool_permissions (
    api_key_hash  TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    allowed       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (api_key_hash, tool_name)
);
```

**Step 1: Add database table and API endpoints**

**Step 2: Filter tool definitions in _run_pre_checks based on key permissions**

**Step 3: Test tool access isolation**

**Step 4: Commit**

```bash
git commit -m "feat: per-key MCP tool access control — allow-list per API key"
```

---

## Stage C: Hybrid Go/Python Architecture (4-6 weeks)

The big architectural shift. Go handles the proxy hot path; Python handles governance intelligence via gRPC sidecar.

---

### Task C.1: Define gRPC Interface (Protobuf Schema)

**Files:**
- Create: `proto/governance.proto`
- Create: `proto/Makefile` (codegen for Go + Python)

**Step 1: Write protobuf schema**

```protobuf
syntax = "proto3";
package walacor.governance.v1;

service GovernanceEngine {
  // Pre-inference: auth, attestation, policy, budget check
  rpc EvaluatePreInference(PreInferenceRequest) returns (PreInferenceResult);

  // Post-inference: content analysis, policy evaluation
  rpc EvaluatePostInference(PostInferenceRequest) returns (PostInferenceResult);

  // Record execution (fire-and-forget, async)
  rpc RecordExecution(ExecutionRecord) returns (WriteResult);

  // Session chain
  rpc NextChainValues(ChainRequest) returns (ChainValues);
  rpc UpdateChain(ChainUpdate) returns (ChainResult);

  // Content analysis (can run in parallel)
  rpc AnalyzeContent(ContentPayload) returns (AnalysisResult);

  // Tool execution
  rpc ExecuteTool(ToolRequest) returns (ToolResponse);

  // Cache operations
  rpc CacheGet(CacheKey) returns (CacheEntry);
  rpc CachePut(CacheEntry) returns (CacheResult);

  // Health
  rpc HealthCheck(Empty) returns (HealthStatus);
}

message PreInferenceRequest {
  string api_key = 1;
  string model_id = 2;
  string provider = 3;
  string prompt_text = 4;
  string tenant_id = 5;
  string user_id = 6;
  map<string, string> metadata = 7;
  repeated ToolDefinition tools = 8;
}

message PreInferenceResult {
  bool allowed = 1;
  string attestation_id = 2;
  string policy_result = 3;
  int32 policy_version = 4;
  optional int64 budget_remaining = 5;
  string tool_strategy = 6;
  string denial_reason = 7;
  int32 denial_status_code = 8;
}

message PostInferenceRequest {
  string content = 1;
  string thinking_content = 2;
  string model_id = 3;
  string provider = 4;
  map<string, string> audit_metadata = 5;
  repeated ToolInteraction tool_interactions = 6;
}

message PostInferenceResult {
  string policy_result = 1;
  repeated PolicyDecision decisions = 2;
  bool blocked = 3;
  string block_reason = 4;
}

message ExecutionRecord {
  string execution_id = 1;
  string model_id = 2;
  string provider = 3;
  string prompt_text = 4;
  string response_content = 5;
  string attestation_id = 6;
  int32 policy_version = 7;
  string policy_result = 8;
  int64 latency_ms = 9;
  int32 prompt_tokens = 10;
  int32 completion_tokens = 11;
  map<string, string> metadata = 12;
}
```

**Step 2: Generate Go and Python stubs**

```makefile
# proto/Makefile
generate:
    protoc --go_out=. --go-grpc_out=. governance.proto
    python -m grpc_tools.protoc -I. --python_out=../src/gateway/grpc --grpc_python_out=../src/gateway/grpc governance.proto
```

**Step 3: Commit**

```bash
git commit -m "feat: define gRPC interface for Go proxy ↔ Python intelligence layer"
```

---

### Task C.2: Python gRPC Server (Intelligence Sidecar)

**Files:**
- Create: `src/gateway/grpc/server.py`
- Create: `src/gateway/grpc/handlers.py`
- Modify: `src/gateway/main.py` (start gRPC server alongside ASGI)
- Modify: `pyproject.toml` (add grpcio dependency)

**Design:**

The Python sidecar exposes all governance intelligence via gRPC:
- Pre/post-inference policy evaluation
- Content analysis (PII, toxicity, Llama Guard, DLP)
- Session chain management
- Audit record hashing and writing
- Tool execution (MCP, web search)

```python
# src/gateway/grpc/server.py
import grpc
from concurrent import futures

class GovernanceServicer(governance_pb2_grpc.GovernanceEngineServicer):
    def __init__(self, ctx, settings):
        self.ctx = ctx
        self.settings = settings

    async def EvaluatePreInference(self, request, context):
        # Reuse existing _run_pre_checks logic
        ...

    async def EvaluatePostInference(self, request, context):
        # Reuse existing _run_response_policy logic
        ...

    async def RecordExecution(self, request, context):
        # Reuse existing _build_and_write_record logic
        ...
```

**Step 1: Implement gRPC servicer wrapping existing orchestrator logic**

**Step 2: Start gRPC server on port 50051 alongside ASGI on 8000**

**Step 3: Test gRPC endpoints independently**

**Step 4: Commit**

```bash
git commit -m "feat: Python gRPC intelligence sidecar — wraps governance pipeline"
```

---

### Task C.3: Go Proxy — Minimal Viable Proxy

**Files:**
- Create: `proxy/` (new Go module at repo root)
- Create: `proxy/go.mod`
- Create: `proxy/main.go`
- Create: `proxy/config/config.go`
- Create: `proxy/handler/proxy.go`
- Create: `proxy/handler/streaming.go`
- Create: `proxy/grpc/client.go`
- Create: `proxy/middleware/auth.go`
- Create: `proxy/middleware/ratelimit.go`
- Create: `proxy/cache/semantic.go`
- Create: `proxy/Dockerfile`
- Modify: `deploy/docker-compose.yml` (add go-proxy service)

**Design:**

```go
// proxy/main.go
package main

import (
    "log"
    "net/http"
    "proxy/config"
    "proxy/handler"
    "proxy/grpc"
    "proxy/middleware"
)

func main() {
    cfg := config.Load()

    // Connect to Python intelligence sidecar
    brain, err := grpc.NewClient(cfg.BrainAddr)
    if err != nil {
        log.Fatal(err)
    }
    defer brain.Close()

    // Build handler chain
    proxy := handler.NewProxy(cfg, brain)

    // Middleware stack
    h := middleware.Chain(
        middleware.Auth(cfg),
        middleware.RateLimit(cfg),
        middleware.Cache(cfg),
        proxy.Handle,
    )

    // Serve
    log.Printf("Go proxy listening on %s", cfg.ListenAddr)
    log.Fatal(http.ListenAndServe(cfg.ListenAddr, h))
}
```

```go
// proxy/handler/proxy.go
func (p *Proxy) Handle(w http.ResponseWriter, r *http.Request) {
    // 1. Pre-inference check (gRPC call to Python)
    preResult, err := p.brain.EvaluatePreInference(r.Context(), &pb.PreInferenceRequest{...})
    if !preResult.Allowed {
        http.Error(w, preResult.DenialReason, int(preResult.DenialStatusCode))
        return
    }

    // 2. Forward to LLM provider
    if isStreaming(r) {
        p.handleStreaming(w, r, preResult)
    } else {
        p.handleNonStreaming(w, r, preResult)
    }
}
```

```go
// proxy/handler/streaming.go — the key advantage
func (p *Proxy) handleStreaming(w http.ResponseWriter, r *http.Request, pre *pb.PreInferenceResult) {
    // Forward to LLM
    resp, err := p.client.Do(proxyReq)

    flusher := w.(http.Flusher)
    var buffer bytes.Buffer

    // Stream chunks to client immediately
    scanner := bufio.NewScanner(resp.Body)
    for scanner.Scan() {
        chunk := scanner.Bytes()
        w.Write(chunk)
        w.Write([]byte("\n"))
        flusher.Flush()
        buffer.Write(chunk) // accumulate for audit
    }

    // After stream completes, fire-and-forget to Python for governance
    go func() {
        p.brain.RecordExecution(context.Background(), &pb.ExecutionRecord{
            Content: buffer.String(),
            ...
        })
    }()
}
```

**Step 1: Initialize Go module and basic proxy**

**Step 2: Add gRPC client for Python sidecar**

**Step 3: Implement streaming with zero-copy forwarding**

**Step 4: Add auth, rate limiting, caching middleware**

**Step 5: Create Dockerfile and docker-compose entry**

**Step 6: Test end-to-end: Go proxy → LLM, with Python sidecar for governance**

**Step 7: Benchmark against Python-only gateway**

**Step 8: Commit**

```bash
git commit -m "feat: Go proxy layer — streaming, auth, rate limiting, gRPC to Python sidecar"
```

---

### Task C.4: Docker Compose — Hybrid Deployment

**Files:**
- Modify: `deploy/docker-compose.yml`
- Create: `deploy/docker-compose.hybrid.yml` (override for hybrid mode)

**Design:**

```yaml
# docker-compose.hybrid.yml
services:
  proxy:
    build: ../proxy
    ports:
      - "${GATEWAY_PORT:-8000}:8000"
    environment:
      - BRAIN_ADDR=brain:50051
      - LLM_PROVIDERS=ollama:http://ollama:11434
    depends_on:
      - brain
      - ollama

  brain:
    build: .
    command: python -m gateway.grpc.server
    expose:
      - "50051"
    volumes:
      - walacor-wal:/var/walacor/wal
    environment:
      - WALACOR_GRPC_MODE=true

  ollama:
    image: ollama/ollama:latest
    ...
```

**Usage:**

```bash
# Python-only (current, default)
docker compose up

# Hybrid (Go proxy + Python sidecar)
docker compose -f docker-compose.yml -f docker-compose.hybrid.yml up
```

---

## Summary: Full Roadmap Timeline

| Stage | Tasks | Effort | Key Deliverables |
|---|---|---|---|
| **A: Quick Wins** | A.1–A.4 | 1-2 days | orjson, uvloop, connection pooling, dedicated writer thread |
| **B: Competitive Features** | B.1–B.10 | 2-3 weeks | PII sanitization, audit export, policy templates, semantic caching, per-key policies, token rate limiting, parallel analysis, DLP, A/B testing, tool ACLs |
| **C: Hybrid Architecture** | C.1–C.4 | 4-6 weeks | Protobuf schema, Python gRPC sidecar, Go proxy, hybrid Docker deployment |

**Total: ~8-10 weeks to full hybrid with competitive feature parity.**

## Competitive Gap Analysis After Implementation

| Feature | Bifrost | LiteLLM | Kong | Cloudflare | FireTail | **Walacor (After)** |
|---|---|---|---|---|---|---|
| Proxy speed | 11μs (Go) | ~5ms (Python) | ~1ms (Nginx) | Edge | N/A | **<100μs (Go)** |
| Cryptographic audit | No | No | No | No | No | **SHA3-512 chains** |
| Session Merkle chains | No | No | No | No | No | **Yes** |
| PII sanitization | No | Presidio | 20+ categories | DLP | No | **Yes (round-trip)** |
| Semantic caching | Yes | No | Yes | Yes | No | **Yes** |
| Policy templates | No | No | No | No | OWASP | **OWASP+EU AI Act+HIPAA+SOC2** |
| Per-key policies | No | Yes | Plugin | No | Yes | **Yes** |
| Token rate limiting | Budget | Budget | Yes | Yes | No | **Yes** |
| Audit export (SIEM) | Prometheus | Yes | Plugin | Logpush | Yes | **S3+Webhook+File** |
| A/B testing | No | No | No | Yes | No | **Yes** |
| Tool access control | Yes | No | No | No | No | **Yes** |
| Parallel analysis | No | during_call | No | No | No | **Yes** |
| EU AI Act compliance | No | No | No | No | Framework | **Full mapping** |

**After Stage C, Walacor Gateway would be the only solution combining Go-level proxy speed with cryptographic audit trails, PII sanitization, and full compliance toolkit.**
