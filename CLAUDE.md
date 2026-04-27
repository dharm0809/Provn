## Project
Walacor Gateway — ASGI audit/governance proxy for LLM providers. Source: `src/gateway/`.

## Thinking effort
- Executing steps from an existing plan: don't think, just implement
- Debugging failures or unexpected behavior: think hard
- Designing new systems or choosing between approaches: ultrathink
- If you're unsure which applies, ask me before starting

## Key Architectural Facts
- Gateway does NOT compute SHA3-512 hashes — sends full text/tool data; Walacor backend hashes on ingest (returns DH as tamper-evident checkpoint). Ed25519 signing still applies to the canonical ID string.
- Session chain is a UUIDv7 ID-pointer chain: `record_id` + `previous_record_id`. No Merkle hash chain.
- Tool events store actual `input_data`/`output_data` (not hashes).
- Model routing reads body `model` field; fnmatch routing precedes path-based routing.
- One port (8000) serves all providers; records differ by model/provider/attestation_id.
- `WALACOR_SKIP_GOVERNANCE=false` (default) = full governance; `=true` = transparent proxy (audit-only, no chain/policy/budget). Tool registry init is also skipped in skip mode.
- Full governance works without a control plane: models auto-attested on first use, policies pass-all. Set `WALACOR_CONTROL_PLANE_URL` for remote attestation/policy.
- Dual-write: both `_store_execution` and `_write_tool_events` write to BOTH the Walacor backend AND the local WAL — never either/or.

## Docs
- `docs/WIKI-EXECUTIVE.md` — leadership-facing; plain English, no crypto formulas, no code
- `README.md` — engineer reference; config, architecture, guarantees
- `docs/FLOW-AND-SOUNDNESS.md` — pipeline flowcharts + soundness analysis
- `OVERVIEW.md` — one-page summary
- WIKI-EXECUTIVE.md and chain-section copy must describe the ID-pointer chain, never SHA3 Merkle.

