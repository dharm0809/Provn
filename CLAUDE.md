## Project
Walacor Gateway — ASGI audit/governance proxy for LLM providers. Source: `src/gateway/`.

## Thinking effort
- Executing steps from an existing plan: don't think, just implement
- Debugging failures or unexpected behavior: think hard
- Designing new systems or choosing between approaches: ultrathink
- If you're unsure which applies, ask me before starting

## Key Architectural Facts
- Gateway does NOT compute SHA3-512 hashes — it sends full text and tool data; Walacor backend hashes on ingest (returns DH as tamper-evident checkpoint)
- Session chain uses UUIDv7 ID-pointer chain: each record carries `record_id` (UUIDv7) + `previous_record_id` (pointer to prior record); no gateway-side SHA3 Merkle chain
- Tool events store `input_data`/`output_data` (actual content, not hashes); Walacor hashes tool data on ingest
- Model routing reads `model` field from request body; routes by fnmatch before path-based routing
- One port (8000) serves all providers; audit records are differentiated by model/provider/attestation_id
- `WALACOR_SKIP_GOVERNANCE=false` (default) = full governance; `=true` = transparent proxy (audit-only, no chain/policy/budget)
- Full governance works without a control plane: models auto-attested on first use, policies pass-all; connect `WALACOR_CONTROL_PLANE_URL` later for remote attestation/policy rules
- `_store_execution` writes to BOTH Walacor backend AND local WAL (not either/or) so lineage dashboard always has data
- `_write_tool_events` also dual-writes to both backends (same pattern)
- Shared HTTP client is initialized in both modes

## Docs
- `docs/WIKI-EXECUTIVE.md` — CEO/leadership-facing; narrative style, no crypto formulas, explains decisions and tradeoffs
- `README.md` — full engineer reference; config, architecture, guarantees
- `docs/FLOW-AND-SOUNDNESS.md` — pipeline flowcharts + soundness analysis (all 9 findings resolved)
- `OVERVIEW.md` — one-page summary

## Doc Conventions
- WIKI-EXECUTIVE.md: plain English only — no SHA3-512 formulas, no code snippets
- Session chain section in README/FLOW docs: describe ID-pointer chain (record_id + previous_record_id), not Merkle hash chain
- Gateway computes no SHA3 hashes; Ed25519 signing still applies to canonical ID string

## Testing
- Async tests use `@pytest.mark.anyio` with `anyio_backend` fixture (NOT `pytest.mark.asyncio`)
- `get_settings()` uses `lru_cache(maxsize=1)` — call `get_settings.cache_clear()` in test teardown when monkeypatching env
- `aiter_bytes` mock in stream tests: use `MagicMock(return_value=aiter([...]))` not `AsyncMock` — `AsyncMock` returns a coroutine that `async for` can't iterate; only matters when the generator is actually consumed
- Tests that directly access `asyncio.Lock` on a tracker must use `async with tracker._lock:`, not `with`
- **Production test suite**: `tests/production/` — 7-tier gate structure run on EC2:
  - Tier 1: `tier1_local.sh` (unit tests) + `tier1_live.py` (health, completeness, session chain, lineage, WAL, metrics)
  - Tier 2: `tier2_security.py` (auth, control plane auth, lineage read-only, no stack traces, method enforcement)
  - Tier 3: `tier3_performance.py` (baseline, ramp, sustained load, SLA card)
  - Tier 4: `tier4_resilience.py` (Ollama down, gateway restart, streaming safety — Docker only)
  - Tier 5: `tier5_compliance.py` (chain audit 50 sessions, EU AI Act, health, metrics, SLA card)
  - Tier 6: `tier6_advanced.py` (web search, tool audit, multi-turn chain, attachments, content analysis, MCP registry)
  - Tier 6b: `tier6_mcp.py` (MCP fetch/time tools, multi-tool, error handling, chain after tools, WAL dual-write — native only)
  - Tier 7: `tier7_gauntlet.py` (89 checks: control plane CRUD, caller identity, PII, streaming, multi-model, metrics depth, lineage completeness, 5-turn chain, WAL burst, completeness invariant, health depth, models API)
  - `scripts/native-setup.sh` — runs gateway natively with MCP servers (Ollama stays in Docker)
  - `run_all_tiers.sh` — sequential gate runner for Tiers 1-6

## Policy Engine Rule Semantics
- `src/gateway/core/policy_engine.py` — rules support `action` field: `"deny"` (blacklist) or `"allow"` (whitelist, default)
- `action="deny"`: blocks when condition MATCHES (e.g. `model_id equals bad-model` blocks only `bad-model`)
- `action="allow"` (default): blocks when condition DOESN'T match (e.g. `model_id equals good-model` blocks everything except `good-model`)
- `_evaluate_rule` returns True/False for condition; action field inverts the meaning for deny rules
- All rules in a policy must pass for the policy to pass; ANY failure → blocked (if enforcement_level=blocking)

## Completeness Invariant
- Every request gets an **attempt record** via `completeness_middleware` finally block (always)
- **Execution records** are only written after a provider call — pre-forward exits (parse error, denied policy/attestation/budget, no adapter, method!=POST) intentionally produce NO execution record
- Post-forward exits (provider 5xx, tool strategy error, response policy block) DO write execution records via `_build_and_write_record` + `_record_token_usage`
- `skip_gov` without `walacor_client` = transparent proxy — no execution record by design
- Streaming: background task is in `generate()`'s `finally`, not `StreamingResponse(background=...)` — so it always runs even on stream interruption

