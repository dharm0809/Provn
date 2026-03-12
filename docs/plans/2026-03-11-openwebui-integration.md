# OpenWebUI Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Walacor Gateway a drop-in governed backend for any OpenWebUI deployment — one env var change, zero disruption to the chat experience, full provable audit trail.

**Architecture:** Four targeted changes: (1) fix `/v1/models` to discover models when the attested list is empty so OpenWebUI's model selector works on first boot; (2) add `WALACOR_SESSION_HEADER_NAMES` config so OpenWebUI's `X-OpenWebUI-Chat-Id` maps to Gateway's session chain; (3) ship an `openwebui` Docker Compose profile; (4) write the two-minute quickstart doc.

**Tech Stack:** Python/Starlette (Gateway), SQLite WAL, Docker Compose v3.8, OpenWebUI `ghcr.io/open-webui/open-webui:main`

---

## Context: What Already Exists

- `src/gateway/models_api.py` — `list_models()` handler, registered at `GET /v1/models` in `main.py:760`
- `src/gateway/control/discovery.py` — `discover_provider_models(settings, http_client)` — queries Ollama and OpenAI; already used by the control plane discover endpoint
- `tests/unit/test_models_api.py` — 3 existing tests; `test_models_endpoint_no_control_store_uses_discovery` currently asserts empty `[]` (wrong — needs fixing)
- `deploy/docker-compose.yml` — has `demo` and `ollama` profiles; no `openwebui` profile
- Session ID is read in all 4 adapters as `request.headers.get("x-session-id") or str(uuid.uuid4())`

---

## Task 1: Fix `/v1/models` — Discovery Fallback + 60s Cache

**Problem:** When `control_store` has zero active attestations (fresh deployment) or `skip_governance=True`, `list_models()` returns `[]`. OpenWebUI shows no models and the demo fails immediately.

**Fix:** When the attested list is empty, fall back to `discover_provider_models()`. Add a 60s module-level cache so OpenWebUI's frequent polling doesn't hammer Ollama.

**Files:**
- Modify: `src/gateway/models_api.py`
- Modify: `tests/unit/test_models_api.py`

---

### Step 1: Write the new failing tests

Add to `tests/unit/test_models_api.py`:

```python
@pytest.mark.anyio
async def test_models_falls_back_to_discovery_when_no_control_store():
    """When control_store is None, discovers models from providers."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    mock_http = MagicMock()

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings"):

        mock_ctx.return_value.control_store = None
        mock_ctx.return_value.http_client = mock_http
        mock_discover.return_value = [
            {"model_id": "qwen3:4b", "provider": "ollama"},
            {"model_id": "gemma3:1b", "provider": "ollama"},
        ]

        scope = {"type": "http", "method": "GET", "path": "/v1/models",
                 "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)

    import json
    body = json.loads(response.body)
    assert body["object"] == "list"
    assert len(body["data"]) == 2
    assert body["data"][0]["id"] == "qwen3:4b"
    assert body["data"][0]["owned_by"] == "ollama"


@pytest.mark.anyio
async def test_models_falls_back_to_discovery_when_store_has_no_active():
    """When control_store exists but has zero active attestations, uses discovery."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    mock_store = MagicMock()
    mock_store.list_attestations.return_value = []  # empty — fresh deployment

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings"):

        mock_ctx.return_value.control_store = mock_store
        mock_ctx.return_value.http_client = MagicMock()
        mock_discover.return_value = [
            {"model_id": "llama3.2:3b", "provider": "ollama"},
        ]

        scope = {"type": "http", "method": "GET", "path": "/v1/models",
                 "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)

    import json
    body = json.loads(response.body)
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == "llama3.2:3b"


@pytest.mark.anyio
async def test_models_cache_serves_without_rediscovery():
    """Second call within TTL returns cached result without calling discovery again."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings"):

        mock_ctx.return_value.control_store = None
        mock_ctx.return_value.http_client = MagicMock()
        mock_discover.return_value = [{"model_id": "qwen3:4b", "provider": "ollama"}]

        scope = {"type": "http", "method": "GET", "path": "/v1/models",
                 "query_string": b"", "headers": []}

        await list_models(Request(scope))
        await list_models(Request(scope))  # second call

    # discovery should only have been called once
    assert mock_discover.call_count == 1
```

