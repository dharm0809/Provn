# Walacor AI Security Gateway — Phase 9–13 Implementation Plan

**Status:** Historical artifact. Phase 13 was implemented as an **ID-pointer chain** (record_id + previous_record_id), not a SHA3-512 Merkle chain — the Walacor backend issues the tamper-evident `DH` on ingest. Treat the Merkle-chain language in this file as the original design intent; the shipped architecture supersedes it.
**Version:** 2.0 | February 17, 2026
**Classification:** Company Confidential
**Builds On:** implementation.md (Phases 1–8, Core Guarantees G1–G3)
**Prepared by:** Quantitative Analytics Engineering, Walacor Corporation, Bethesda, MD

---

## 1. Executive Summary

Phases 1–8 delivered a functional AI Security Gateway with three guarantees: attestation verification (G1), cryptographic hash-only recording (G2), and pre-inference policy enforcement (G3). The core product works. This document defines Phases 9–13, which transform the gateway from a compliance proxy into a full AI security platform.

### 1.1 What Changes

| Phase | Name | New Capability | New Guarantee |
|-------|------|----------------|---------------|
| 9 | Infrastructure Hardening | Connection pooling, settings cache, JSON logging, WAL compaction, stream limits, startup self-test, **Completeness Invariant** | — (fixes defects) |
| 10 | Response Gate | Post-inference policy evaluation on model responses, **semantic plugin interface** | **G4** |
| 11 | Token Budget Governance | Per-tenant/user token tracking, budget enforcement, cost visibility | — (cost governance) |
| 12 | Audit-Only Mode | Shadow enforcement — log what would be blocked without blocking | — (adoption enabler) |
| 13 | Session Chain Integrity | Merkle chain linking execution records within conversations | **G5** |

### 1.2 Why These Five Phases

**Phase 9 is non-negotiable.** Without connection pooling, the gateway creates a new TCP+TLS handshake per request (~50–100ms overhead). Without WAL compaction, delivered records accumulate forever (~8.6M records/day at 100 req/s). Without stream buffer limits, a single large response can OOM the container. These are production defects, not features.

**Phase 10 (Response Gate) is the single highest-value feature.** The current gateway governs what goes OUT to providers but has zero governance on what comes BACK. Responses containing PII, leaked training data, harmful content, or prompt injection payloads flow through unexamined. G4 closes this gap — the gateway becomes a complete security perimeter.

**Phase 11 (Token Budget) answers the #1 enterprise procurement question:** "Can we set spending limits?" You already have `ModelResponse.usage` with token counts but discard them today.

**Phase 12 (Audit-Only) is critical for enterprise adoption.** No enterprise flips to `enforced` mode on day one. They need to observe what would be blocked, tune policies, build confidence, then enforce. Today `audit_only` barely differs from `enforced`. This phase makes it a real onboarding path.

**Phase 13 (Session Chains) adds a guarantee no competitor has.** A Merkle chain linking records within a session proves the complete, unaltered conversation history. If any record is removed, inserted, or modified, the chain breaks. For FedRAMP, HIPAA, and financial compliance, this is a differentiator.

### 1.3 What We Deliberately Do NOT Build

| Rejected Feature | Reason |
|-----------------|--------|
| **Semantic fingerprinting (SimHash)** | Similarity analysis is an offline/batch problem. The control plane should do this from execution records it already has. Adding LSH to the hot path adds latency for something better done as analytics. |
| **Provider circuit breaker / failover routing** | This enters API gateway territory (Kong, Envoy, LiteLLM). Our moat is security/compliance, not routing. Competing on their turf dilutes focus. Enterprises can put an API gateway in front of us. |
| **Push-based sync (SSE from control plane)** | 60-second pull interval is acceptable for V1. Model revocation is a rare, planned operation. Push adds connection management, reconnection, ordering guarantees — significant complexity for marginal improvement. |
| **Request signing (non-repudiation)** | Requires PKI (key generation, rotation, trust establishment). Build when DoD/IC customers explicitly require it. Current HTTPS + gateway_id provides sufficient integrity for commercial customers. |
| **Config hot-reload (SIGHUP)** | Kubernetes rolling deployments handle config changes. SIGHUP reload introduces race conditions (settings change mid-request) and testing complexity. |
| **Provider retry with idempotency key** | AI responses are non-deterministic. Retrying produces a different response — which one do you hash? Which do you record? This breaks G2 integrity. Let the caller retry. |

---

## 2. Architecture Impact

### 2.1 Current Pipeline (Phases 1–8)

```
Request → API Key Auth → Adapter → Attestation (G1) → Pre-Policy (G3) → Forward → Hash → WAL (G2) → Response
```

### 2.2 Pipeline After Phase 13

```
Request → API Key Auth → Adapter → Attestation (G1)
       → Pre-Policy (G3) → [Budget Check] → Forward
       → Post-Policy (G4) → Hash → [Session Chain (G5)] → WAL (G2)
       → Response
```

New steps in brackets. The pipeline grows from 5 steps to 8, but the new steps are lightweight:
- Budget check: in-memory counter comparison (< 1μs)
- Post-policy: content analysis on response text (< 5ms for regex-based)
- Session chain: one SHA3-512 hash of previous record (< 0.1ms)

### 2.3 Execution Record Changes

Current `ExecutionRecord` fields:
```
execution_id, model_attestation_id, prompt_hash, response_hash,
policy_version, policy_result, tenant_id, gateway_id, timestamp,
user, session_id, metadata
```