## asyncio.Lock pattern (in-memory trackers)
- Startup-only sync methods (e.g. `BudgetTracker.configure`) drop the lock — no concurrent access before first request
- Single `len()` reads (e.g. `active_session_count`) are atomic in CPython/asyncio — no lock needed
- `BudgetTracker.get_snapshot` is `async def` — callers must `await ctx.budget_tracker.get_snapshot(...)`

## Built-in Tools (Phase 16)
- `src/gateway/tools/` — built-in tool package; `web_search.py` implements `WebSearchTool`
- Built-in clients duck-type MCPClient: must implement `get_tools() -> list[ToolDefinition]` and `async call_tool(name, args, timeout_ms) -> ToolResult`
- Register via `ToolRegistry.register_builtin_client(name, client)` — no subprocess, no `close()` needed
- `web_search_enabled=True` requires `tool_aware_enabled=True`; both must be set or web search is never registered
- `WALACOR_SKIP_GOVERNANCE=true` early-returns in `on_startup()` BEFORE tool registry init — tool loop does NOT fire in transparent proxy mode
- DDG Instant Answers API (`api.duckduckgo.com/?format=json`) only returns results for Wikipedia-indexed/well-known topics; returns empty for specific/recent queries
- Validation-failure early-return in `_execute_one_tool` correctly keeps `sources=None` — only post-execution path propagates `result.sources`
- `_write_tool_events` writes to BOTH Walacor backend (ETId 9000003) AND local WAL (dual-write, same pattern as `_store_execution`)
- `_build_tool_event_record` stores actual `input_data` (function arguments) alongside `input_hash` — dashboard shows what was searched/called
- Active tool loop (`_run_active_tool_loop`) returns 6-tuple including `final_http_resp`; main pipeline uses this instead of the initial forward response so the caller gets the model's final answer (not intermediate `finish_reason: tool_calls`)
- Content analyzers (PII, toxicity, Llama Guard) run on tool output for indirect prompt injection detection; results stored in tool event `content_analysis` field
- `_route_tool_strategy` wraps `_run_active_tool_loop` in try/except — exceptions log at ERROR and fall back to original model response (no 500)
- `web_search.py`: `json.dumps` of search results wrapped in try/except for TypeError/ValueError (non-serializable DDG data)
- **Model compatibility**: llama3.1:8b recommended for tool-aware workloads (deterministic `finish_reason=tool_calls`). qwen3 thinking models (qwen3:4b, qwen3:1.7b) consume tokens in `<think>` blocks and may never emit `tool_calls` — tool loop never triggers. `supports_tools=True` in capability cache only means the model ACCEPTS tool definitions (no 400/422), not that it CALLS them.
- **Active tool loop hides tool_calls from client**: gateway executes tools internally, returns final answer with `finish_reason=stop`. To verify tools were called, check lineage `/executions/{id}` for `tool_events`, NOT the response body.

## Phase 18: Lineage Dashboard
- `src/gateway/lineage/` — read-only SQLite reader + 5 JSON API endpoints + vanilla JS SPA
- `LineageReader` opens WAL db with `?mode=ro` + `PRAGMA query_only=ON` (never blocks WALWriter)
- Routes: `/v1/lineage/sessions`, `sessions/{id}`, `executions/{id}`, `attempts`, `verify/{id}`
- Static dashboard served at `/lineage/` via `StaticFiles(html=True)`
- Lineage and `/v1/lineage` paths skip `api_key_middleware` and `completeness_middleware`
- `WALACOR_LINEAGE_ENABLED=true` (default); lineage always inits WAL even in skip_governance mode
- Chain verification walks `previous_record_id` pointers server-side; dashboard calls `verifySession()` API (no client-side js-sha3)
- Dashboard tool events display: rich cards with tool name/type/source badges, terminal-style input data, clickable source links, content analysis verdicts, duration, iteration count
- Timeline chain cards show gold tool badges (`⚙ web_search`) for tool-augmented requests
- **Live throughput chart**: Overview page renders a canvas-based real-time telemetry graph polling `/metrics` every 3 seconds; shows req/s (gold line), allowed (green fill), blocked (red fill), animated pulse dot on latest point; 60-point buffer (3 min history); live counters below (req/s, tokens/s, % allowed, total); `ThroughputChart` class manages lifecycle (starts on overview, stops on navigation)
- **Execution record `model_id`/`provider` fields**: `build_execution_record()` in `hasher.py` accepts `model_id` and `provider` params; stored alongside `model_attestation_id` so lineage queries show actual model names; `list_sessions` SQL uses `COALESCE(model_id, model_attestation_id)` for backward compat with older records
- **Lineage API enrichment**: `_enrich_execution_record()` in `api.py` — `/executions/{id}` and `/sessions/{id}` responses enrich records: `model_id` extracted from `model_attestation_id` ("self-attested:X" → "X") when missing; `content_analysis` promoted from `metadata.analyzer_decisions` to top level; `/executions/{id}` returns flat record (no `"record":` wrapper)
- Tests: `tests/unit/test_lineage_reader.py` (11 tests)

