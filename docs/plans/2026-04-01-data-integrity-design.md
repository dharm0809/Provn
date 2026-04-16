# Data Integrity: Normalization Engine, Unified Web Search, Walacor Envelope Display

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure every execution record has consistent, complete data regardless of provider — normalize adapter output, unify web search through the gateway's own tool, and surface Walacor-generated blockchain envelope data in the dashboard.

**Architecture:** Three layers: (1) a post-parse normalization function that enforces a strict ModelResponse contract before the orchestrator touches it, (2) a strategy override that routes all web search through the active tool loop using the built-in WebSearchTool, (3) a new lineage API endpoint that proxies Walacor's `getcomplex` query to fetch envelope/blockchain data for dashboard display.

**Tech Stack:** Python (existing gateway codebase), SQLite (WAL), Walacor REST API, React dashboard (vanilla JS SPA)

---

## Feature 1: Normalization Engine

### Problem

Each adapter produces a `ModelResponse` with different field shapes. The orchestrator and `build_execution_record()` assume a canonical format that not all adapters deliver:

| Gap | Impact |
|---|---|
| Anthropic `usage` has `input_tokens`/`output_tokens` but downstream reads `prompt_tokens`/`completion_tokens` | Every Anthropic record stores 0 tokens |
| HuggingFace/Generic `usage` never gets `detect_cache_hit()` | Missing `cache_hit`, `cached_tokens` fields |
| OpenAI `__RETRY_WITHOUT_SUMMARY__` sentinel in `content` | Literal string stored as response_content if retry path skipped |
| Empty `content` + populated `thinking_content` (qwen3 full-think-wrap) | Dashboard shows empty response |

### Design

Single function in a new file `src/gateway/pipeline/normalizer.py`:

```python
def normalize_model_response(response: ModelResponse, provider: str) -> ModelResponse:
```

**Rules applied in order:**

1. **Usage field normalization:**
   - If `usage` has `input_tokens` but not `prompt_tokens` → add `prompt_tokens = input_tokens`
   - If `usage` has `output_tokens` but not `completion_tokens` → add `completion_tokens = output_tokens`
   - If `usage` missing `total_tokens` → compute `prompt_tokens + completion_tokens`
   - Apply `detect_cache_hit()` if `cache_hit` key absent

2. **Content sentinel check:**
   - If `content == "__RETRY_WITHOUT_SUMMARY__"` → set `content = ""`

3. **Thinking fallback:**
   - If `content` is empty/whitespace AND `thinking_content` is non-empty → set `content = thinking_content`

4. **Content type enforcement:**
   - If `content is None` → set `content = ""`

Returns a new `ModelResponse` via `dataclasses.replace()` (frozen dataclass).

### Call sites

Two places in `orchestrator.py`:

1. **Non-streaming** (`_handle_request_inner`): after `forward()` returns `model_response`, before `_maybe_fetch_ollama_hash`
2. **Streaming** (`_after_stream_record`): after `adapter.parse_streamed_response(buffer)`, before `_record_token_usage`

### Files

- **Create:** `src/gateway/pipeline/normalizer.py`
- **Modify:** `src/gateway/pipeline/orchestrator.py` (2 call sites)
- **Create:** `tests/unit/test_normalizer.py`

---

## Feature 2: Unified Web Search

### Problem

OpenAI native web search (Responses API) is opaque — the gateway sees search queries and source URLs, but never the actual search results. `output_data` is always `null`. Content analysis cannot scan search results for injection. Two completely separate code paths exist for web search (passive for OpenAI, active for Ollama).

### Design

Route all web search through the gateway's active tool loop and built-in `WebSearchTool`, regardless of provider.

**Config change:**
- New field: `gateway_web_search_enabled: bool = False` (env: `WALACOR_GATEWAY_WEB_SEARCH_ENABLED`)
- When `True`, this takes priority over `openai_web_search_enabled`
- `openai_web_search_enabled` remains for backward compat but is effectively superseded

**Flow when `gateway_web_search_enabled=True`:**