After Phase 13, new fields:
```
+ response_policy_version    (Phase 10 — which response policy was evaluated)
+ response_policy_result     (Phase 10 — pass | blocked | flagged)
+ content_flags              (Phase 10 — list of detected categories, e.g. ["pii", "toxicity"])
+ token_usage                (Phase 11 — {prompt_tokens, completion_tokens, total_tokens})
+ budget_remaining           (Phase 11 — tokens remaining in budget at time of request)
+ enforcement_mode           (Phase 12 — "enforced" | "audit_only")
+ would_have_blocked         (Phase 12 — True if audit_only mode would have blocked)
+ sequence_number            (Phase 13 — monotonic within session)
+ previous_record_hash       (Phase 13 — SHA3-512 of previous record in session chain)
+ record_hash                (Phase 13 — SHA3-512 of this record, for chain verification)
```

> *These fields are additive. Existing control plane endpoints accept unknown fields gracefully (Pydantic `extra="ignore"` or `extra="allow"`). The gateway can be upgraded before the control plane — new fields are simply stored in `metadata` until the control plane schema is updated.*

### 2.4 New Files

```
src/gateway/
├── pipeline/
│   ├── response_evaluator.py      ← Phase 10: post-inference policy
│   ├── budget_tracker.py          ← Phase 11: token budget enforcement
│   └── session_chain.py           ← Phase 13: Merkle chain builder
├── content/
│   ├── __init__.py                ← Phase 10: content analyzer framework
│   ├── base.py                    ← Phase 10: ContentAnalyzer ABC
│   ├── pii_detector.py            ← Phase 10: PII pattern detection
│   └── toxicity_detector.py       ← Phase 10: harmful content detection
└── (modified files)
    ├── pipeline/orchestrator.py   ← All phases: new pipeline steps
    ├── pipeline/forwarder.py      ← Phase 9: connection pool, buffer limits
    ├── pipeline/context.py        ← All phases: new context fields
    ├── pipeline/hasher.py         ← Phase 13: chain hash computation
    ├── config.py                  ← All phases: new config vars
    ├── health.py                  ← Phase 9, 11: new health fields
    ├── main.py                    ← Phase 9, 11, 12: startup changes
    ├── wal/writer.py              ← Phase 9: compaction
    └── wal/delivery_worker.py     ← Phase 9: logging improvements
```

---

## 3. Phase 9: Infrastructure Hardening

**Goal:** Fix production defects. No new features — only correctness, performance, and operability.

### 3.1 Connection Pooling

**Problem:** Every request in `forwarder.py` creates a new `httpx.AsyncClient`, which opens a new TCP+TLS connection. At 100 req/s, that's 100 handshakes/second (~50–100ms each). This is the single largest latency bottleneck.

**Solution:** Create a shared `httpx.AsyncClient` on `PipelineContext` at startup. Configure connection pooling with keep-alive.

```python
# pipeline/context.py — new field
self.http_client: httpx.AsyncClient | None = None

# main.py — on_startup
ctx.http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(60.0, connect=10.0),
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    http2=True,
)

# main.py — on_shutdown
if ctx.http_client:
    await ctx.http_client.aclose()

# forwarder.py — use shared client
async def forward(adapter, call, request) -> tuple[Response, ModelResponse]:
    ctx = get_pipeline_context()
    client = ctx.http_client or httpx.AsyncClient(timeout=60.0)
    # ... use client instead of creating new one
```

**Impact:** Reduces per-request latency by 50–100ms. Enables HTTP/2 multiplexing (multiple requests over one connection).

### 3.2 Settings Caching

**Problem:** `get_settings()` creates a new `Settings()` instance on every call, re-parsing environment variables. Called 3–5 times per request.

**Solution:** Cache with `functools.lru_cache`.

```python
from functools import lru_cache

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

**Impact:** Eliminates redundant env parsing. Settings become immutable for the process lifetime (correct behavior — config changes require restart, which aligns with Kubernetes rolling deployments).

### 3.3 Structured JSON Logging with Request ID

**Problem:** Current logging uses `logging.basicConfig` with unstructured text. No correlation between log lines for the same request. Cannot debug production issues.

**Solution:**

1. Generate `request_id` (UUID) at the start of each request
2. Store in context variable (Python `contextvars`)
3. Custom JSON log formatter that includes `request_id`, `timestamp`, `level`, `component`
4. Propagate `X-Request-ID` header to providers and back to callers

```python
# util/request_id.py
import contextvars, uuid
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

def new_request_id() -> str:
    rid = str(uuid.uuid4())
    request_id_var.set(rid)
    return rid
```

```python
# util/json_logger.py
class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(""),
        })
```

**Impact:** Every log line is machine-parseable. Every log line for a request shares a `request_id`. Compatible with ELK, Datadog, Splunk, CloudWatch.

### 3.4 WAL Compaction

**Problem:** Delivered records are never deleted. At 100 req/s, 8.6M records/day accumulate. Disk fills, gateway dies.

**Solution:** Add `purge_delivered(max_age_hours)` to `WALWriter`. Run periodically.

```python
# wal/writer.py — new method
def purge_delivered(self, max_age_hours: float) -> int:
    """Delete delivered records older than max_age_hours. Returns count deleted."""
    conn = self._ensure_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    cur = conn.execute(
        "DELETE FROM wal_records WHERE delivered = 1 AND delivered_at < ?",
        (cutoff,)
    )
    conn.commit()
    deleted = cur.rowcount
    if deleted > 0:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return deleted