## Model Capability Registry (Phase 19)
- `_model_capabilities` dict in `orchestrator.py` — in-memory cache: `{model_id: {"supports_tools": bool}}`
- `_model_supports_tools(model_id)` returns `True`/`False`/`None` (unknown)
- `_record_model_capability(model_id, supports_tools)` caches a discovered capability
- **Tool-unsupported retry**: if a model returns 400/422 with a tool-unsupported error message, the gateway caches `supports_tools=False`, strips tools from the request, and retries. Subsequent requests skip tool injection entirely — no wasted round-trip.
- **Tool-supported caching**: if a model accepts tools and returns <400, the gateway caches `supports_tools=True`
- Pre-check bypass: `_run_pre_checks` checks `_model_supports_tools()` before injecting tools; if `False`, sets `tool_strategy="none"` and skips injection
- `_TOOL_UNSUPPORTED_PHRASES` — 7 error patterns covering Ollama, OpenAI, Anthropic, and generic providers
- Status codes checked: 400 and 422 (some providers use 422 for validation errors)
- `/health` endpoint exposes `model_capabilities` dict when non-empty
- Thread-safe for asyncio: single writer, dict mutation is atomic in CPython
- Effect: models like gemma3:1b that don't support function calling work on first request (retry) and all subsequent requests (no retry, real SSE streaming preserved)
- Concurrent multi-model requests are safe: session chains stay contiguous, budget tracking is consistent, no race conditions

## Phase 20: Embedded Control Plane
- `src/gateway/control/` — SQLite-backed CRUD store + API endpoints + loader + sync-contract
- `ControlPlaneStore` in `store.py`: 3 tables (attestations, policies, budgets); WAL mode, synchronous=FULL, lazy init
- `api.py`: 12 route handlers (11 CRUD + 1 status); every mutation immediately refreshes in-memory caches
- `sync_api.py`: 2 endpoints (`/v1/attestation-proofs`, `/v1/policies`) serving SyncClient format for fleet sync
- `loader.py`: `load_into_caches()` loads DB→caches at startup; `_run_local_sync_loop()` refreshes every `sync_interval` seconds — **fixes `fail_closed`** (policy staleness never reached)
- Config: `WALACOR_CONTROL_PLANE_ENABLED=true` (default), `WALACOR_CONTROL_PLANE_DB_PATH=""` (defaults to `{wal_path}/control.db`)
- `PipelineContext` additions: `control_store`, `local_sync_task`
- `BudgetTracker.remove()` method — used by control plane when a budget is deleted
- Cache refresh helpers: `_refresh_attestation_cache()` snapshots auto-attested entries before clearing, repopulates from DB, then restores any auto-attested entries not in DB — prevents CRUD on unrelated models from de-attesting production models; `_refresh_policy_cache()` calls `set_policies()` with fresh version; `_refresh_budget_tracker()` iterates DB budgets + removes deleted keys
- Skips attestation/policy loading when `ctx.sync_client is not None` (remote sync takes precedence)
- Local sync loop only created when no remote SyncClient (embedded mode)
- `/v1/control` paths skip `completeness_middleware` and require `api_key_middleware`
- Dashboard: "Control" tab with 4 sub-views (Models, Policies, Budgets, Status) + auth gate (sessionStorage API key)
- `fetchControlJSON()` and `controlFetch()` helpers in app.js — adds `X-API-Key` header
- `discovery.py`: on-demand model discovery from Ollama (`/api/tags`) and OpenAI (`/v1/models`); 5s timeout, fail-open
- `GET /v1/control/discover`: scans providers, returns `{models: [{model_id, provider, source, registered}]}`
- `control_status()` enriched: `auth_mode`, `jwt_configured`, `content_analyzers`, `providers`, `session_chain`, `token_budget`, `model_routes_count`, `lineage_enabled`
- Dashboard Models tab: "Discover Models" button → discovery panel with Register/Register All; Status tab: Auth & Security, Providers, Runtime State cards
- Tests: `test_control_store.py` (20 tests), `test_control_api.py` (10 tests), `test_discovery.py` (12 tests); total suite: 204 pass, 2 skip

## Auto-Attestation (no control plane)
- When `control_plane_url` is empty, `_init_governance` seeds empty pass-all policies and skips SyncClient
- `_attestation_check` auto-attests models on first use: creates `CachedAttestation(status="active", verification_level="self_attested")` and caches it
- Auto-attestation is skipped when `ctx.control_store is not None` (embedded control plane manages attestations explicitly via CRUD API — prevents auto-attesting revoked models)
- Attestation ID format: `self-attested:{model_id}` (e.g. `self-attested:qwen3:4b`)
- No sync loop task created when `ctx.sync_client is None`
- Policy attestation context (`att_ctx`) includes `status` and `provider` fields — policies can check `status equals active` to block revoked models

## Phase 17: Thinking Strip + Llama Guard + OTel + Docker Demo
- **Thinking strip**: `strip_thinking_tokens()` in `src/gateway/adapters/thinking.py`; applied in `OllamaAdapter.parse_response` + `parse_streamed_response`; `WALACOR_THINKING_STRIP_ENABLED=true` (default). Clean content in `ModelResponse.content`, reasoning in `ModelResponse.thinking_content`. `hasher.py` writes `thinking_content` to execution record.
- **Ollama native reasoning**: Ollama OpenAI-compat endpoint natively separates `<think>` into a `reasoning` field in both non-streaming (message.reasoning) and streaming (delta.reasoning) responses. OllamaAdapter checks for native `reasoning` first, falls back to `strip_thinking_tokens` for older Ollama versions.
- **Llama Guard**: `LlamaGuardAnalyzer` in `src/gateway/content/llama_guard.py` implements `ContentAnalyzer`; covers S1–S14 categories; S4 (child_safety) → BLOCK, all others → WARN; fail-open (PASS, confidence=0.0) on Ollama unavailability or timeout; `timeout_ms=5000` (much higher than PII/toxicity 20ms); enabled via `WALACOR_LLAMA_GUARD_ENABLED=true` + `ollama pull llama-guard3`
- **OTel**: optional dep `[telemetry]` (`pip install 'walacor-gateway[telemetry]'`); `src/gateway/telemetry/otel.py`; single retroactive span per request emitted in `_build_and_write_record` after write; fail-open on `ImportError`; GenAI semantic conventions (gen_ai.system, gen_ai.request.model, gen_ai.usage.*) + walacor.* custom attributes; `ctx.tracer` in `PipelineContext`
- **Docker demo**: `deploy/docker-compose.yml` has `ollama` + `demo-init` services under `profiles: [demo, ollama]`; `demo/quickstart.py` pulls model, sends request, prints audit info; `docker compose --profile demo up` to run
- `.env.example` at repo root — all WALACOR_ env vars with comments, grouped by feature

