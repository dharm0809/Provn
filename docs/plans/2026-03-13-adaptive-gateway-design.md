# Phase 23: Adaptive Gateway — Design Document

**Date:** 2026-03-13
**Status:** Approved
**Approach:** B+C hybrid — Adaptive Core with extensible interfaces

## Problem Statement

The Gateway has 24 identified areas where behavior is hardcoded or requires manual configuration instead of self-adapting at runtime. These span provider health detection, request classification, content analysis policies, model capability discovery, resource monitoring, and identity validation. The result is a gateway that works well when manually tuned but doesn't adapt to its environment.

## Design Goals

1. **Self-configuring** — detect providers, disk, capabilities at startup without manual config
2. **Runtime-adaptive** — adjust timeouts, retries, WAL limits based on observed behavior
3. **Enterprise-extensible** — every decision point has a documented ABC that enterprises can override
4. **Zero new dependencies** — use stdlib (`shutil`, `importlib`, `hashlib`) and existing deps
5. **Control-plane-driven** — content policies, severity tiers configurable via API without restart
6. **Fail-open** — all probes and monitors degrade gracefully; never block traffic due to a probe failure

## Architecture

### New Package

```
src/gateway/adaptive/
  __init__.py              — load_custom_class() utility, registration helpers
  interfaces.py            — 5 ABCs: StartupProbe, RequestClassifier, CapabilityProbe,
                              IdentityValidator, ResourceMonitor
  startup_probes.py        — ProviderHealthProbe, RoutingEndpointProbe, DiskSpaceProbe,
                              APIVersionProbe
  request_classifier.py    — DefaultRequestClassifier (body > headers > prompt)
  capability_registry.py   — CapabilityRegistry with TTL, re-probing, persistence
  resource_monitor.py      — DefaultResourceMonitor (disk, connections, provider cooldown)
  identity_validator.py    — DefaultIdentityValidator (JWT↔header cross-check)
```

### The 5 Interfaces

```python
class StartupProbe(ABC):
    """Runs at gateway startup to validate environment readiness."""
    @abstractmethod
    async def check(self, http_client, settings) -> ProbeResult: ...

class RequestClassifier(ABC):
    """Classifies incoming requests (user_message, system_task, synthetic, etc.)."""
    @abstractmethod
    def classify(self, prompt: str, headers: dict, body: dict) -> str: ...

class CapabilityProbe(ABC):
    """Discovers model capabilities at runtime."""
    @abstractmethod
    async def probe(self, model_id, provider, http_client) -> dict: ...

class IdentityValidator(ABC):
    """Validates caller identity consistency across auth sources."""
    @abstractmethod
    def validate(self, jwt_identity, header_identity, request) -> ValidationResult: ...

class ResourceMonitor(ABC):
    """Monitors system resources and reports health status."""
    @abstractmethod
    async def check(self) -> ResourceStatus: ...
```

### Enterprise Extension

Custom implementations loaded via Python dotted paths in config:

```
WALACOR_CUSTOM_STARTUP_PROBES=mycompany.probes.DatadogProbe,mycompany.probes.VaultProbe
WALACOR_CUSTOM_REQUEST_CLASSIFIERS=mycompany.classify.CustomClassifier
```

Loader utility (~10 lines):
```python
def load_custom_class(dotted_path: str) -> type:
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)
```

### Control Plane Schema Additions

```sql
CREATE TABLE IF NOT EXISTS content_policies (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '*',
  analyzer_id TEXT NOT NULL,
  category TEXT NOT NULL,
  action TEXT NOT NULL,           -- "block", "warn", "pass"
  threshold REAL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, analyzer_id, category)
);

CREATE TABLE IF NOT EXISTS provider_status (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model_id TEXT,
  capability_key TEXT NOT NULL,
  value TEXT NOT NULL,
  last_checked_at TEXT NOT NULL,
  UNIQUE(provider, model_id, capability_key)
);
```

### New Config Fields (8 total)

```python
startup_probes_enabled: bool = True
provider_health_check_on_startup: bool = True
capability_probe_ttl_seconds: int = 86400      # 24h
identity_validation_enabled: bool = True
disk_monitor_enabled: bool = True
disk_min_free_percent: float = 5.0
custom_startup_probes: str = ""
custom_request_classifiers: str = ""
custom_capability_probes: str = ""
custom_identity_validators: str = ""
custom_resource_monitors: str = ""
```

## Phases

### Phase 23a: Startup Intelligence

**Findings:** #12 (routing endpoints not probed), #17 (no provider check at startup), #20 (WAL disk thresholds not dynamic), #10 (API version not detected)