## Testing
- Async tests use `@pytest.mark.anyio` with `anyio_backend` fixture (NOT `pytest.mark.asyncio`).
- `get_settings()` is `lru_cache(maxsize=1)` — call `get_settings.cache_clear()` in test teardown when monkeypatching env.
- `aiter_bytes` mock in stream tests: use `MagicMock(return_value=aiter([...]))` not `AsyncMock` (`async for` can't iterate the coroutine that `AsyncMock` returns).
- Direct access to `asyncio.Lock` on a tracker is `async with tracker._lock:`, not `with`.
- Production tier suite lives in `tests/production/` — 7-tier gate runner via `run_all_tiers.sh`. Native runs use `scripts/native-setup.sh` (Ollama stays in Docker).

## Policy Engine Rule Semantics
- `src/gateway/core/policy_engine.py` — rules support `action`: `"deny"` (blacklist) or `"allow"` (whitelist, default).
- `action="deny"`: blocks when condition MATCHES.
- `action="allow"`: blocks when condition does NOT match.
- All rules in a policy must pass; ANY failure → blocked when `enforcement_level=blocking`.

## Completeness Invariant
- Every request gets an **attempt record** via `completeness_middleware` finally block.
- **Execution records** are only written after a provider call. Pre-forward exits (parse error, denied policy/attestation/budget, no adapter, non-POST) intentionally produce NO execution record. Post-forward exits (provider 5xx, tool error, response policy block) DO write one.
- `skip_gov` without `walacor_client` = transparent proxy → no execution record by design.
- Streaming: background task sits in `generate()`'s `finally`, not `StreamingResponse(background=...)`, so it always runs even on stream interruption.

## asyncio.Lock pattern (in-memory trackers)
- Startup-only sync methods (e.g. `BudgetTracker.configure`) drop the lock — no concurrency before first request.
- Single `len()` reads are atomic in CPython/asyncio — no lock needed.
- `BudgetTracker.get_snapshot` is `async def` — callers must `await`.

## Subsystems (current invariants)

### Built-in tools (`src/gateway/tools/`)
Built-in clients duck-type MCPClient (`get_tools`, `async call_tool`). Register via `ToolRegistry.register_builtin_client`. `web_search_enabled=True` requires `tool_aware_enabled=True`. Active tool loop hides `tool_calls` from the client and returns the model's final answer — to verify tools ran, check lineage `/executions/{id}.tool_events`, NOT the response body. Model gotcha: qwen3 thinking models burn tokens in `<think>` and may never emit `tool_calls`; llama3.1:8b is the deterministic baseline. `supports_tools=True` only means the model ACCEPTS tool defs, not that it CALLS them.

### Lineage dashboard (`src/gateway/lineage/`)
Read-only SQLite reader + `/v1/lineage/*` endpoints + Vite SPA at `/lineage/` (built into `static/`, base `/lineage/`). Reader opens WAL with `?mode=ro` + `PRAGMA query_only=ON`. Lineage paths bypass `completeness_middleware`; auth is gated by `lineage_auth_required` (default true; static HTML always open so AuthGate loads). Chain verification walks `previous_record_id` server-side. **Dashboard build output goes to `src/gateway/lineage/static/`** — FastAPI `StaticFiles` serves on the fly, no restart needed; hashed filenames mean stale `index.html` → blank page (hard reload fixes).

### Model capability registry (`orchestrator.py`)
`_model_capabilities` dict caches `{model_id: {supports_tools: bool}}`. On 400/422 with a tool-unsupported phrase, gateway caches `False`, strips tools, and retries; subsequent requests skip injection. `/health` exposes the cache when non-empty.

### Embedded control plane (`src/gateway/control/`)
SQLite-backed CRUD for attestations, policies, budgets, content_policies, pricing. Mutations refresh in-memory caches; `_refresh_attestation_cache` preserves auto-attested entries on partial CRUD. `/v1/control/*` requires `X-API-Key`. `discovery.py` queries Ollama `/api/tags`, OpenAI `/v1/models`, Anthropic `/v1/models` (5s timeout, fail-open). Local sync loop refreshes when no remote SyncClient.

### Auto-attestation (no control plane)
When `control_plane_url` empty, `_attestation_check` self-attests on first use as `self-attested:{model_id}`, status `active`. Skipped when `ctx.control_store is not None` (embedded plane manages explicitly) OR when `WALACOR_STRICT_MODEL_ALLOWLIST=true`. With strict allowlist + embedded plane, an unattested model returns 403 `model_not_attested` with copy directing the admin to Control → Discover Models.

### JWT/SSO + caller identity (`src/gateway/auth/`)
`WALACOR_AUTH_MODE=api_key|jwt|both`. JWT validator supports HS256 + RS256/ES256 (JWKS, 1h cache). `CallerIdentity` resolved from headers OR JWT claims; orchestrator merges into `call.metadata` (`user`, `team`, `caller_roles`, `caller_email`, `identity_source`). `gateway_attempts.user` column added via `ALTER TABLE` (try/except for existing). Bootstrap key: when `control_plane_enabled` AND no API keys configured, `ensure_bootstrap_key(wal_path)` writes `wgk-*` to `{wal_path}/gateway-bootstrap-key.txt` (mode 0600) and reloads it on subsequent boots.

### Anthropic bridge (`src/gateway/adapters/anthropic.py`)
Bidirectional translator for OpenAI `/v1/chat/completions` ↔ Anthropic `/v1/messages` (incl. SSE, multimodal, tool_use, thinking, server_tool_use, web_search_tool_result). Provider tool strategy: Anthropic=passive (native `web_search_20250305`, no gateway loop), OpenAI/Ollama=active (gateway DDG loop). Speed: `synthesize_openai_sse_from_response` builds a fake stream from a non-streaming peek for the active path (avoids double-forward).

### Adaptive layer (`src/gateway/adaptive/`)
5 extension ABCs: StartupProbe, RequestClassifier, CapabilityProbe, IdentityValidator, ResourceMonitor. Defaults probe providers, classify by body `task` → user-agent → prompt regex, monitor disk + provider error rate (LiteLLM-style cooldown). All probes/monitors fail-open. Enterprise extension via `WALACOR_CUSTOM_*` class paths.

### Readiness (`src/gateway/readiness/`)
`GET /v1/readiness` runs 31 checks across sec/int/persistence/dependency/feature/hygiene; singleflight + 15s TTL; each check bounded by `asyncio.wait_for(5.0)`. Rollup: `unready` iff any sec/int red, `degraded` iff any non-warn red/amber, `ready` otherwise. Drift audit: sec/int flips green→red write a `gateway_attempts` row with `disposition="readiness_degraded"` (rate-limited 1/check-id/5min). Use `write_attempt(request_id=, tenant_id=, path=, disposition=, status_code=, reason=)` — `timestamp` kwarg does not exist and silently swallows.
- **Severity convention**: `Severity.sec` / `Severity.int` RED → `unready` (LB keep-out). `Severity.warn` RED → `degraded` only. Use `warn` when the failure reflects an external concern the gateway can't fix (Walacor anchoring lag, upstream config); reserve `int`/`sec` for local invariants.

### Connections (`src/gateway/connections/`)
`GET /v1/connections` — 10 tiles (providers, walacor_delivery, analyzers, tool_loop, model_capabilities, control_plane, auth, readiness, streaming, intelligence_worker). Singleflight + 3s TTL. Per-tile fail-open. All `last_*` fields are 60s window-scoped. Fed by bounded deques attached to each subsystem; nothing persisted.

### OpenWebUI integration (`src/gateway/ollama_proxy.py`, `src/gateway/openwebui/governance.py`)
4 Ollama-shape handlers (`/api/tags|ps|version|show`) proxy to `provider_ollama_url` so OpenWebUI registers the gateway as an *Ollama* connection. `/api/` and `/v1/openwebui/` are exempted in `api_key_middleware._plugin_paths` and `completeness_middleware`. Plugin event governance reproduces the proxy-path pipeline for events that bypassed the proxy (outlet=full, inlet=lightweight, sessions namespaced `owui:{chat_id}`, blocks surface as `blocked_post_facto`). **Source of truth for chain logic is `pipeline/orchestrator.py:_apply_session_chain`** — never reintroduce the old SHA3 Merkle helpers.

## Gotchas

- **Content analysis on thinking models**: `evaluate_post_inference()` reads `model_response.content or model_response.thinking_content`. Without the fallback, qwen3-class models (where the strip moves ALL content into `thinking_content`) silently bypass Llama Guard / PII / toxicity.
- **Walacor envelope $lookup is mandatory for anchor proof**: BlockId/TransId/DH live on the `envelopes` collection, NOT on the data collection. Any `getcomplex` query that wants the anchor fields must include `{"$lookup": {"from": "envelopes", "localField": "EId", "foreignField": "EId", "as": "env"}}` and read from `row["env"][0]`. The previous "sandbox never anchors" finding was a query bug — sandbox does anchor (verified via direct `GET /envelopes/{EId}`).
- **Async-vs-sync reader helper**: `connections/builder.py:_call_reader(fn, *args, **kwargs)` dispatches `await` vs `asyncio.to_thread` based on `inspect.iscoroutinefunction`. Use it for any `LineageReader` / `WalacorLineageReader` call whose impl differs per subclass — the previous direct `asyncio.to_thread` silently dropped the WalacorLineageReader coroutine.
- **PII severity tiers**: `_BLOCK_PII_TYPES = {credit_card, ssn, aws_access_key, api_key}` block; ip_address / email / phone WARN only. Avoids false-positive blocks on educational responses with example IPs.
- **Sealed-in-Walacor drawer** (`/v1/lineage/envelope/<exec_id>`): intentionally returns the RAW envelope (UID/ORGId/SV/EId preserved) — skips `_deserialize_record`. Degrades to 503 / 502 / `envelope:null` so the frontend can render pending/unreachable.
- **`.gitignore *.png` blanket rule** silently excludes dashboard assets. Every frontend PNG must live under `src/gateway/lineage/dashboard/src/assets/**` and be unblocked by the explicit `!...` exception.
- **Session detail lives in `Sessions.jsx`, not `Timeline.jsx`**: `/lineage/?view=sessions` routes through `Sessions.jsx → SessionTimelineView → ChainRecord` (class `ses-chain-card`). `Timeline.jsx` is only the deep-link route. UI changes to records must edit Sessions.jsx (or both).
- **No internal jargon in UI**: grep `Phase [0-9]`, `Phase-`, `-check`, codenames out of JSX/CSS/JS comments before landing — Vite can inline comments into shipped bundles. Customer-facing labels only.
- **Provider error hook gap**: `DefaultResourceMonitor.record_provider_result` is called with `error="HTTP {code}"` on 5xx, but TransportError (timeouts, connection refused) doesn't reach either hook. TODO in `main.py`.

## Dashboard React Rules
- **Rules of Hooks**: every `useMemo`/`useState`/`useEffect`/`useCallback` runs BEFORE any `if (…) return`. A hook after an early return causes React Error #310 and a fully blank page. Reference: `Overview.jsx` — palette `useMemo` sits above the loading/error early returns.
- **Debugging blank dashboard**: minified errors don't name the component. Use Playwright MCP (`browser_navigate` → `browser_console_messages level=error`) to pull line/column in `index-*.js`, then grep the source tree.

## Operations (EC2 gateway_dharm @ 35.165.21.8)
- Gateway runs natively on port 8100 (not Docker). Start/restart via `~/start_gateway_dharm.sh`; logs `/tmp/gateway_dharm.log`; WAL `/tmp/walacor-wal-dharm/`.
- OpenWebUI runs in Docker on port 3100 (`gateway-dharm-openwebui`). Volume `gateway_dharm_webui-data` mounts at `/app/backend/data` → `/var/lib/docker/volumes/gateway_dharm_webui-data/_data/`.
- **OpenWebUI secret key must be persisted to the volume**, not via env var. Without `.webui_secret_key` on disk, OpenWebUI regenerates it on every restart → all sessions invalidated. Fix: write 32-byte `secrets.token_urlsafe` to `<volume>/.webui_secret_key` (chmod 600, root-owned), restart container.
- Multiple orphan `*webui*` volumes exist on the host. Confirm the live one with `docker inspect gateway-dharm-openwebui | grep -A 10 Mounts` before backing up.
- Cleaning orphan processes: `pkill` as `ec2-user` won't touch root-owned uvicorns from the legacy stack; use `sudo pkill` or `sudo kill $(sudo lsof -t -i :<port>)`. Legacy stack stops cleanly with `docker compose -f ~/Gateway/docker-compose.yml down`.
- **PDF compliance export needs Pango + Cairo**. Wired into `scripts/native-setup.sh`, `deploy/Dockerfile`, `deploy/Dockerfile.fips`, and the EC2 start script (self-heal via `ldconfig -p | grep libpango || sudo dnf install`). Without them, `/v1/compliance/export?format=pdf` 501s with `cannot load library 'libpango-1.0-0'`.