1. `parse_request` (openai.py): do NOT set `_responses_api` or `_openai_web_search`. Set `metadata["_gateway_web_search"] = True` instead. Request stays on Chat Completions path (`/v1/chat/completions`).

2. `_select_tool_strategy` (orchestrator.py): return `"active"` when `call.metadata.get("_gateway_web_search")` is set, regardless of provider name.

3. `_run_pre_checks`: injects `web_search` function definition from `WebSearchTool.get_tools()` into the request body (standard OpenAI function-calling format).

4. OpenAI Chat Completions sees the function tool → decides when to call it → emits `finish_reason=tool_calls`.

5. `_run_active_tool_loop` dispatches to `WebSearchTool.call_tool()` → gets full results (content + sources) → feeds back via `build_tool_result_call` → OpenAI returns final answer.

6. `_write_tool_events` records everything: `input_data` (query), `output_data` (full results JSON), `sources` (URLs), `duration_ms`, content analysis.

**Reasoning models exception:** `_is_reasoning_model()` models (o1/o3/o4) still use Responses API for reasoning summaries but WITHOUT web_search tool injection. If both reasoning and web search are needed, the active tool loop handles web search through Chat Completions path (no `_responses_api` flag).

**Startup registration:** `_init_web_search_tool` in `main.py` is currently gated on `tool_aware_enabled AND web_search_enabled`. Must also fire when `gateway_web_search_enabled=True` — otherwise the WebSearchTool won't be in the registry for OpenAI requests.

**Ollama impact:** None. Ollama already uses active strategy + WebSearchTool. This design brings OpenAI to the same path. Ollama web search is unchanged.

### Files

- **Modify:** `src/gateway/config.py` (add `gateway_web_search_enabled`)
- **Modify:** `src/gateway/adapters/openai.py` (`parse_request` — flag logic)
- **Modify:** `src/gateway/pipeline/orchestrator.py` (`_select_tool_strategy` — override)
- **Modify:** `src/gateway/main.py` (`_init_web_search_tool` gate condition)
- **Modify:** `tests/unit/test_adapters.py` (new test cases)

---

## Feature 3: Walacor Envelope Data in Dashboard

### Problem

Walacor generates blockchain proof data (EId, blockchain hash, verification status) on ingest, but the dashboard only shows gateway-local data from the WAL database. Walter shared the `getcomplex` API that joins execution records with their blockchain envelopes.

### Design

**New lineage API endpoint:** `GET /v1/lineage/executions/{execution_id}/envelope`

This endpoint proxies a query to Walacor's `/api/query/getcomplex`:
```json
[
  {"$lookup": {"from": "envelopes", "localField": "EId", "foreignField": "EId", "as": "env"}},
  {"$match": {"execution_id": "<execution_id>"}},
  {"$sort": {"CreatedAt": -1}},
  {"$limit": 1}
]
```

Uses the existing `WalacorClient._http` client and JWT auth. Returns the envelope data (EId, blockchain hash, timestamps) alongside the execution record.

**Fallback:** When Walacor is not configured (`walacor_server` empty), returns `{"envelope": null, "reason": "walacor_not_configured"}`. Dashboard shows a "Local only" indicator.

**Dashboard change:** Execution detail view (`Execution.jsx`) adds a "Blockchain Proof" card showing:
- EId (Walacor entity ID)
- Blockchain Hash
- Envelope timestamp
- Verification status

Fetched on-demand when the user opens an execution detail (not preloaded in session lists).

### Files

- **Modify:** `src/gateway/walacor/client.py` (add `query_complex` method)
- **Modify:** `src/gateway/lineage/api.py` (add envelope endpoint)
- **Modify:** `src/gateway/lineage/dashboard/src/api.js` (add `getEnvelope` call)
- **Modify:** `src/gateway/lineage/dashboard/src/views/Execution.jsx` (add Blockchain Proof card)

---

## Feature 4: Walacor-Only Backend (Remove SQLite WAL)

### Decision

Remove local SQLite WAL entirely. Walacor is the sole storage and query backend.