```

Run in the delivery worker loop (every N cycles) or as a separate periodic task using `wal_max_age_hours` from config (already defined, default 72h).

**Impact:** WAL disk usage stays bounded. Delivered records purged after configurable retention.

### 3.5 Streaming Buffer Limits

**Problem:** `stream_with_tee()` buffers all response chunks with no size limit. A single large response can OOM the container.

**Solution:** Add `max_stream_buffer_bytes` config. Truncate buffer (not stream) when exceeded — the caller still gets the full response, but the hash is computed on a truncated buffer.

```python
# config.py
max_stream_buffer_bytes: int = Field(default=10_485_760, description="Max stream buffer for hashing (10MB)")

# forwarder.py — in generate()
buffer_size = 0
async for chunk in upstream.aiter_bytes():
    if buffer_size < settings.max_stream_buffer_bytes:
        buffer.append(chunk)
        buffer_size += len(chunk)
    yield chunk  # always stream to caller
```

> *Design note: We truncate the buffer, not the stream. The caller always gets the full response. The hash covers the first N bytes. The execution record includes a `response_truncated: bool` flag so the control plane knows the hash is partial.*

**Impact:** Container memory is bounded. No OOM from large responses.

### 3.6 Startup Self-Test

**Problem:** If the WAL directory is read-only, or the hash function is broken, or the control plane URL is malformed, the gateway starts but fails on first request. Users see cryptic errors.

**Solution:** Add a `self_test()` function called during `on_startup`, after sync but before accepting traffic.

```python
async def self_test():
    """Verify critical subsystems before accepting traffic."""
    # 1. WAL is writable
    test_record = ... # minimal ExecutionRecord
    ctx.wal_writer.write_and_fsync(test_record)
    ctx.wal_writer.mark_delivered(test_record.execution_id)

    # 2. Hash function works
    h = compute_sha3_512_string("self-test")
    assert len(h) == 128

    # 3. Control plane URL is syntactically valid
    from urllib.parse import urlparse
    parsed = urlparse(settings.control_plane_url)
    assert parsed.scheme in ("http", "https")

    logger.info("Startup self-test passed")
```

**Impact:** Fast failure with clear error messages instead of silent failures on first request.

### 3.7 Completeness Invariant

**Problem:** The WAL currently records only requests that clear the full pipeline. Requests that fail at auth, parsing, attestation, policy, provider error, or gateway exceptions produce no WAL record. This means the gateway cannot prove to an auditor that every inbound request is accounted for.

**The Invariant:**

```
GEN_ATTEMPT = GEN + GEN_DENY + GEN_ERROR
```

Where:
- `GEN_ATTEMPT` — total requests received by the gateway
- `GEN` — requests that reached the provider and were recorded (allowed)
- `GEN_DENY` — requests blocked by auth, attestation, or policy (denied)
- `GEN_ERROR` — requests that failed at any stage due to an error

Every request MUST appear in exactly one of these three categories. If this identity holds, the gateway can prove complete audit coverage. If it breaks — if `GEN_ATTEMPT > GEN + GEN_DENY + GEN_ERROR` — records are missing.

**Solution:** A separate `gateway_attempts` table in the WAL that captures every inbound request, regardless of where it fails. Written via middleware at the end of every request lifecycle, not in the pipeline.

```python
# wal/writer.py — new table alongside wal_records
CREATE TABLE IF NOT EXISTS gateway_attempts (
    request_id   TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    tenant_id    TEXT NOT NULL,
    provider     TEXT,               -- null if undetermined (parse failed)
    model_id     TEXT,               -- null if undetermined
    path         TEXT NOT NULL,
    disposition  TEXT NOT NULL,      -- see DispositionCode enum below
    execution_id TEXT,               -- FK to wal_records if disposition='allowed'
    status_code  INTEGER NOT NULL
)
```

**Disposition codes:**

| Code | Meaning |
|------|---------|
| `allowed` | Request forwarded, execution record written |
| `denied_auth` | API key auth failure |
| `denied_attestation` | Model not attested or attestation revoked |
| `denied_policy` | Pre-inference policy block |
| `denied_response_policy` | Post-inference response policy block |
| `denied_budget` | Token budget exhausted |
| `denied_wal_full` | WAL at capacity (enforced mode) |
| `error_parse` | Request body could not be parsed |
| `error_no_adapter` | No adapter registered for this path |
| `error_provider` | Provider returned 5xx |
| `error_gateway` | Internal gateway exception |

**Implementation:** A Starlette middleware wraps every request. After the response is sent, the middleware writes one row to `gateway_attempts`. The `request_id` (from Phase 9.3) is the primary key. The middleware reads the disposition from a context variable set by each pipeline stage.

```python
# middleware: runs after response, always
async def completeness_middleware(request: Request, call_next):
    rid = new_request_id()
    disposition_var.set("error_gateway")  # default if unset
    try:
        response = await call_next(request)
        return response
    finally:
        ctx.wal_writer.write_attempt(
            request_id=rid,
            tenant_id=...,
            path=request.url.path,
            disposition=disposition_var.get(),
            status_code=response.status_code,
            execution_id=execution_id_var.get(None),
        )