## Phase 21: JWT/SSO Auth, Caller Identity, Compliance Doc, Dashboard Polish
- **JWT/SSO auth**: `src/gateway/auth/jwt_auth.py` — `validate_jwt()` supports HS256 (secret) and RS256/ES256 (JWKS); lazy `PyJWKClient` with 1h TTL cache; optional dep `[auth]` (`pip install 'walacor-gateway[auth]'`)
- **CallerIdentity**: `src/gateway/auth/identity.py` — frozen dataclass (`user_id`, `email`, `roles`, `team`, `source`); `resolve_identity_from_headers()` reads `X-User-Id`, `X-Team-Id`, `X-User-Roles`
- **Auth modes**: `WALACOR_AUTH_MODE=api_key` (default, unchanged) | `jwt` (JWT-only) | `both` (JWT first, API key fallback); configured in `main.py:api_key_middleware`
- **Caller identity in audit trail**: orchestrator merges `request.state.caller_identity` into `call.metadata` (`user`, `team`, `caller_roles`, `caller_email`, `identity_source`); `walacor_user_id` exposed for completeness middleware
- **WAL schema migration**: `writer.py:_ensure_conn()` adds `user TEXT` column to `gateway_attempts` (ALTER TABLE ADD COLUMN, try/except for existing)
- **Completeness middleware**: passes `user=user_id` to both `walacor_client.write_attempt()` and `wal_writer.write_attempt()`
- **Lineage reader**: `list_sessions()` extracts `$.user` from record JSON; `get_attempts()` includes `user` column
- **Dashboard polish**: identity badges (`.badge-identity`), governance status card (`.compliance-card`), chain verification glow animations (`.chain-verified-pass`/`.chain-verified-fail`)
- **Compliance doc**: `docs/EU-AI-ACT-COMPLIANCE.md` — EU AI Act (Articles 9/12/14/15/61), NIST AI RMF, SOC 2 Trust Criteria, config appendix
- Config: 10 new fields (`auth_mode`, `jwt_secret`, `jwt_jwks_url`, `jwt_issuer`, `jwt_audience`, `jwt_algorithms`, `jwt_user_claim`, `jwt_email_claim`, `jwt_roles_claim`, `jwt_team_claim`)
- Tests: `test_jwt_auth.py` (11 tests), `test_identity.py` (7 tests); total suite: 189 pass, 2 skip

## Phase 22: Token/Latency Charts + Governance Hardening (Stress Test Fixes)
- **Token Usage & Latency Charts**: `TokenLatencyChart` class in `app.js` — dual canvas (stacked area for tokens, line+area for latency); live mode polls `/metrics`, historical mode fetches `/v1/lineage/token-latency`; shared range bar (Current | 1h | 24h | 7d | 30d)
- **New fields in execution records**: `latency_ms`, `prompt_tokens`, `completion_tokens`, `total_tokens` added to `build_execution_record()` in `hasher.py`; threaded through all write paths in `orchestrator.py`
- **New API endpoint**: `GET /v1/lineage/token-latency?range=1h|24h|7d|30d` — time-bucketed aggregation via `json_extract()` in `reader.py`
- **CRITICAL FIX: Content analysis for thinking models**: `evaluate_post_inference()` now uses `model_response.content or model_response.thinking_content` — previously, when thinking strip moved ALL content to `thinking_content` (e.g. qwen3:4b), `content` was empty and content analysis was **always skipped**, meaning Llama Guard/PII/toxicity NEVER analyzed thinking model responses
- **PII detector severity tiers**: `_BLOCK_PII_TYPES = {"credit_card", "ssn", "aws_access_key", "api_key"}` — high-risk PII blocks, low-risk PII (ip_address, email_address, phone_number) issues WARN. Prevents false positive blocks when models include example IPs in educational responses
- **Governance stress test**: `tests/governance_stress.py` — 88 parallel requests across 48 questions, both qwen3:4b and gemma3:1b, tests all categories (general, reasoning, web search, creative, code, Llama Guard S1/S4/S9/S11)
- Tests: `test_lineage_reader.py` (17 tests); total suite: 207 pass, 2 skip