### Step 2: Run the new tests to confirm they fail

```bash
cd /Users/dharmpratapsingh/Walcor/Gateway
python -m pytest tests/unit/test_models_api.py::test_models_falls_back_to_discovery_when_no_control_store tests/unit/test_models_api.py::test_models_falls_back_to_discovery_when_store_has_no_active tests/unit/test_models_api.py::test_models_cache_serves_without_rediscovery -v
```

Expected: `FAILED` — `_invalidate_models_cache` does not exist yet.

### Step 3: Rewrite `src/gateway/models_api.py`

Replace the entire file:

```python
"""GET /v1/models — OpenAI-compatible model listing.

Falls back to live provider discovery when:
  - No embedded control plane (skip_governance mode), OR
  - Control plane exists but has zero active attestations (fresh deployment)

Results are cached for 60 seconds so OpenWebUI's frequent polling (~1s) does
not hammer Ollama/OpenAI on every request.
"""
import time
import logging
from starlette.requests import Request
from starlette.responses import JSONResponse
from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)

# ── 60-second in-memory cache ─────────────────────────────────────────────────
_models_cache: list[dict] = []
_models_cache_at: float = 0.0
_MODELS_CACHE_TTL = 60.0


def _invalidate_models_cache() -> None:
    """Force next request to bypass cache. Called by tests and control-plane mutations."""
    global _models_cache, _models_cache_at
    _models_cache = []
    _models_cache_at = 0.0


async def _build_models_list(ctx) -> list[dict]:
    """Build the OpenAI-format model list from attestations or discovery."""
    now = int(time.time())

    # ── Path 1: attested models from embedded control plane ───────────────────
    if ctx.control_store:
        attestations = ctx.control_store.list_attestations()
        active = [a for a in attestations if a.get("status") == "active"]
        if active:
            return [
                {
                    "id": a["model_id"],
                    "object": "model",
                    "created": now,
                    "owned_by": a.get("provider", "walacor-gateway"),
                }
                for a in active
            ]
        # Fall through: control store exists but no attested models yet (fresh deployment)
        logger.info("/v1/models: control store has no active attestations — falling back to discovery")

    # ── Path 2: live discovery from configured providers ──────────────────────
    if not ctx.http_client:
        logger.debug("/v1/models: no http_client available — returning empty list")
        return []

    try:
        from gateway.config import get_settings
        from gateway.control.discovery import discover_provider_models

        settings = get_settings()
        discovered = await discover_provider_models(settings, ctx.http_client)
        logger.info("/v1/models: discovered %d model(s) from providers", len(discovered))
        return [
            {
                "id": m["model_id"],
                "object": "model",
                "created": now,
                "owned_by": m.get("provider", "walacor-gateway"),
            }
            for m in discovered
        ]
    except Exception:
        logger.warning("/v1/models: discovery failed", exc_info=True)
        return []


async def list_models(request: Request) -> JSONResponse:
    global _models_cache, _models_cache_at

    # Serve from cache if fresh
    now = time.monotonic()
    if _models_cache and (now - _models_cache_at) < _MODELS_CACHE_TTL:
        return JSONResponse({"object": "list", "data": _models_cache})

    ctx = get_pipeline_context()
    models = await _build_models_list(ctx)

    _models_cache = models
    _models_cache_at = now

    return JSONResponse({"object": "list", "data": models})
```

### Step 4: Update the existing test that expected `[]` to be correct now

In `tests/unit/test_models_api.py`, update `test_models_endpoint_no_control_store_uses_discovery`:

```python
@pytest.mark.anyio
async def test_models_endpoint_no_control_store_uses_discovery():
    """When no control store, discovers from providers (not empty list)."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings"):

        mock_ctx.return_value.control_store = None
        mock_ctx.return_value.http_client = MagicMock()
        mock_discover.return_value = []  # no providers configured — empty is valid

        scope = {"type": "http", "method": "GET", "path": "/v1/models",
                 "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)

    import json
    body = json.loads(response.body)
    assert body["object"] == "list"
    assert body["data"] == []  # empty because no providers — not a crash
```