```

**Health endpoint addition:**

```json
{
    "completeness": {
        "attempts_last_hour": 3600,
        "allowed": 3480,
        "denied": 105,
        "errored": 15,
        "invariant_holds": true
    }
}
```

**Metrics addition:**

```python
gateway_attempts_total = Counter(
    "walacor_gateway_attempts_total",
    "All gateway request attempts by disposition",
    ["disposition"],
)
```

> *Compliance note: The Completeness Invariant is the gateway's answer to the auditor question "How do we know you didn't miss anything?" It proves that G1+G2+G3 enforcement is not merely applied to most requests — it is applied to all of them, and every exception is recorded.*

### 3.8 Sync Loop Error Handling

**Problem:** The `sync_loop` in `main.py` has no try/except. If an uncaught exception escapes `sync_attestations` or `sync_policies`, the loop dies silently and never restarts.

**Solution:** Wrap the loop body.

```python
async def sync_loop() -> None:
    while True:
        await asyncio.sleep(settings.sync_interval)
        try:
            if ctx.sync_client:
                await ctx.sync_client.sync_attestations(provider=settings.gateway_provider)
                await ctx.sync_client.sync_policies()
        except Exception as e:
            logger.warning("Periodic sync error (will retry next interval): %s", e)
```

**Impact:** Sync loop survives transient errors.

### 3.8 Configuration Variables (Phase 9)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_MAX_STREAM_BUFFER_BYTES` | 10485760 (10MB) | Max stream buffer size for hashing |
| `WALACOR_COMPLETENESS_ENABLED` | true | Enable gateway_attempts completeness tracking |
| `WALACOR_ATTEMPTS_RETENTION_HOURS` | 168 (7 days) | Retention for attempt records (longer than execution records — audit requirement) |
| (existing) `WALACOR_WAL_MAX_AGE_HOURS` | 72 | Delivered record retention before purge |

### 3.9 Test Checkpoints

- **Performance:** Benchmark forward latency before/after connection pooling (expect 50–100ms improvement)
- **WAL compaction:** Write 1000 records, deliver all, purge, verify count = 0 and disk usage decreased
- **Stream limits:** Stream a response larger than buffer limit, verify caller gets full response and hash is computed on truncated buffer
- **Startup self-test:** Make WAL directory read-only, verify startup fails with clear error
- **JSON logging:** Send a request, verify all log lines are valid JSON with matching `request_id`
- **Completeness invariant:** Send 100 requests with varied outcomes (allowed, denied, errored). Verify `attempts = allowed + denied + errored` exactly. Verify every request_id appears in `gateway_attempts`, including auth failures that never entered the pipeline.

---

## 4. Phase 10: Response Gate (Guarantee G4)

**Goal:** Post-inference policy evaluation. The gateway inspects model responses before returning them to the caller. Responses containing PII, harmful content, or policy violations are blocked or flagged. This is Guarantee G4.

### 4.1 Guarantee G4 Statement

> *Every model response is evaluated against tenant response policies before delivery to the caller. Responses that violate blocking policies are replaced with a policy-violation notice. The execution record captures the evaluation result and detected content categories. No response content is stored — only classification labels and hashes.*

### 4.2 Semantic Plugin Interface

The content analysis framework is designed as a formal plugin contract, not just an internal ABC. The interface must be stable and well-specified so that third-party semantic firewalls (Protect AI, Lakera Guard, custom NLP services) can integrate as analyzers without forking the gateway.

#### Plugin Contract

```python
# content/base.py
from enum import Enum

class Verdict(str, Enum):
    PASS  = "pass"    # content is clean, proceed
    WARN  = "warn"    # content has findings, flag but do not block
    BLOCK = "block"   # content violates policy, block the request/response

@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    confidence: float       # 0.0 – 1.0; required for all verdicts
    analyzer_id: str        # unique, stable identifier for this analyzer
    category: str           # "pii" | "toxicity" | "injection" | "secrets" | custom
    reason: str             # short, human-readable (no content); e.g. "email_pattern_matched"

class ContentAnalyzer(ABC):
    @property
    @abstractmethod
    def analyzer_id(self) -> str:
        """Stable unique identifier, e.g. 'walacor.pii.v1', 'protectai.scanv2'."""

    @property
    def timeout_ms(self) -> int:
        """Max time this analyzer may take. Default 50ms. Slow analyzers don't block the pipeline."""
        return 50

    @abstractmethod
    async def analyze(self, text: str) -> Decision:
        """
        Analyze text and return a Decision.
        Contract:
        - MUST NOT store or log the text
        - MUST return within timeout_ms or be cancelled
        - MUST return Decision with confidence 0.0 if analysis is inconclusive
        """
```

#### Timeout Enforcement

The orchestrator runs all analyzers under their declared timeouts using `asyncio.wait_for`. A slow semantic firewall that takes 2 seconds cannot stall every request:

```python
async def run_analyzer(analyzer: ContentAnalyzer, text: str) -> Decision | None:
    try:
        return await asyncio.wait_for(
            analyzer.analyze(text),
            timeout=analyzer.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Analyzer %s timed out after %dms", analyzer.analyzer_id, analyzer.timeout_ms)
        return None  # timeout = no decision; pipeline continues
```

#### Execution Record Impact

`content_flags` becomes `analyzer_decisions`:
```python
analyzer_decisions: list[dict] = []
# Each entry: {"analyzer_id": "walacor.pii.v1", "verdict": "block", "confidence": 0.97, "category": "pii"}
```

This allows the control plane to track which specific analyzer blocked or flagged each response — critical for tuning and audit.

#### Built-in Analyzers