## Phase 23: Adaptive Gateway
- `src/gateway/adaptive/` — self-configuring intelligence layer with 5 extensible ABCs
- `interfaces.py`: StartupProbe, RequestClassifier, CapabilityProbe, IdentityValidator, ResourceMonitor
- `startup_probes.py`: ProviderHealthProbe (pings providers), RoutingEndpointProbe (validates routing URLs), DiskSpaceProbe (auto-scales WAL limits), APIVersionProbe (detects Ollama version)
- `request_classifier.py`: DefaultRequestClassifier — body `task` field (priority 1) > user-agent synthetic detection (priority 2) > prompt regex fallback (priority 3)
- `identity_validator.py`: DefaultIdentityValidator — cross-checks X-User-Id against JWT sub claim; JWT wins on mismatch
- `capability_registry.py`: CapabilityRegistry — model capabilities with TTL-based re-probing, per-model timeouts (reasoning=2x, embedding=0.5x)
- `resource_monitor.py`: DefaultResourceMonitor — disk space checks, provider error rate tracking, LiteLLM-style cooldown (>50% failure in 60s → 30s cooldown)
- Content policies: `content_policies` table in control plane, configurable BLOCK/WARN/PASS per category per analyzer; analyzers have `configure(policies)` method for hot-reload
- Enterprise extension: `WALACOR_CUSTOM_*` config fields accept comma-separated Python class paths; `load_custom_class()` utility for importlib loading
- All probes/monitors fail-open; never block traffic due to probe failure
- Content analysis caching: SHA256-keyed bounded cache (max 1000 entries) in response_evaluator
- Config: `startup_probes_enabled`, `provider_health_check_on_startup`, `capability_probe_ttl_seconds`, `identity_validation_enabled`, `disk_monitor_enabled`, `disk_min_free_percent`, `resource_monitor_interval_seconds`, `custom_startup_probes`, `custom_request_classifiers`, `custom_identity_validators`, `custom_resource_monitors`
- Tests: `test_adaptive_interfaces.py`, `test_startup_probes.py`, `test_request_classifier.py`, `test_identity_validator.py`, `test_content_policies.py`, `test_content_policy_configure.py`, `test_resource_monitor.py`, `test_capability_registry.py`, `test_analysis_cache.py`

## Phase 24: OpenAI ↔ Anthropic Protocol Bridge + Native Provider Tools
- `src/gateway/adapters/anthropic.py` — full bidirectional translator: OpenAI `/v1/chat/completions` ↔ Anthropic `/v1/messages`
- **Request translation**: system extraction, max_tokens default, stop→stop_sequences, multimodal images (data URL + http URL), role:tool→tool_result content blocks, role:assistant tool_calls→tool_use blocks, reasoning_effort→thinking.budget_tokens, tool definitions (function-calling→input_schema), tool_choice
- **Response translation**: text+thinking+tool_use blocks→OpenAI chat.completion shape, finish_reason mapping, cache token breakdown in usage, error body translation (Anthropic error→OpenAI error)
- **SSE streaming**: `_AnthropicToOpenAISSE` stateful translator handles text_delta, input_json_delta (tool streaming), thinking_delta (suppressed client-side, captured for audit), server_tool_use (hidden from client), web_search_tool_result (hidden from client), citations_delta
- **Native Anthropic web_search**: auto-injects `web_search_20250305` server tool when `web_search_enabled=true`; Anthropic runs search server-side in same streaming forward. Zero gateway overhead. `_select_strategy` returns "passive" for anthropic — no active tool loop, no non-streaming peek, real-time streaming.
- **Audit capture**: `_parse_content_block` handles `server_tool_use` (ToolInteraction with source="anthropic_native") + `web_search_tool_result` (extracts URLs/titles/page_age into sources). `_merge_server_tool_pairs` collapses paired blocks into single ToolInteraction. `_iter_sse_objects` concatenates chunks before parsing (fixes 46KB web_search_tool_result split across TCP chunks).
- **Provider tool strategy**: Anthropic=passive (native tools), OpenAI/Ollama=active (gateway DDG loop)
- **Speed**: synthesize_openai_sse_from_response in forwarder.py builds fake stream from non-streaming peek (for non-Anthropic active strategy). Eliminates double-forward. 855ms vs 2.4s.
- Config: `provider_anthropic_beta_headers` (comma-separated, sent as `anthropic-beta` header)
- `src/gateway/control/discovery.py` — `_discover_anthropic()` queries `/v1/models` with x-api-key + anthropic-version headers
- **Metadata enrichment**: `_build_and_write_record` and `_after_stream_record` dump thinking_content, provider_response_id, tool_events_detail (with full input/output/sources), canonical (SchemaMapper output), token_usage into metadata dict. Note: metadata goes to Walacor backend; WAL has flat columns only (follow-up: add metadata TEXT column to WAL).
- **Dashboard fix**: `te.sources` stored as JSON string in WAL (type coercion); Execution.jsx now JSON.parse + Array.isArray guard before .map(). ErrorBoundary added to main.jsx for crash visibility.