Also add `_invalidate_models_cache()` at the start of the two existing tests that mock `control_store` (to prevent cache bleed between tests):

```python
# At the top of test_models_endpoint_returns_openai_format and test_models_excludes_revoked:
from gateway.models_api import list_models, _invalidate_models_cache
_invalidate_models_cache()
```

### Step 5: Run all models_api tests

```bash
python -m pytest tests/unit/test_models_api.py -v
```

Expected: All 6 tests PASS.

### Step 6: Commit

```bash
git add src/gateway/models_api.py tests/unit/test_models_api.py
git commit -m "feat: /v1/models falls back to provider discovery when no attested models

Fixes OpenWebUI model selector showing blank on fresh deployment or in
skip_governance mode. Adds 60s in-memory cache to absorb OpenWebUI's
frequent polling. Falls back to discover_provider_models() from
control/discovery.py when control_store is absent or has zero active
attestations.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: `WALACOR_SESSION_HEADER_NAMES` — Multi-Header Session Resolution

**Problem:** OpenWebUI sends the conversation ID as `X-OpenWebUI-Chat-Id`. Gateway reads only `X-Session-ID`. Result: every request starts a new session chain, even if it's part of the same conversation.

**Fix:** Add `WALACOR_SESSION_HEADER_NAMES` config (comma-separated, checked in order). Extract session ID resolution into a utility function so all 4 adapters use the same logic.

**Files:**
- Modify: `src/gateway/config.py` (add field + property)
- Create: `src/gateway/util/session_id.py` (new utility)
- Modify: `src/gateway/adapters/openai.py`
- Modify: `src/gateway/adapters/ollama.py`
- Modify: `src/gateway/adapters/anthropic.py`
- Modify: `src/gateway/adapters/generic.py`
- Create: `tests/unit/test_session_id.py`

---

### Step 1: Write the failing tests

Create `tests/unit/test_session_id.py`:

```python
"""Tests for multi-header session ID resolution."""
from unittest.mock import MagicMock


def _make_request(headers: dict) -> MagicMock:
    """Create a mock request with given headers (lowercased keys)."""
    req = MagicMock()
    req.headers = {k.lower(): v for k, v in headers.items()}
    return req


def test_resolves_primary_header():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({"X-Session-ID": "session-abc"})
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result == "session-abc"


def test_resolves_fallback_header():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({"X-OpenWebUI-Chat-Id": "chat-xyz"})
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result == "chat-xyz"


def test_primary_takes_precedence_over_fallback():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({
        "X-Session-ID": "primary",
        "X-OpenWebUI-Chat-Id": "fallback",
    })
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result == "primary"


def test_generates_uuid_when_no_header_matches():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({})
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result  # non-empty
    assert len(result) == 36  # UUID format


def test_empty_header_value_treated_as_missing():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({"X-Session-ID": "", "X-OpenWebUI-Chat-Id": "chat-123"})
    result = resolve_session_id(req, ["X-Session-ID", "X-OpenWebUI-Chat-Id"])
    assert result == "chat-123"


def test_single_header_name_list():
    from gateway.util.session_id import resolve_session_id
    req = _make_request({"X-Session-ID": "only-one"})
    result = resolve_session_id(req, ["X-Session-ID"])
    assert result == "only-one"
```

### Step 2: Run tests to confirm they fail

```bash
python -m pytest tests/unit/test_session_id.py -v
```

Expected: `FAILED` — `gateway.util.session_id` does not exist.

### Step 3: Create `src/gateway/util/session_id.py`

```python
"""Session ID resolution from HTTP request headers.

Checks a prioritized list of header names and returns the first non-empty value.
Falls back to a fresh UUID if none match. This allows a single Gateway to serve
multiple UI clients (OpenWebUI, LibreChat, LobeChat, custom) that each use
different session header names.
"""
from __future__ import annotations

import uuid