**PII Detector** (`content/pii_detector.py`) — `analyzer_id: "walacor.pii.v1"`:
- Regex patterns for: email addresses, phone numbers, SSN (XXX-XX-XXXX), credit card numbers (Luhn validation), IP addresses
- Synchronous (fast), runs with 20ms timeout
- Returns `Decision(verdict=BLOCK, confidence=0.99, category="pii", reason="email_address")` when found

**Toxicity Detector** (`content/toxicity_detector.py`) — `analyzer_id: "walacor.toxicity.v1"`:
- Keyword and phrase matching against configurable deny-lists
- Deny-list loaded from `WALACOR_TOXICITY_DENY_LIST` or synced from control plane policies
- Returns `Decision(verdict=WARN, confidence=0.8, category="toxicity", reason="deny_list_match")` by default; configurable to BLOCK

> *Third-party integration example: A Protect AI scanner that calls their hosted API implements `ContentAnalyzer`, sets `timeout_ms=500`, and in `analyze()` calls their API with the text. If their API is slow or down, the gateway continues after 500ms — it does not stall. The gateway's built-in analyzers always run first (20ms, synchronous). Third-party analyzers run in parallel afterward.*

#### Built-in Analyzers

**PII Detector** (`content/pii_detector.py`):
- Regex patterns for: email addresses, phone numbers, SSN (XXX-XX-XXXX), credit card numbers (Luhn validation), IP addresses
- Returns `AnalysisResult(category="pii", severity="high", detail="email_address")`
- No NLP models — pure regex for deterministic, fast evaluation

**Toxicity/Harmful Content Detector** (`content/toxicity_detector.py`):
- Keyword and phrase matching for configurable deny-lists
- Categories: hate speech indicators, violence indicators, self-harm indicators
- Configurable per-tenant deny-list via control plane policies
- Returns `AnalysisResult(category="toxicity", severity=..., detail="deny_list_match")`

> *Design principle: Phase 10 analyzers are regex/keyword-based, not ML-based. ML-based content analysis (e.g., calling a classifier model) is a future extension. Regex is deterministic, fast (< 1ms), and has no external dependencies.*

### 4.3 Response Policy Evaluator

```python
# pipeline/response_evaluator.py
def evaluate_post_inference(
    policy_cache: PolicyCache,
    model_response: ModelResponse,
    attestation_context: dict,
    content_analyzers: list[ContentAnalyzer],
) -> tuple[bool, int, str, list[str], JSONResponse | None]:
    """
    Evaluate response against post-inference policies.
    Returns (blocked, response_policy_version, response_policy_result,
             content_flags, error_response_or_none).
    """
```