**Deliverables:**
- `adaptive/__init__.py` — package init + `load_custom_class()`
- `adaptive/interfaces.py` — all 5 ABCs + data classes (ProbeResult, etc.)
- `adaptive/startup_probes.py` — 4 built-in probes:
  - `ProviderHealthProbe` — pings Ollama `/api/tags`, OpenAI `/v1/models`, Anthropic `/v1/models`
  - `RoutingEndpointProbe` — validates every model routing URL is reachable
  - `DiskSpaceProbe` — checks WAL free space, auto-scales `wal_max_size_gb`
  - `APIVersionProbe` — detects Ollama version for compat warnings
- `main.py` — `_run_startup_probes()` called before `_self_test()`
- `health.py` — expose `startup_probes` in `/health` response
- `config.py` — 4 new fields

**Behavior:**
- All probes run concurrently via `asyncio.gather(return_exceptions=True)`
- Failures log WARNING/CRITICAL but never prevent startup (fail-open)
- DiskSpaceProbe auto-adjusts `ctx.effective_wal_max_gb` to 80% of free space (capped at configured max)
- Results stored in `ctx.startup_probe_results` for `/health`
- Custom probes loaded from `custom_startup_probes` config

**Tests:** 12 new tests

### Phase 23b: Request Intelligence

**Findings:** #14 (fragile OpenWebUI regex detection), #15 (header spoofing), #16 (only OpenWebUI tasks detected)

**Deliverables:**
- `adaptive/request_classifier.py` — `DefaultRequestClassifier`:
  - Priority 1: `task` field in request body (OpenWebUI sends `title_generation`, `tags_generation`, etc.)
  - Priority 2: Header-based detection (user-agent for synthetic traffic)
  - Priority 3: Prompt regex fallback (existing patterns, kept for backward compat)
- `adaptive/identity_validator.py` — `DefaultIdentityValidator`:
  - Cross-checks `X-User-Id` header against JWT `sub` claim
  - JWT always wins on mismatch; mismatch logged as WARNING with client IP
  - Merges JWT + header fields (JWT priority, headers fill gaps)
- `orchestrator.py` — replace `_classify_request_type()` with `ctx.request_classifier.classify()`
- `main.py` — initialize classifier and validator in `on_startup()`
- `lineage/reader.py` — add `system_task_count` to session list SQL

**Behavior:**
- Body-based detection is 100x more reliable than prompt regex
- Synthetic traffic (curl, httpie, k6, python-requests) auto-tagged as `"synthetic"`
- Identity mismatches don't block requests — they log warnings and audit the mismatch
- Custom classifiers/validators loaded from config

**Tests:** 15 new tests

### Phase 23c: Content Policy Engine

**Findings:** #1 (Llama Guard categories hardcoded), #2 (PII block types hardcoded), #3 (toxicity patterns hardcoded), #23 (no contextual thresholds)

**Deliverables:**
- `control/store.py` — add `content_policies` table to schema + CRUD methods
- `control/api.py` — 3 new endpoints: `GET/POST/DELETE /v1/control/content-policies`
- `content/pii_detector.py` — add `configure(policies)` method
- `content/toxicity_detector.py` — add `configure(policies)` method
- `content/llama_guard.py` — add `configure(policies)` method
- `control/api.py` — `_refresh_content_policies()` hook called on mutation
- Dashboard — "Content Policies" sub-tab under Control

**Default seed data:**
- PII: credit_card/ssn/aws_key/api_key → BLOCK; email/phone/ip → WARN
- Llama Guard: S4 (child_safety) → BLOCK; S1-S3, S5-S14 → WARN
- Toxicity: child_safety → BLOCK; self_harm/violence → WARN

**Behavior:**
- On startup, `load_into_caches()` loads content policies and calls `configure()` on each analyzer
- On API mutation, `_refresh_content_policies()` immediately reconfigures running analyzers
- Without control plane, analyzers use their current hardcoded defaults (backward compat)
- Per-tenant policies supported via `tenant_id` field (default `*` = all tenants)
- Enterprise: custom analyzers just need to implement `ContentAnalyzer` ABC + optional `configure()`

**Tests:** 14 new tests

### Phase 23d: Runtime Adaptation

**Findings:** #8 (capability re-probing), #9 (per-model timeouts), #18 (provider-aware retry), #21 (connection pool monitoring), #24 (content analysis caching)

**Deliverables:**
- `adaptive/capability_registry.py` — `CapabilityRegistry`:
  - Replaces `_model_capabilities` dict in orchestrator
  - TTL-based staleness (default 24h); stale entries re-probed on next request
  - `get_timeout(model_id)` — reasoning models get 2x, embeddings get 0.5x
  - Optional persistence to `provider_status` control plane table