## Phase 26: Readiness self-check + sealed-in-Walacor drawer + Control redesign
- **Readiness system**: `src/gateway/readiness/` — 31 checks across 6 categories (security, integrity, persistence, dependency, feature, hygiene); `GET /v1/readiness` returns singleflight-cached report with 15s TTL; each check bounded by `asyncio.wait_for(5.0)`; timeouts and exceptions become amber, never crash the endpoint. `runner.py:_rollup` rules: `unready` iff any sec/int red, `degraded` iff any non-warn red/amber, `ready` otherwise (warn-only ambers OK). Rollback flag: `WALACOR_READINESS_ENABLED=false` → 503.
- **Drift audit**: sec/int checks flipping green→red write a `gateway_attempts` row with `disposition="readiness_degraded"` and JSON metadata `{check_id, detail, previous_status}` in the `reason` column. Rate-limited once per check-id per 5 min. Always use `write_attempt(request_id=, tenant_id=, path=, disposition=, status_code=, reason=)` — the `timestamp` kwarg doesn't exist; calling it silently swallows into the except block.
- **Bootstrap key persistence**: `src/gateway/auth/bootstrap_key.py` — when `control_plane_enabled` and no API keys configured, `ensure_bootstrap_key(wal_path)` writes a `wgk-*` token to `{wal_path}/gateway-bootstrap-key.txt` (mode 0600), reloaded on subsequent boots. Idempotent, fail-open. SEC-01 evidence carries `bootstrap_key_stable: bool` so it can distinguish "rotating on every restart" from "stable but recommend secret store".
- **Lineage auth hole closed**: new config `lineage_auth_required: bool = True`. `api_key_middleware` only exempts `/v1/lineage/*` + `/v1/compliance` when the flag is false. `/lineage/` (static dashboard HTML) stays always-open so the AuthGate loads. Dashboard `api.js:fetchJSON` attaches stored control API key to lineage calls.
- **Sealed-in-Walacor drawer** (on each Session record): `GET /v1/lineage/envelope/<execution_id>` calls `walacor_client.query_complex(executions_etid, [{$match:{execution_id}}, {$limit:1}])` and returns `{envelope (raw, UNSTRIPPED — keeps UID/ORGId/SV/EId), local (anchor fields from local WAL), match: {block_id, trans_id, dh, all_ok}}`. Intentionally skips `_deserialize_record` so envelope identity fields survive. Degradation: 503 when walacor_client missing, 502 when query fails, 200 + `envelope:null` when sealed-locally-but-not-delivered. Frontend helper `api.getSealEnvelope(executionId)` returns body for 502/503 too so the drawer can render "pending"/"unreachable" states.
- **Control redesign**: `views/Control.jsx` replaces the old rich CRUD with a 5-tab layout (status · attestations · policies · budgets · providers). Within Policies: nested sub-sections for content-analyzer thresholds + policy templates. Within Budgets: nested pricing sub-section. Providers: bulk `register all →` button + wired per-row `attest →` via `createAttestation({model_id, provider, status:'active'})`. Unlock modal seeds `sessionStorage.cp_api_key`; writes gated server-side via `X-API-Key`. 8 new api.js helpers: `getContentPolicies/upsertContentPolicy/deleteContentPolicy`, `getPricing/upsertPricing/deletePricing`, `listTemplates/applyTemplate`.
- **Control data-contract adapters**: backend returns `{attestations: [...]}`, `{policies: [...]}`, `{budgets: [...]}`, `{pricing: [...]}`, `{templates: [...]}`, `{models: [...]}` — NOT `{rows: [...]}`. Each `loadX` callback in `Control.jsx` reshapes to the panel-expected field names. Runtime counters NOT tracked backend-side today: `budgets.spent_usd`, `budgets.tokens_used`, `policies.hits_24h` render as 0.
- **Session detail is in Sessions.jsx, not Timeline.jsx**: `/lineage/?view=sessions` routes through `Sessions.jsx → SessionTimelineView → ChainRecord` (class `ses-chain-card`). `Timeline.jsx` is the lazy-loaded route-split view for direct `/sessions/:id` deep links. Dashboard UI changes to session records must edit both views (or at least Sessions.jsx) — classes starting `ses-*` mean Sessions.jsx.
- Tests: `tests/unit/readiness/` (97 tests), `tests/unit/test_bootstrap_key.py` (7 tests). Full strict audit caught 9 real bugs before landing — see commit `db7cd8d` message for details.

## Phase 27: Connections (silent-failure cockpit)
- **`/v1/connections`**: single GET endpoint returning a live 10-tile snapshot + recent-events stream. Singleflight + 3s TTL cache. Fail-open per tile on probe errors (tile goes `status:"unknown"`, endpoint never 5xx). Gated by `WALACOR_CONNECTIONS_ENABLED` (default true). Source: `src/gateway/connections/{api.py,builder.py}`.
- **10 tiles (fixed order)**: providers, walacor_delivery, analyzers, tool_loop, model_capabilities, control_plane, auth, readiness, streaming, intelligence_worker. Detail shapes + thresholds defined in `docs/plans/2026-04-23-connections-page-design.md`.
- **5 new bounded deques (no new storage)**: `WalacorClient._delivery_log` + `.delivery_snapshot()`; `ContentAnalyzer._fail_open_log` + `.fail_open_snapshot()` (lazy-init; wired at per-request fail-open sites including the `unavailable` early-return path in presidio_pii / prompt_guard — critical for the tile to surface uninstalled analyzers); module-level `_tool_exception_log` + `tool_exceptions_snapshot()` in `pipeline/tool_executor.py`; module-level `_stream_interruption_log` + `stream_interruptions_snapshot()` in `pipeline/forwarder.py` (gated on `_exc is not None`; also wires the S4 content-safety mid-stream abort); `IntelligenceWorker._last_error` + `.snapshot()`.
- **All `last_*` fields are window-scoped to 60s** (consistent convention): a stale failure from an hour ago does NOT surface once the window clears. Implemented in every snapshot method.
- **Shared ISO helper**: `gateway.util.time.iso8601_utc`. Don't reach into `walacor.client._iso8601` — it's an alias pointing at the shared helper now.
- **`DefaultResourceMonitor.record_provider_result(provider, success, *, error=None)`**: the production hook in `main.py:_on_provider_response` now passes `error=f"HTTP {code}"` on 5xx, so `snapshot().last_error` is populated. TransportError (timeouts, connection refused) still doesn't reach either hook — flagged TODO in `main.py`.
- **`get_attempts(disposition=...)`**: new kwarg on both `LineageReader.get_attempts` and `walacor_reader.get_attempts`. Used by the readiness tile for the `degraded_rows_24h` count.
- **Dashboard view**: `views/Connections.jsx` — v3 triage-queue spine ported verbatim from TruzenAI bundle, with three v4 grafts: banner stats strip (6 numbers — down/degraded/healthy + sessions/executions/requests hit) shown when `overall_status != green`, red incident banner when `counts.red >= 1`, `V4Runbook` block inside the existing tile slide-over (3 seed runbooks: walacor_delivery, auth, providers; others show "no runbook yet"). Poll every 3s via `getConnections()`. TruzenAI source bundle lives at `docs/plans/assets/2026-04-23-connections-truzenai/`.
- **Tests**: `tests/unit/connections/` (36 tests — shape, rollup, fail-open, 10 × (empty + threshold) builders, events merger).