def resolve_session_id(request, header_names: list[str]) -> str:
    """Return the first non-empty value from the given header names, or a new UUID.

    Args:
        request: Any object with a `headers` dict-like attribute (lowercased keys).
        header_names: Ordered list of header names to check (case-insensitive).

    Returns:
        Session ID string — always non-empty.
    """
    for name in header_names:
        value = request.headers.get(name.lower(), "").strip()
        if value:
            return value
    return str(uuid.uuid4())
```

### Step 4: Run tests to confirm they pass

```bash
python -m pytest tests/unit/test_session_id.py -v
```

Expected: All 6 tests PASS.

### Step 5: Add config field to `src/gateway/config.py`

Find the `# Phase 21: JWT/SSO authentication` block (around line 34). Add the new field just before or after the existing session chain fields (around line 116):

```python
# Phase 23 (OpenWebUI integration): configurable session header names
session_header_names: str = Field(
    default="X-Session-ID,X-OpenWebUI-Chat-Id,X-Chat-Id",
    description=(
        "Comma-separated list of request header names to check for session ID, "
        "in priority order. First non-empty match wins. Falls back to UUID. "
        "Allows OpenWebUI (X-OpenWebUI-Chat-Id), LibreChat, and custom UIs to "
        "share the same session chain semantics."
    ),
)
```

Then add the property (after the existing `api_keys_list` and `jwt_algorithms_list` properties):

```python
@property
def session_header_names_list(self) -> list[str]:
    """Parsed session header names in priority order."""
    return [h.strip() for h in self.session_header_names.split(",") if h.strip()]
```

### Step 6: Update all 4 adapters to use `resolve_session_id`

**In each adapter's `parse_request` (or equivalent) method, find the line:**
```python
metadata["session_id"] = request.headers.get("x-session-id") or str(uuid.uuid4())
```
**Replace with:**
```python
from gateway.util.session_id import resolve_session_id
metadata["session_id"] = resolve_session_id(request, get_settings().session_header_names_list)
```

The four files and what to search for:

**`src/gateway/adapters/openai.py`** — search for `x-session-id`, update the assignment.

**`src/gateway/adapters/ollama.py`** — same pattern.

**`src/gateway/adapters/anthropic.py`** — same pattern.

**`src/gateway/adapters/generic.py`** — same pattern.

In each file, add at the top of the method:
```python
from gateway.config import get_settings
from gateway.util.session_id import resolve_session_id
```

### Step 7: Run full test suite

```bash
python -m pytest tests/unit/ -v --tb=short 2>&1 | tail -20
```

Expected: All existing tests pass. Note the total count.

### Step 8: Commit

```bash
git add src/gateway/util/session_id.py tests/unit/test_session_id.py \
        src/gateway/config.py \
        src/gateway/adapters/openai.py \
        src/gateway/adapters/ollama.py \
        src/gateway/adapters/anthropic.py \
        src/gateway/adapters/generic.py
git commit -m "feat: configurable session header names for multi-UI support

Adds WALACOR_SESSION_HEADER_NAMES (default: X-Session-ID,X-OpenWebUI-Chat-Id,X-Chat-Id).
OpenWebUI's X-OpenWebUI-Chat-Id now maps to Gateway's session chain automatically.
All 4 adapters use shared resolve_session_id() utility.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Docker Compose `openwebui` Profile

**Goal:** `docker compose --profile openwebui up` gives a complete governed stack (Ollama + Gateway + OpenWebUI) with network isolation so Ollama is unreachable from OpenWebUI directly.

**Files:**
- Modify: `deploy/docker-compose.yml`

---

### Step 1: Read the current compose file

Already done — it's 110 lines with `redis`, `ollama`, `gateway`, and `demo-init` services.

### Step 2: Add the OpenWebUI service and networks

Append to `deploy/docker-compose.yml`. The final file should look like this (add the `openwebui` service, `webui-data` volume, and explicit networks):

```yaml
  openwebui:
    image: ghcr.io/open-webui/open-webui:main
    profiles: [openwebui]
    ports:
      - "3000:8080"
    volumes:
      - webui-data:/app/backend/data
    environment:
      # Route ALL chat traffic through Gateway (governed)
      - OPENAI_API_BASE_URL=http://gateway:8000/v1
      - OPENAI_API_KEY=${WALACOR_GATEWAY_API_KEYS:-dev-key}
      # Disable direct Ollama access — all traffic must go through Gateway
      - ENABLE_OLLAMA_API=false
      # Forward OpenWebUI user identity (name, id, email, role) to Gateway
      - ENABLE_FORWARD_USER_INFO_HEADERS=true
      # Disable user-level direct API connections (prevents governance bypass)
      - ENABLE_DIRECT_CONNECTIONS=false
      # Branding
      - WEBUI_NAME=${WEBUI_NAME:-Walacor Chat}
    depends_on:
      gateway:
        condition: service_healthy
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      start_period: 30s
      retries: 5