- `adaptive/resource_monitor.py` — `DefaultResourceMonitor`:
  - Disk space checks on 60s interval
  - Provider error rate tracking (sliding window, LiteLLM-style cooldown)
  - Active request counting
- `orchestrator.py`:
  - `_get_retry_config(provider, resource_monitor)` — provider-aware retry params
  - Per-model timeout from routing JSON `timeout_ms` or capability registry
- `main.py`:
  - httpx event hooks for provider response tracking
  - `_run_resource_monitor_loop()` background task
  - `_run_capability_refresh_loop()` background task
- `response_evaluator.py` — SHA256-keyed analysis cache (max 1000 entries)
- `control/store.py` — add `provider_status` table

**Behavior:**
- Provider cooldown: >50% failure rate in 60s window → 30s cooldown → reduce retries to 1
- Model timeout: chat=60s, reasoning=120s, embedding=30s, route override via `timeout_ms`
- Content analysis cache: identical text produces same decisions (cache hit), bounded at 1000 entries
- Capability re-probe: after TTL expires, next request triggers fresh probe
- All adaptation is logged at DEBUG level for operator visibility

**Tests:** 18 new tests

## Findings Coverage

| Finding | Phase | Solution |
|---------|-------|----------|
| #1 Llama Guard categories hardcoded | 23c | Configurable via content_policies table |
| #2 PII block types hardcoded | 23c | Configurable via content_policies table |
| #3 Toxicity patterns hardcoded | 23c | Configurable via content_policies table |
| #4 Model digest cache TTL | 23d | Part of capability registry TTL |
| #5 JWKS cache TTL | Config field | `jwt_jwks_cache_ttl` (simple addition) |
| #6 Stream buffer size | Config field | Already configurable, just needs docs |
| #7 JWT algorithm auto-detect | Not implemented | Security risk per RFC 8725 — keep allowlist |
| #8 Capability re-probing | 23d | TTL-based staleness in CapabilityRegistry |
| #9 Per-model timeouts | 23d | CapabilityRegistry.get_timeout() + route override |
| #10 API version detection | 23a | APIVersionProbe at startup |
| #11 GenericAdapter format drift | Low priority | Not in scope (rare edge case) |
| #12 Routing endpoints not probed | 23a | RoutingEndpointProbe at startup |
| #13 Response path not validated | Low priority | Not in scope (rare edge case) |
| #14 OpenWebUI detection fragile | 23b | Body `task` field detection (primary) |
| #15 Header spoofing | 23b | JWT↔header cross-validation |
| #16 Only OpenWebUI tasks detected | 23b | Synthetic traffic detection via user-agent |
| #17 No provider check at startup | 23a | ProviderHealthProbe |
| #18 Provider-aware retry | 23d | _get_retry_config() with cooldown |
| #19 WAL delivery backoff | 23d | Provider cooldown feeds into retry |
| #20 WAL disk thresholds | 23a | DiskSpaceProbe auto-scaling |
| #21 Connection pool monitoring | 23d | httpx event hooks + ResourceMonitor |
| #22 K8s service discovery | Future | DNS-based, out of scope for Phase 23 |
| #23 Content analysis not contextual | 23c | Per-category thresholds in content_policies |
| #24 Content analysis not cached | 23d | SHA256-keyed cache in response_evaluator |

## Totals

| Metric | Count |
|--------|-------|
| New files | 7 |
| Modified files | ~10 |
| New tests | 59 |
| New config fields | 8 |
| New API endpoints | 3 |
| New DB tables | 2 |
| New dependencies | 0 |

## Decision Log

| Decision | Rationale |
|----------|-----------|
| Approach B+C hybrid | Clean adaptive core + extensible ABCs for enterprise differentiation |
| No plugin discovery/marketplace | YAGNI — importlib.import_module is sufficient for class loading |
| JWT algorithm auto-detect excluded | RFC 8725 security risk — keep configured allowlist |
| K8s discovery deferred | Needs dns-python dep + K8s-specific testing; future phase |
| GenericAdapter format drift excluded | Extremely rare; monitoring cost > benefit |
| Fail-open everywhere | Probes/monitors must never block traffic |
| Body `task` field over regex | OpenWebUI sends explicit task types; 100x more reliable |
| LiteLLM cooldown pattern | Proven in production; simple sliding window implementation |
| Content analysis cache bounded at 1000 | Prevents unbounded memory growth; LRU not needed (dict is fast) |