## Dashboard React Rules (important)
- **Rules of Hooks**: every `useMemo`/`useState`/`useEffect`/`useCallback` must run BEFORE any `if (…) return` in a component. A hook placed after an early return executes only on some renders, causing React Error #310 ("rendered more hooks than during the previous render") and a fully blank page. Check `Overview.jsx` as the reference pattern — palette `useMemo` sits above the `if (loading) return <Skeleton />` / `if (error) return …` block.
- **Debugging blank dashboard**: minified errors don't name the source component. Use Playwright MCP (`browser_navigate` → `browser_console_messages level=error`) against the dashboard URL to pull the minified stack with line/column in `index-*.js`, then grep the source tree for the suspect hook call.
- **Dashboard build output goes to `src/gateway/lineage/static/`** (`vite.config.js` has `outDir: '../static'`, `emptyOutDir: true`, `base: '/lineage/'`). FastAPI `StaticFiles` serves these on the fly — no gateway restart needed after rebuild + sync.
- **Cache-busting after rebuild**: Vite generates hashed filenames (`index-<hash>.js`). If a user's browser holds a stale `index.html`, it requests now-404 hashed files and the page goes blank. Hard reload or incognito resolves.

## Operations (EC2 gateway_dharm @ 35.165.21.8)
- **Gateway runs natively on port 8100** (not Docker). Start/restart via `~/start_gateway_dharm.sh`; logs at `/tmp/gateway_dharm.log`; WAL at `/tmp/walacor-wal-dharm/`.
- **OpenWebUI runs in Docker on port 3100** (`gateway-dharm-openwebui`). Volume: `gateway_dharm_webui-data` (mounted at `/app/backend/data` → `/var/lib/docker/volumes/gateway_dharm_webui-data/_data/` on host).
- **OpenWebUI secret key must be persisted to the volume**, not via env var. Without `.webui_secret_key` on disk, OpenWebUI auto-generates a new one on every container restart → all session tokens invalidated → users logged out, admin-added users appear to reset. Fix: write a 32-byte `secrets.token_urlsafe` to `/var/lib/docker/volumes/gateway_dharm_webui-data/_data/.webui_secret_key` (chmod 600, root-owned), then restart the container. Logs should then show `Loading WEBUI_SECRET_KEY from .webui_secret_key`.
- **Do not trust bare `webui-data` volume name** — there are several orphan `*webui*` volumes on the host from old compose stacks. The running container uses the compose-project-prefixed one (`gateway_dharm_webui-data`); confirm with `docker inspect gateway-dharm-openwebui | grep -A 10 Mounts`.
- **Admin users and API keys persist in `webui.db`** (`sudo sqlite3 <volume>/webui.db 'SELECT name, email, role FROM user;'`) — safe to back up with `tar -czf` on the volume directory.
- **When cleaning orphan processes**: `pkill` as `ec2-user` won't touch root-owned processes (old OpenWebUI host uvicorns from the legacy `~/Gateway` stack). Use `sudo pkill` or `sudo kill $(sudo lsof -t -i :<port>)` for those. `docker compose -f ~/Gateway/docker-compose.yml down` stops the legacy stack cleanly.

## Apr-24 session (strict allowlist + compliance wiring + readiness rework)
- **Strict model allowlist**: `WALACOR_STRICT_MODEL_ALLOWLIST=true` is the production knob. Effects: (a) startup auto-registration of discovered provider models in `main.py:_auto_register_models` is skipped; (b) `_attestation_check` stops auto-attesting on first use when `ctx.sync_client is None AND settings.strict_model_allowlist`; (c) `/v1/models` refuses the fallback-to-discovery and returns `[]` when the control store has no active attestations. Admins curate via Control → Discover Models → register per row or `register all →`. Denial copy when a request hits an unattested model + embedded control plane present: orchestrator overrides `err` with a 403 `{error.type: "model_not_attested", code: "model_not_attested", message: "model 'X' is not in the gateway allowlist (provider=Y). An admin must attest it via Control → Discover Models."}`.
- **ETIds bumped 21/22/23 → 31/32/33**: gateway_executions schema was missing `record_id` and `previous_record_id` (the UUIDv7 ID-pointer chain). Walacor sandbox refused in-place SV evolution (`POST /schemas` with SV=2 → `400 Invalid ETId or SV for schema`), so fresh ETIds (9000031/32/33) were created via `scripts/setup_walacor_schemas.py`. `scripts/upgrade_walacor_executions_schema.py` kept for future attempts but documents the sandbox rejection path. `EXECUTIONS_FIELDS` now includes both chain fields; client code already wrote them through `_EXECUTION_SCHEMA_FIELDS`, nothing there needs changing.
- **Walacor sandbox never anchors**: verified against old (9000021) and new (9000031) ETIds — `BlockId`, `TransId`, `DH` stay null on every record. The blockchain-anchoring worker doesn't run on this sandbox (tier/entitlement concern, out of gateway scope). Implication: INT-04 would be permanently red with `severity=int` forcing the rollup to `unready`. Demoted to `severity=warn` — it still surfaces as `degraded`, doesn't take the gateway out of the LB.
- **Readiness fixes (`integrity.py`, `hygiene.py`)**:
  - INT-02 + INT-07 exclude internal records (`execution_id LIKE 'self-test-%'` and `request_type IN ('system_task', 'intelligence_verdict')`). Self-test and intelligence-verdict writes skip the HTTP pipeline/signing by design, so counting them as "unsigned" or "missing attempt row" was a false positive.
  - INT-04 rewritten: was looking at local wal_records for `walacor_block_id/trans_id/dh` — those fields only exist on records READ BACK from Walacor (populated in `lineage/_normalize.py:30-32`), never on the pre-submit local copy. Now queries `walacor_client.query_complex` on the executions ETId for `BlockId/TransId/DH` on records ≥2 min old. 2-minute grace before un-anchored counts as red.
  - HYG-03 wrapped `Path.exists()` + `Path.stat()` in PermissionError guards — root-owned Docker volumes made it raise "internal error: Errno 13", now returns green with "not readable by gateway — skipping".
