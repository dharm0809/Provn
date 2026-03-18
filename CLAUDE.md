## Project
Walacor Gateway — ASGI audit/governance proxy for LLM providers. Source: `src/gateway/`.

## Key Architectural Facts
- Gateway does NOT compute SHA3-512 hashes of prompt/response text — it sends full text; Walacor backend hashes on ingest
- Session chain `record_hash` IS computed by the gateway (metadata fields only: execution_id, policy_version, policy_result, previous_record_hash, sequence_number, timestamp)
- Tool input/output hashes ARE computed by the gateway (orchestrator.py, for MCP/tool interactions)
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
- WIKI-EXECUTIVE.md: plain English only — no SHA3-512 formulas, no code snippets, no `record_hash = ...` blocks
- Session chain formula belongs in README/FLOW docs, not the wiki
- "We compute hashes" only applies to: session chain record_hash (G5) and tool input/output hashes

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
  - Tier 6: `tier6_advanced.py` (web search, tool audit SHA3-512, multi-turn chain, attachments, content analysis, MCP registry)
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
- Chain verification recomputes SHA3-512 server-side; client-side uses js-sha3 CDN
- Dashboard tool events display: rich cards with tool name/type/source badges, terminal-style input data, clickable source links, SHA3-512 hashes, content analysis verdicts, duration, iteration count
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