```

And add `webui-data` to the volumes block:

```yaml
volumes:
  gateway-wal: {}
  ollama_data: {}
  webui-data: {}    # ← add this
```

### Step 3: Verify compose file is valid

```bash
cd /Users/dharmpratapsingh/Walcor/Gateway
docker compose -f deploy/docker-compose.yml --profile openwebui config --quiet
```

Expected: No errors, exits 0.

### Step 4: Run a smoke test (requires Docker)

```bash
# Start just the services needed (no GPU required — CPU inference)
WALACOR_GATEWAY_API_KEYS=test-key \
docker compose -f deploy/docker-compose.yml \
  --profile openwebui --profile ollama \
  up gateway ollama openwebui --detach

# Wait for gateway health
sleep 20
curl -s http://localhost:8002/health | python3 -m json.tool | grep '"status"'

# Verify /v1/models returns models (OpenWebUI's model selector will be populated)
curl -s http://localhost:8002/v1/models | python3 -m json.tool | grep '"id"'

# Verify OpenWebUI is running
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/

docker compose -f deploy/docker-compose.yml --profile openwebui --profile ollama down
```

Expected:
- `/health` returns `"status": "ok"`
- `/v1/models` returns at least one model (after Ollama pulls one)
- OpenWebUI returns HTTP 200

### Step 5: Commit

```bash
git add deploy/docker-compose.yml
git commit -m "feat: add openwebui Docker Compose profile

docker compose --profile openwebui --profile ollama up
gives a complete governed stack: Ollama + Gateway + OpenWebUI.
OpenWebUI is pre-configured to route all chat through Gateway,
forward user identity headers, and disable direct connections.
WEBUI_NAME defaults to 'Walacor Chat' (configurable via env).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: Quickstart Documentation

**Goal:** A concise, copy-paste-ready guide for OpenWebUI users to add Gateway governance in under 2 minutes.

**Files:**
- Create: `docs/OPENWEBUI-QUICKSTART.md`

---

### Step 1: Write the quickstart doc

Create `docs/OPENWEBUI-QUICKSTART.md`:

````markdown
# OpenWebUI + Walacor Gateway — 2-Minute Quickstart

> **What you get:** Every conversation in your existing OpenWebUI is now part of a provable, immutable audit trail — Merkle-chain verified, PII-detected, policy-governed. Your users see zero difference.

---

## Option A: Fresh Stack (Recommended)

One command gives you Ollama + Gateway + OpenWebUI, fully wired:

```bash
git clone https://github.com/your-org/walacor-gateway
cd walacor-gateway/Gateway

WALACOR_GATEWAY_API_KEYS=your-secret-key \
docker compose -f deploy/docker-compose.yml \
  --profile openwebui --profile ollama \
  up -d
```

| Service | URL |
|---------|-----|
| Chat UI (OpenWebUI) | http://localhost:3000 |
| Governance Dashboard | http://localhost:8002/lineage/ |
| Gateway API | http://localhost:8002 |

Pull a model then start chatting:
```bash
docker exec -it $(docker compose ps -q ollama) ollama pull qwen3:4b
```

Every message you send appears in the governance dashboard within seconds with its chain sequence, policy verdict, and PII status.

---

## Option B: Add to Existing OpenWebUI

If you already run OpenWebUI, change two env vars and restart:

```bash
# Before (direct to Ollama)
OPENAI_API_BASE_URL=http://ollama:11434/v1

# After (through Gateway)
OPENAI_API_BASE_URL=http://gateway:8000/v1
OPENAI_API_KEY=your-gateway-key
ENABLE_FORWARD_USER_INFO_HEADERS=true
ENABLE_DIRECT_CONNECTIONS=false
ENABLE_OLLAMA_API=false
```

Your users notice nothing different. Gateway now governs every request.

---

## What Gateway Adds Invisibly

For every message sent through OpenWebUI, Gateway:

1. **Attests the model** — cryptographic proof of which model handled the request
2. **Evaluates policy** — configurable rules (block PII, restrict models, enforce budgets)
3. **Detects PII** — credit cards, SSNs, API keys blocked before reaching the model
4. **Chains the session** — every turn in a conversation is Merkle-linked; tamper-evident
5. **Records the audit trail** — immutable SQLite WAL, exportable for compliance

---

## Governance Dashboard

After sending a few messages, visit http://localhost:8002/lineage/ to see:

- **Sessions** — every conversation, linked by session chain
- **Chain verification** — cryptographic proof each turn is unmodified
- **Policy results** — ALLOWED / BLOCKED per request
- **PII incidents** — what was detected and what action was taken
- **Token usage** — per-user, per-model, per-period

---

## Required OpenWebUI Settings (for governed deployments)

| Setting | Value | Why |
|---------|-------|-----|
| `ENABLE_OLLAMA_API` | `false` | Forces all chat through Gateway |
| `ENABLE_FORWARD_USER_INFO_HEADERS` | `true` | User identity in audit trail |
| `ENABLE_DIRECT_CONNECTIONS` | `false` | Prevents governance bypass |
| `OPENAI_API_BASE_URL` | `http://gateway:8000/v1` | Routes traffic to Gateway |

> **Note:** `ENABLE_DIRECT_CONNECTIONS=false` is non-negotiable for a governed deployment. If a user adds their own API key in OpenWebUI's settings, their conversations bypass Gateway entirely and have no audit trail.

---

## Troubleshooting

**OpenWebUI shows no models**
Gateway auto-discovers models from Ollama. If the model selector is empty, ensure Ollama has at least one model pulled: `ollama pull qwen3:4b`. The model list refreshes every 60 seconds.

**Conversations don't appear in the dashboard**
Check that `ENABLE_FORWARD_USER_INFO_HEADERS=true` and `ENABLE_DIRECT_CONNECTIONS=false` are set. Verify Gateway is healthy: `curl http://gateway:8000/health`.

**Gateway returns 401**
Ensure `OPENAI_API_KEY` in OpenWebUI matches `WALACOR_GATEWAY_API_KEYS` in Gateway. These must be identical.
````

### Step 2: Verify the doc reads cleanly

```bash
# Check markdown renders (requires pandoc or just review manually)
wc -l docs/OPENWEBUI-QUICKSTART.md
```

Expected: ~90–120 lines.

### Step 3: Commit

```bash
git add docs/OPENWEBUI-QUICKSTART.md
git commit -m "docs: OpenWebUI quickstart guide

Two-minute guide for OpenWebUI users to add Gateway governance.
Covers fresh stack (docker compose --profile openwebui) and
adding Gateway to an existing OpenWebUI deployment.
Includes required settings, what Gateway adds, and troubleshooting.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Final Verification

Run the full unit test suite to confirm no regressions:

```bash
python -m pytest tests/unit/ -v --tb=short 2>&1 | tail -5
```

Expected: All tests pass (207+ pass, 2 skip, 0 fail).

---

## Summary

| Task | Files Changed | Tests Added |
|------|--------------|-------------|
| `/v1/models` discovery fallback + cache | `models_api.py` | 3 new tests |
| Session header names config | `config.py`, `session_id.py`, 4 adapters | 6 new tests |
| Docker Compose OpenWebUI profile | `docker-compose.yml` | Manual smoke test |
| Quickstart documentation | `OPENWEBUI-QUICKSTART.md` | — |