- **Async-vs-sync reader helper**: `connections/builder.py:_call_reader(fn, *args, **kwargs)` dispatches to `await fn(...)` when `inspect.iscoroutinefunction(fn)`, else `asyncio.to_thread(fn, ...)`. Use this any time you call a `LineageReader`/`WalacorLineageReader` method whose implementation differs per subclass. The previous `asyncio.to_thread(reader.get_attempts, ...)` on WalacorLineageReader returned an un-awaited coroutine, silently dropped the result, and spammed `RuntimeWarning`.
- **Auth tile + bootstrap key**: bootstrap-key stability is only a meaningful signal when `control_plane_enabled AND not api_keys_list` (same condition `main.py:1496` uses to decide whether to write the `wgk-*.txt` file). Any deployment with admin keys set → `bootstrap_key_stable` is `None` (not applicable), tile is green. Detail now includes `admin_api_keys_configured: bool` so operators can tell which auth path is active without reading logs.
- **Compliance UI wired live**: `views/Compliance.jsx` was a placeholder. Now calls `getComplianceReport(framework, start, end)` in parallel for all 4 frameworks, renders `audit_readiness.score/grade` per card with Preview drawer (dimensions, evidence, control-mapping articles, chain integrity). Date picker: Today / 7d / 30d / 90d / 1y / Custom (two `<input type="date">`), drives both the fetch AND the download URLs. Downloads go via `fetch → blob → <a download>` so the `X-API-Key` header is attached and `Content-Disposition` filename survives.
- **Compliance scoring accuracy**: dimension 3 (Content Safety) scored purely from `len(ctx.content_analyzers)` (configured breadth), never checked whether analyzers actually RAN. Now both `LineageReader.get_compliance_summary` and `WalacorLineageReader.get_compliance_summary` compute `content_analysis_coverage_pct` over the window by probing each execution for top-level `content_analysis` or `metadata.analyzer_decisions`. `audit_intelligence.py` dimension-3 formula: `70% × coverage_pct + 30% × min(30, analyzers_count * 8)`. No traffic in window → falls back to configured breadth capped at 60 so it can't mask an unverifiable posture. Evidence strings now name `covered/total_exec` explicitly. **Note**: score is still framework-agnostic — `assess_audit_readiness()` doesn't consume the framework id, only `framework_mapping.articles` differs per framework. Compliance subtitle copy documents this.
- **Dashboard localStorage for control API key**: `api.js:getControlKey()` now reads `localStorage.getItem('cp_api_key') || sessionStorage.getItem('cp_api_key')`; `setControlKey` writes to localStorage; `clearControlKey` clears both. Fix for multi-tab dashboards where unlocking in one tab used to leave siblings at 401. Unlock modal (`Control.jsx:UnlockModal`) no longer dismisses on backdrop click — only the Cancel button.
- **Providers VIEW drawer**: clicking `view` on a provider row in Control opens an in-row expansion (`<Fragment>` pattern) listing every attested model on that provider (id, context, first-seen). Button flips to `hide`. CSS in `styles/control.css`: `cp-row-expansion`, `cp-expansion-inner`, `cp-expansion-head`, `cp-table-inner`.
- **Overview window consistency**: `views/Overview.jsx` header "Total Requests" + "% Allowed" now read from `counters.total/pct` (windowed throughput buckets) with `—` placeholder before first fetch, instead of mixing unbounded `getAttempts` count with the chart. Previously showed 7 vs 6 / 57.1% vs 50% inconsistency.
- **PDF compliance export deploy dependencies**: WeasyPrint needs native Pango + Cairo. Wired into every deploy surface: `scripts/native-setup.sh` step 3a does `sudo dnf install -y pango cairo` (or apt equivalent); `deploy/Dockerfile` adds `libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libharfbuzz0b libffi8 shared-mime-info`; `deploy/Dockerfile.fips` does `dnf install -y pango cairo` on ubi9; `~/start_gateway_dharm.sh` on EC2 self-heals with `ldconfig -p | grep libpango || sudo dnf install`. Without these, `/v1/compliance/export?format=pdf` returns 501 from `api.py:_build_pdf_response` with `cannot load library 'libpango-1.0-0'`.
- **No internal jargon in UI**: scrub "Phase N", "31-check", release codenames from JSX labels/blurbs/subtitles AND from `.js`/`.css` comments (Vite can inline them into shipped bundles). Grep `Phase [0-9]`, `Phase-`, `-check` before landing any dashboard change. Architectural labels ("Gateway self-check rollup", "readiness rollup") are OK.
- **Readiness severity convention**: `Severity.sec` / `Severity.int` RED → rollup `unready` (gateway keep-out). `Severity.warn` RED → rollup `degraded` only (gateway stays in LB). Use `warn` when the red condition reflects a backend or external concern the gateway cannot fix (Walacor anchoring lag, upstream provider config); reserve `int`/`sec` for local invariants the gateway itself breaks (unsigned records it wrote, attempts missing for executions it served).