### What gets removed

| Component | File | Status |
|---|---|---|
| WALWriter | `src/gateway/wal/writer.py` | Remove (keep file for WAL-mode backward compat behind flag) |
| WALBackend | `src/gateway/storage/wal_backend.py` | Remove |
| BatchWriter | `src/gateway/wal/batch_writer.py` | Remove |
| Delivery worker | `src/gateway/wal/delivery.py` | Remove (no local WAL to deliver from) |
| StorageRouter | `src/gateway/storage/router.py` | Simplify to direct WalacorBackend calls |
| LineageReader (SQLite) | `src/gateway/lineage/reader.py` | Rewrite to query Walacor API |
| WAL init in main.py | `_init_wal`, `_init_batch_writer` | Remove |
| SQLite schema/migrations | `_apply_schema` | Remove |

### What stays

| Component | Why |
|---|---|
| WalacorClient | Writes execution records, attempts, tool events |
| WalacorBackend | Single storage backend |
| Normalization engine | Normalizes ModelResponse before writing to Walacor |
| Session chain tracker | In-memory (or Redis) — chain fields added to records before Walacor write |
| Completeness middleware | Writes attempts to Walacor only |

### LineageReader rewrite

The lineage dashboard API endpoints stay the same. The `LineageReader` class gets rewritten from SQLite queries to Walacor API queries using `/api/query/getcomplex`:

```python
class WalacorLineageReader:
    """Read execution/attempt data from Walacor API instead of local SQLite."""

    def __init__(self, client: WalacorClient):
        self._client = client

    async def list_sessions(self, limit, offset, search, sort, order):
        pipeline = [
            {"$group": {"_id": "$session_id", "count": {"$sum": 1}, ...}},
            {"$sort": {"last_activity": -1}},
            {"$skip": offset},
            {"$limit": limit},
        ]
        return await self._client.query_complex(ETId=9000011, pipeline=pipeline)

    async def get_execution(self, execution_id):
        pipeline = [
            {"$match": {"execution_id": execution_id}},
            {"$lookup": {"from": "envelopes", "localField": "EId", "foreignField": "EId", "as": "env"}},
            {"$limit": 1},
        ]
        # Returns execution record WITH blockchain envelope data
        return await self._client.query_complex(ETId=9000011, pipeline=pipeline)
```

**Key benefit:** `get_execution` automatically includes envelope/blockchain data via `$lookup` — Feature 3 comes for free.

### WalacorClient additions

```python
async def query_complex(self, etid: int, pipeline: list[dict]) -> list[dict]:
    """Query Walacor via /api/query/getcomplex with aggregation pipeline."""
    resp = await self._http.post(
        f"{self._server}/api/query/getcomplex",
        json=pipeline,
        headers=self._headers(etid),
    )
    resp.raise_for_status()
    return resp.json()
```

### Dashboard impact

- Lineage API endpoints stay the same (same URLs, same JSON shapes)
- Dashboard JS code doesn't change — it calls the same endpoints
- Execution detail view gains envelope data automatically (from $lookup in every query)
- The "Blockchain Proof" card in Feature 3 uses data that's already in the response

### Migration path

1. Add `query_complex` to WalacorClient
2. Rewrite LineageReader as WalacorLineageReader (async methods)
3. Update lineage API endpoints to use WalacorLineageReader
4. Remove WAL writer, batch writer, delivery worker, WAL backend from startup
5. Add config flag: `WALACOR_STORAGE_MODE=walacor` (default) vs `local` (backward compat)

---

## Implementation Order

1. **Normalization engine** (Feature 1) — backend-agnostic, fixes data quality now
2. **Unified web search** (Feature 2) — backend-agnostic, fixes tool audit trail
3. **Walacor-only backend** (Feature 4) — remove SQLite, rewrite reads to Walacor API
4. **Envelope display** (Feature 3) — comes free with Feature 4's `$lookup` in queries

Features 1 and 2 work with the current dual-write architecture AND with Walacor-only. They should be implemented first so all future data going into Walacor is clean.