**Logic:**
1. Run all content analyzers on `model_response.content` (parallel if async, sequential if sync)
2. Collect `AnalysisResult` list
3. Map results to content_flags: `["pii", "toxicity"]` (category labels only, no content)
4. Evaluate against response policies in policy cache:
   - If any analyzer finding matches a blocking response policy → block
   - If any finding matches a flagging response policy → flag (don't block)
5. Return result with `response_policy_version` and `response_policy_result`

**When blocked (non-streaming):**
```json
{
  "error": "Response blocked by policy",
  "policy_violation": "response_content_policy",
  "content_flags": ["pii"]
}
```

**When blocked (streaming):**
- For streaming, the response has already been partially sent. The gateway cannot un-send chunks.
- Strategy: The execution record is flagged with `response_policy_result: "flagged_post_stream"`. The control plane can alert on these.
- Future: Add a `buffered_streaming` mode where the gateway holds the full response before streaming (trades latency for safety).

### 4.4 Orchestrator Changes

```python
# In handle_request(), after forward and before hash:

# NEW — Step 3.5: Post-inference policy evaluation
content_analyzers = ctx.content_analyzers or []
if not ctx.skip_governance and model_response:
    resp_blocked, resp_policy_version, resp_policy_result, content_flags, resp_err = (
        evaluate_post_inference(
            ctx.policy_cache, model_response, attestation_context, content_analyzers
        )
    )
    if resp_err is not None and not call.is_streaming:
        _inc_request(provider, model, "blocked_response_policy")
        return resp_err
else:
    resp_policy_version, resp_policy_result, content_flags = 0, "skipped", []

# Pass new fields to build_execution_record
```

### 4.5 Execution Record Impact

New fields on `ExecutionRecord`:
```python
response_policy_version: int = 0
response_policy_result: str = "pass"      # pass | blocked | flagged | flagged_post_stream | skipped
content_flags: list[str] = []             # ["pii", "toxicity", ...] — labels only
```

These fields are added to `walacor-core` `ExecutionRecord` model. Until the control plane schema is updated, they flow through as `metadata`.

### 4.6 Configuration Variables (Phase 10)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_RESPONSE_POLICY_ENABLED` | true | Enable post-inference policy evaluation |
| `WALACOR_PII_DETECTION_ENABLED` | true | Enable PII analyzer |
| `WALACOR_TOXICITY_DETECTION_ENABLED` | false | Enable toxicity analyzer (requires deny-list) |
| `WALACOR_TOXICITY_DENY_LIST` | "" | Comma-separated deny terms (or path to file) |

### 4.7 Test Checkpoints (Compliance Evidence)

- **G4-basic:** Response containing email address → detected, flagged in execution record
- **G4-blocking:** Response with PII + blocking policy configured → non-streaming response replaced with 403
- **G4-streaming:** Response with PII in streaming mode → execution record flagged as `flagged_post_stream`
- **G4-clean:** Response with no policy violations → `response_policy_result: "pass"`, empty `content_flags`
- **G4-hash-only:** Verify no response content appears in execution record or WAL — only category labels
- **Performance:** Response evaluation adds < 5ms for regex-based analyzers on typical response sizes (< 10KB)

---

## 5. Phase 11: Token Budget Governance

**Goal:** Track token usage per tenant/user, enforce configurable budgets, return 429 when exhausted. Provide cost visibility.

### 5.1 Why This Matters

Every enterprise deploying LLMs faces uncontrolled spend. Engineering teams spin up GPT-4 usage and the bill hits $50K before anyone notices. The gateway intercepts every request and every response — it already has the token data in `ModelResponse.usage` but discards it.

### 5.2 Budget Tracker

```python
# pipeline/budget_tracker.py
@dataclass
class BudgetState:
    tenant_id: str
    user: str | None
    period: str                    # "daily" | "monthly"
    period_start: datetime
    total_tokens_used: int
    max_tokens: int
    alert_threshold: float         # 0.8 = alert at 80%

class BudgetTracker:
    """In-memory token budget tracking per tenant/user. Syncs to control plane periodically."""

    def check_budget(self, tenant_id: str, user: str | None) -> tuple[bool, int]:
        """Returns (allowed, remaining_tokens). If remaining <= 0, returns (False, 0)."""

    def record_usage(self, tenant_id: str, user: str | None, tokens: int) -> None:
        """Add tokens to running total."""

    def get_usage_snapshot(self) -> dict:
        """Current usage for health endpoint / metrics."""
```

**Budget check placement in pipeline:**
- BEFORE forwarding (Step 2.5): Check if budget allows the request. If exhausted, return 429 immediately — don't waste provider tokens.
- AFTER response (Step 4.5): Record actual token usage from `ModelResponse.usage`.

**Token estimation for pre-check:**
- Use prompt length as rough estimator: `estimated_tokens = len(prompt_text) // 4`
- This prevents forwarding a request that will definitely exceed budget
- Actual usage recorded from response overrides the estimate

### 5.3 Budget Configuration

Budgets are configured via the control plane and synced as part of the policy sync:

```python
# In policy sync response, new field:
{
    "policies": [...],
    "budgets": [
        {
            "tenant_id": "walacor-bethesda",
            "user": null,           # null = tenant-wide
            "period": "monthly",
            "max_tokens": 10000000, # 10M tokens/month
            "alert_threshold": 0.8
        },
        {
            "tenant_id": "walacor-bethesda",
            "user": "analyst-team",
            "period": "daily",
            "max_tokens": 100000,   # 100K tokens/day per team
            "alert_threshold": 0.9
        }
    ]
}
```

**Fallback config (when control plane doesn't support budgets yet):**

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_TOKEN_BUDGET_ENABLED` | false | Enable token budget enforcement |
| `WALACOR_TOKEN_BUDGET_MONTHLY` | 0 | Monthly token limit (0 = unlimited) |
| `WALACOR_TOKEN_BUDGET_DAILY` | 0 | Daily token limit (0 = unlimited) |

### 5.4 Execution Record Impact

New fields:
```python
token_usage: dict | None = None    # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
budget_remaining: int | None = None # tokens remaining in applicable budget
```

### 5.5 Health Endpoint Impact

New section in `/health` response:
```json
{
    "token_budget": {
        "tenant_budget": {
            "period": "monthly",
            "used": 4500000,
            "limit": 10000000,
            "percent_used": 45.0
        }
    }
}
```

### 5.6 Metrics Impact

New Prometheus metrics:
```python
token_usage_total = Counter(
    "walacor_gateway_token_usage_total",
    "Total tokens consumed",
    ["tenant_id", "provider", "model", "token_type"],  # token_type: prompt | completion
)
budget_exceeded_total = Counter(
    "walacor_gateway_budget_exceeded_total",
    "Requests rejected due to budget exhaustion",
    ["tenant_id"],
)
```

### 5.7 Test Checkpoints

- **Budget enforcement:** Set 1000-token budget, send requests until exhausted, verify 429 on next request
- **Usage tracking:** Send request, verify `token_usage` in execution record matches `ModelResponse.usage`
- **Period reset:** Set daily budget, advance clock past midnight, verify budget resets
- **No budget:** When `WALACOR_TOKEN_BUDGET_ENABLED=false`, verify no budget checks occur

---

## 6. Phase 12: Audit-Only Mode (Real)

**Goal:** Make `enforcement_mode=audit_only` a genuine shadow-enforcement mode where the gateway logs what would have been blocked but forwards all requests.

### 6.1 Current Behavior vs Target

| Scenario | Current `audit_only` | Target `audit_only` |
|----------|---------------------|---------------------|
| Pre-policy blocks | Request blocked (403) | Request **forwarded**, record tagged `would_have_blocked: true` |
| Response policy blocks | Response blocked | Response **delivered**, record tagged `would_have_blocked: true` |
| Attestation missing | Request blocked (403) | Request **forwarded** (if provider configured), record tagged |
| Attestation expired + CP down | Request blocked (503) | Request **forwarded**, record tagged |
| WAL at capacity | Request blocked (503) | Request **forwarded**, WAL write best-effort |
| Budget exceeded | Request blocked (429) | Request **forwarded**, record tagged |

### 6.2 Implementation

In `orchestrator.py`, wrap each enforcement point:

```python
is_audit_only = settings.enforcement_mode == "audit_only"

# At attestation gate:
if err is not None:
    if is_audit_only:
        logger.warning("AUDIT_ONLY: Would have blocked (attestation): %s", ...)
        would_have_blocked = True
        # continue pipeline with degraded attestation context
    else:
        return err

# At policy gate:
if err is not None:
    if is_audit_only:
        logger.warning("AUDIT_ONLY: Would have blocked (policy): %s", ...)
        would_have_blocked = True
    else:
        return err
```

### 6.3 Execution Record Impact

New fields:
```python
enforcement_mode: str = "enforced"         # "enforced" | "audit_only"
would_have_blocked: bool = False           # True if audit_only mode would have blocked
would_have_blocked_reason: str | None = None  # "attestation" | "policy" | "response_policy" | "budget" | "wal_exhausted"
```

### 6.4 Control Plane Impact

The control plane gains a powerful new view: "What would enforcement look like if we turned it on?"

Dashboard can show:
- Percentage of requests that would be blocked (shadow block rate)
- Top policies that would trigger blocks
- Top users/models that would be affected

This gives enterprises confidence to flip to `enforced` mode.

### 6.5 Test Checkpoints

- **Shadow attestation:** In audit_only, request to unattested model → forwarded, execution record has `would_have_blocked: true`
- **Shadow policy:** In audit_only, request violating blocking policy → forwarded, execution record tagged
- **Enforced unchanged:** In enforced mode, all existing blocking behavior unchanged
- **WAL best-effort:** In audit_only + WAL at capacity, requests still forwarded

---

## 7. Phase 13: Session Chain Integrity (Guarantee G5)

**Goal:** Create a Merkle chain linking execution records within a session. If any record is removed, inserted, or modified, the chain breaks. This is Guarantee G5.

### 7.1 Guarantee G5 Statement

> *Every execution record within a session is cryptographically linked to its predecessor. The control plane can verify the complete, unaltered conversation history by walking the chain. Any tampering — deletion, insertion, reordering, or modification of records — is detectable.*

### 7.2 Chain Structure

```
Session: "session-abc-123"

Record 0:
  sequence_number: 0
  previous_record_hash: "0" * 128  (genesis)
  record_hash: SHA3-512(execution_id + prompt_hash + response_hash + ... + previous_record_hash)

Record 1:
  sequence_number: 1
  previous_record_hash: Record 0's record_hash
  record_hash: SHA3-512(execution_id + prompt_hash + response_hash + ... + previous_record_hash)

Record 2:
  sequence_number: 2
  previous_record_hash: Record 1's record_hash
  record_hash: SHA3-512(all fields including previous_record_hash)
```

### 7.3 Session State Tracking

```python
# pipeline/session_chain.py
@dataclass
class SessionState:
    session_id: str
    last_sequence_number: int
    last_record_hash: str
    last_activity: datetime

class SessionChainTracker:
    """In-memory session state for Merkle chain computation."""

    def __init__(self, max_sessions: int = 10000, ttl_seconds: int = 3600):
        self._sessions: dict[str, SessionState] = {}
        self._max_sessions = max_sessions
        self._ttl = ttl_seconds

    def next_chain_values(self, session_id: str) -> tuple[int, str]:
        """Returns (sequence_number, previous_record_hash) for next record in session."""
        state = self._sessions.get(session_id)
        if state is None:
            return 0, "0" * 128  # genesis
        return state.last_sequence_number + 1, state.last_record_hash

    def update(self, session_id: str, sequence_number: int, record_hash: str) -> None:
        """Update session state after WAL write."""
        self._sessions[session_id] = SessionState(
            session_id=session_id,
            last_sequence_number=sequence_number,
            last_record_hash=record_hash,
            last_activity=datetime.now(timezone.utc),
        )
        self._evict_stale()

    def _evict_stale(self) -> None:
        """Remove sessions inactive beyond TTL or when over max_sessions."""
        # LRU eviction by last_activity
```

### 7.4 Record Hash Computation

```python
# pipeline/hasher.py — new function
def compute_record_hash(
    execution_id: str,
    prompt_hash: str,
    response_hash: str,
    policy_version: int,
    policy_result: str,
    previous_record_hash: str,
    sequence_number: int,
    timestamp: str,
) -> str:
    """Compute SHA3-512 hash of record fields for chain integrity."""
    canonical = "|".join([
        execution_id, prompt_hash, response_hash,
        str(policy_version), policy_result,
        previous_record_hash, str(sequence_number), timestamp,
    ])
    return compute_sha3_512_string(canonical)
```

### 7.5 Execution Record Impact

New fields:
```python
sequence_number: int | None = None          # monotonic within session (0-indexed)
previous_record_hash: str | None = None     # SHA3-512 of previous record (or "0"*128 for genesis)
record_hash: str | None = None              # SHA3-512 of this record for chain verification
```

These are `None` when `session_id` is not provided (non-session requests are not chained).

### 7.6 Chain Verification (Control Plane Side)

The control plane verifies chains by:
1. Query all execution records for a session, ordered by `sequence_number`
2. Verify `record_hash[0]` = `SHA3-512(fields + "0"*128)`
3. Verify `record_hash[N]` = `SHA3-512(fields + record_hash[N-1])`
4. If any verification fails → chain is broken → flag for investigation

This verification logic belongs in the control plane, not the gateway. The gateway only builds the chain.

### 7.7 Configuration Variables (Phase 13)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALACOR_SESSION_CHAIN_ENABLED` | true | Enable Merkle chain for session records |
| `WALACOR_SESSION_CHAIN_MAX_SESSIONS` | 10000 | Max concurrent sessions tracked in memory |
| `WALACOR_SESSION_CHAIN_TTL` | 3600 | Session state TTL seconds (evict inactive sessions) |

### 7.8 Test Checkpoints (Compliance Evidence)

- **G5-chain:** Send 3 requests with same `session_id`. Verify `sequence_number` is 0, 1, 2. Verify `record_hash[1]`'s `previous_record_hash` equals `record_hash[0]`. Verify `record_hash[2]`'s `previous_record_hash` equals `record_hash[1]`.
- **G5-genesis:** First request in session has `sequence_number: 0` and `previous_record_hash: "0"*128`
- **G5-tamper-detection:** Modify one record's `prompt_hash` in the chain. Recompute chain. Verify mismatch detected.
- **G5-no-session:** Request without `session_id` → `sequence_number`, `previous_record_hash`, `record_hash` are all `None`
- **G5-eviction:** After TTL expires, session state is evicted. Next request for that session starts a new chain (sequence 0).

---

## 8. Timeline Summary

| # | Phase | Estimated Duration | Dependencies | Key Milestone |
|---|-------|-------------------|--------------|---------------|
| 9 | Infrastructure Hardening | 1 week | None | Connection pooling, WAL compaction, JSON logging operational |
| 10 | Response Gate (G4) | 2 weeks | Phase 9 | Response-side policy evaluation with content analysis |
| 11 | Token Budget Governance | 1 week | Phase 9 | Budget enforcement active, 429 on exhaustion |
| 12 | Audit-Only Mode | 1 week | Phase 10, 11 | Shadow enforcement with full reporting |
| 13 | Session Chain Integrity (G5) | 1 week | Phase 9 | Merkle chain linking session records |

> *Phases 11 and 13 can be developed in parallel once Phase 9 is complete. Phase 12 should follow Phase 10 and 11 since it needs to shadow both response policies and budget enforcement.*

---

## 9. Risk Register (Phases 9–13)

| # | Risk | Impact | Likelihood | Mitigation |
|---|------|--------|------------|------------|
| 1 | Connection pool exhaustion under load spike | High | Medium | Configure `max_connections` with headroom; health endpoint reports pool utilization; circuit-break on pool full |
| 2 | Content analyzer false positives block legitimate responses | High | Medium | Start with high-confidence regex patterns only; flagging (not blocking) as default; per-tenant tuning via policy |
| 3 | Token budget tracking inaccurate across multiple gateway instances | Medium | High | Each gateway tracks locally; control plane aggregates across instances on sync; accept minor over-budget risk at instance level |
| 4 | Session chain breaks when gateway restarts (in-memory state lost) | Medium | High | Session state is ephemeral; on restart, next request for existing session starts a new chain segment. Control plane must handle chain gaps (detected by missing sequence numbers). Document this as expected behavior. |
| 5 | Audit-only mode creates false confidence (enterprise never switches to enforced) | Low | Medium | Control plane dashboard shows "shadow block rate" prominently; periodic compliance reports flag audit-only tenants |
| 6 | WAL compaction during high-write periods causes SQLite contention | Low | Low | Run compaction during delivery worker idle periods; use `DELETE ... LIMIT` to bound transaction size |

---

## 10. Success Metrics (Phases 9–13)

| Metric | Target | Measurement |
|--------|--------|-------------|
| Forward latency improvement (Phase 9) | < 10ms p99 gateway overhead (excluding provider) | Before/after benchmark with connection pooling |
| Response analysis latency (Phase 10) | < 5ms added per request for regex analyzers | `pipeline_duration.labels(step="response_policy")` |
| PII detection accuracy (Phase 10) | > 95% recall on standard PII test corpus | Automated test suite with known PII patterns |
| Budget enforcement accuracy (Phase 11) | Within 1% of actual token usage | Compare gateway-tracked usage vs provider billing |
| Chain integrity verification (Phase 13) | 100% of chains verify correctly | Automated chain walk on control plane for all sessions |
| Audit-only shadow accuracy (Phase 12) | 100% match between shadow blocks and actual blocks | Run same traffic in enforced and audit_only, compare records |

---

## 11. Guarantee Summary (After Phase 13)

| Guarantee | Description | Enforcement Point | Phase |
|-----------|-------------|-------------------|-------|
| **G1** | Only attested, non-revoked models serve traffic | Attestation cache + model resolver | Phase 2 |
| **G2** | Every execution is cryptographically recorded (hash-only) | WAL + delivery worker | Phase 4 |
| **G3** | Pre-inference policy compliance | Policy cache + policy evaluator | Phase 3 |
| **G4** | Post-inference response governance | Response evaluator + content analyzers | **Phase 10** |
| **G5** | Session conversation integrity (tamper-evident chain) | Session chain tracker + record hash | **Phase 13** |

> *With G1–G5, the Walacor AI Security Gateway provides the most complete AI governance enforcement in the market: model verification, bidirectional policy enforcement, cryptographic recording, cost governance, and tamper-evident conversation chains — all without storing any prompt or response content.*

---

## Document Approval

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Technical Architect | | | |
| Engineering Lead | | | |
| Product Owner | | | |
| Security Officer | | | |
