# OpenWebUI Deep Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Integrate Gateway and OpenWebUI deeply across three pillars: governance visibility in chat, enterprise identity/RBAC from OpenWebUI roles, and operational intelligence via status endpoint and Pipeline plugin.

**Architecture:** Gateway enriches HTTP response headers with content analysis, budget, and model metadata. A new `/v1/openwebui/status` endpoint exposes banners, budget, and model health. An optional OpenWebUI Pipeline plugin reads these to render governance badges in chat. OpenWebUI identity headers (`X-OpenWebUI-User-*`) are captured into `CallerIdentity` for policy rules and per-user budgets.

**Tech Stack:** Python (Starlette routes, httpx), OpenWebUI Pipelines framework (Python outlet/inlet filters), existing policy engine + budget tracker.

---

### Task 1: CORS Expose Headers

Browsers block JavaScript from reading custom response headers unless the server explicitly exposes them via `Access-Control-Expose-Headers`. OpenWebUI's frontend can't read `x-walacor-*` headers without this.

**Files:**
- Modify: `src/gateway/main.py:148-153`
- Test: `tests/unit/test_cors_headers.py`

**Step 1: Write the failing test**

Create `tests/unit/test_cors_headers.py`:

```python
"""Tests for CORS headers including Access-Control-Expose-Headers."""

from gateway.main import _CORS_HEADERS


class TestCorsHeaders:
    def test_expose_headers_present(self):
        assert "Access-Control-Expose-Headers" in _CORS_HEADERS

    def test_expose_headers_includes_walacor(self):
        expose = _CORS_HEADERS["Access-Control-Expose-Headers"]
        assert "x-walacor-execution-id" in expose
        assert "x-walacor-attestation-id" in expose
        assert "x-walacor-chain-seq" in expose
        assert "x-walacor-policy-result" in expose
        assert "x-walacor-content-analysis" in expose
        assert "x-walacor-budget-remaining" in expose
        assert "x-walacor-budget-percent" in expose
        assert "x-walacor-model-id" in expose
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_cors_headers.py -v`
Expected: FAIL — `Access-Control-Expose-Headers` not in `_CORS_HEADERS`

**Step 3: Write minimal implementation**

In `src/gateway/main.py`, update `_CORS_HEADERS` dict (around line 148):

```python
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-API-Key, X-Session-ID, X-User-Id, X-User-Email, X-User-Roles, X-Team-Id",
    "Access-Control-Expose-Headers": "x-walacor-execution-id, x-walacor-attestation-id, x-walacor-chain-seq, x-walacor-policy-result, x-walacor-content-analysis, x-walacor-budget-remaining, x-walacor-budget-percent, x-walacor-model-id, X-Session-Id, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset",
    "Access-Control-Max-Age": "86400",
}
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_cors_headers.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/unit/test_cors_headers.py src/gateway/main.py
git commit -m "feat: CORS expose x-walacor-* headers for browser clients"
```

---

### Task 2: Enriched Governance Response Headers

Add 4 new headers to every non-streaming response: content analysis verdict, budget remaining, budget percent, and model ID. Also add these fields to the streaming governance SSE event.

**Files:**
- Modify: `src/gateway/pipeline/orchestrator.py:169-178` (expand `_add_governance_headers`)
- Modify: `src/gateway/pipeline/orchestrator.py:1552-1559` (pass new params at call site)
- Modify: `src/gateway/pipeline/orchestrator.py:1411` (add to streaming `governance_meta`)
- Modify: `src/gateway/pipeline/orchestrator.py:787-789` (add to `_after_stream_record` governance_meta population)
- Modify: `src/gateway/pipeline/forwarder.py:22-34` (expand `build_governance_sse_event`)
- Test: `tests/unit/test_governance_headers.py`

**Step 1: Write the failing test**

Create `tests/unit/test_governance_headers.py`:

```python
"""Tests for enriched governance response headers."""

from unittest.mock import MagicMock

from gateway.pipeline.orchestrator import _add_governance_headers
from gateway.pipeline.forwarder import build_governance_sse_event


class TestAddGovernanceHeaders:
    def test_existing_headers_still_set(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp, execution_id="exec-1", attestation_id="att-1", chain_seq=3, policy_result="pass")
        assert resp.headers["x-walacor-execution-id"] == "exec-1"
        assert resp.headers["x-walacor-attestation-id"] == "att-1"
        assert resp.headers["x-walacor-chain-seq"] == "3"
        assert resp.headers["x-walacor-policy-result"] == "pass"

    def test_new_content_analysis_header(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp, content_analysis="pii_warn")
        assert resp.headers["x-walacor-content-analysis"] == "pii_warn"

    def test_new_budget_headers(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp, budget_remaining=5000, budget_percent=82)
        assert resp.headers["x-walacor-budget-remaining"] == "5000"
        assert resp.headers["x-walacor-budget-percent"] == "82"

    def test_new_model_id_header(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp, model_id="qwen3:4b")
        assert resp.headers["x-walacor-model-id"] == "qwen3:4b"

    def test_none_values_not_set(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp)
        assert "x-walacor-content-analysis" not in resp.headers
        assert "x-walacor-budget-remaining" not in resp.headers
        assert "x-walacor-budget-percent" not in resp.headers
        assert "x-walacor-model-id" not in resp.headers


class TestBuildGovernanceSseEvent:
    def test_includes_new_fields(self):
        event = build_governance_sse_event(
            execution_id="exec-1", content_analysis="clean",
            budget_remaining=1000, budget_percent=50, model_id="qwen3:4b",
        )
        text = event.decode()
        assert "event: governance" in text
        assert '"content_analysis": "clean"' in text
        assert '"budget_remaining": 1000' in text
        assert '"budget_percent": 50' in text
        assert '"model_id": "qwen3:4b"' in text

    def test_omits_none_fields(self):
        event = build_governance_sse_event(execution_id="exec-1")
        text = event.decode()
        assert "content_analysis" not in text
        assert "budget_remaining" not in text
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_governance_headers.py -v`
Expected: FAIL — `_add_governance_headers() got an unexpected keyword argument 'content_analysis'`

**Step 3: Write minimal implementation**

Update `_add_governance_headers` in `src/gateway/pipeline/orchestrator.py` (line ~169):

```python
def _add_governance_headers(
    response, execution_id=None, attestation_id=None, chain_seq=None,
    policy_result=None, content_analysis=None, budget_remaining=None,
    budget_percent=None, model_id=None,
):
    """Add X-Walacor-* governance metadata headers to response."""
    if execution_id:
        response.headers["x-walacor-execution-id"] = str(execution_id)
    if attestation_id:
        response.headers["x-walacor-attestation-id"] = str(attestation_id)
    if chain_seq is not None:
        response.headers["x-walacor-chain-seq"] = str(chain_seq)
    if policy_result:
        response.headers["x-walacor-policy-result"] = str(policy_result)
    if content_analysis:
        response.headers["x-walacor-content-analysis"] = str(content_analysis)
    if budget_remaining is not None:
        response.headers["x-walacor-budget-remaining"] = str(budget_remaining)
    if budget_percent is not None:
        response.headers["x-walacor-budget-percent"] = str(budget_percent)
    if model_id:
        response.headers["x-walacor-model-id"] = str(model_id)
```

Update `build_governance_sse_event` in `src/gateway/pipeline/forwarder.py` (line ~22):

```python
def build_governance_sse_event(
    execution_id=None, attestation_id=None, chain_seq=None,
    policy_result=None, content_analysis=None, budget_remaining=None,
    budget_percent=None, model_id=None,
):
    """Build an SSE event with governance metadata, sent after data: [DONE]."""
    import json as _json
    payload = {}
    if execution_id:
        payload["execution_id"] = execution_id
    if attestation_id:
        payload["attestation_id"] = attestation_id
    if chain_seq is not None:
        payload["chain_seq"] = chain_seq
    if policy_result:
        payload["policy_result"] = policy_result
    if content_analysis:
        payload["content_analysis"] = content_analysis
    if budget_remaining is not None:
        payload["budget_remaining"] = budget_remaining
    if budget_percent is not None:
        payload["budget_percent"] = budget_percent
    if model_id:
        payload["model_id"] = model_id
    return f"event: governance\ndata: {_json.dumps(payload)}\n\n".encode()
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_governance_headers.py -v`
Expected: PASS

**Step 5: Wire new headers into non-streaming call site**

In `src/gateway/pipeline/orchestrator.py`, update the `_add_governance_headers` call site (line ~1553).

Compute a `_content_verdict` summary from `rp_decisions`:

```python
def _summarize_content_analysis(decisions: list) -> str:
    """Summarize content analysis decisions into a single header value."""
    if not decisions:
        return "clean"
    for d in decisions:
        if d.get("action") == "block":
            return "blocked"
    verdicts = [d.get("verdict", "") for d in decisions]
    if any("pii" in v for v in verdicts):
        return "pii_warn"
    if any("toxic" in v or "warn" in v for v in verdicts):
        return "toxicity_warn"
    return "clean"
```

Add this function before the `_add_governance_headers` call, then update the call site:

```python
    # Phase 23: governance response headers (non-streaming)
    _content_verdict = _summarize_content_analysis(rp_decisions)
    _add_governance_headers(
        http_response,
        execution_id=getattr(request.state, "walacor_execution_id", None),
        attestation_id=pre.att_id,
        chain_seq=getattr(request.state, "walacor_chain_seq", None),
        policy_result=pre.pr,
        content_analysis=_content_verdict,
        budget_remaining=pre.budget_remaining,
        budget_percent=_compute_budget_percent(pre.budget_remaining, settings),
        model_id=call.model_id,
    )
```

Add a helper to compute budget percent:

```python
def _compute_budget_percent(budget_remaining, settings) -> int | None:
    """Compute budget usage percent. Returns None if budget not configured."""
    if budget_remaining is None:
        return None
    if budget_remaining < 0:  # unlimited sentinel
        return None
    max_tokens = settings.token_budget_max_tokens
    if max_tokens <= 0:
        return None
    used = max_tokens - budget_remaining
    return min(100, max(0, round(used / max_tokens * 100)))
```

**Step 6: Wire new fields into streaming `governance_meta`**

In `src/gateway/pipeline/orchestrator.py`, update the streaming path (line ~1411):

```python
        governance_meta: dict = {
            "attestation_id": pre.att_id, "policy_result": pre.pr,
            "model_id": call.model_id,
            "budget_remaining": pre.budget_remaining,
            "budget_percent": _compute_budget_percent(pre.budget_remaining, settings),
        }
```

In `_after_stream_record` (line ~787), add content analysis to governance_meta after rp_decisions is computed:

```python
        if governance_meta is not None:
            governance_meta["execution_id"] = record.get("execution_id")
            governance_meta["chain_seq"] = record.get("sequence_number")
            governance_meta["content_analysis"] = _summarize_content_analysis(rp_decisions)
```

**Step 7: Run full test suite**

Run: `python -m pytest tests/unit/ -v`
Expected: All tests PASS

**Step 8: Commit**

```bash
git add src/gateway/pipeline/orchestrator.py src/gateway/pipeline/forwarder.py tests/unit/test_governance_headers.py
git commit -m "feat: enriched governance headers (content analysis, budget, model ID)"
```

---

### Task 3: OpenWebUI Identity Enrichment

Expand `resolve_identity_from_headers()` to capture OpenWebUI-specific headers as fallbacks.

**Files:**
- Modify: `src/gateway/auth/identity.py`
- Modify: `tests/unit/test_identity.py`

**Step 1: Write the failing tests**

Add to `tests/unit/test_identity.py`:

```python
    def test_openwebui_user_name_fallback(self):
        request = _make_request({"x-openwebui-user-name": "alice"})
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id == "alice"

    def test_openwebui_user_id_fallback(self):
        request = _make_request({"x-openwebui-user-id": "uuid-123"})
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id == "uuid-123"

    def test_generic_header_takes_precedence_over_openwebui(self):
        request = _make_request({
            "x-user-id": "generic-alice",
            "x-openwebui-user-name": "owui-alice",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.user_id == "generic-alice"

    def test_openwebui_email_fallback(self):
        request = _make_request({
            "x-openwebui-user-name": "alice",
            "x-openwebui-user-email": "alice@example.com",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.email == "alice@example.com"

    def test_generic_email_takes_precedence(self):
        request = _make_request({
            "x-user-id": "alice",
            "x-user-email": "generic@co.com",
            "x-openwebui-user-email": "owui@co.com",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.email == "generic@co.com"

    def test_openwebui_role_as_roles_list(self):
        request = _make_request({
            "x-openwebui-user-name": "bob",
            "x-openwebui-user-role": "admin",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.roles == ["admin"]

    def test_generic_roles_takes_precedence(self):
        request = _make_request({
            "x-user-id": "alice",
            "x-user-roles": "editor, viewer",
            "x-openwebui-user-role": "admin",
        })
        identity = resolve_identity_from_headers(request)
        assert identity is not None
        assert identity.roles == ["editor", "viewer"]
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_identity.py -v`
Expected: FAIL — OpenWebUI headers not recognized

**Step 3: Write minimal implementation**

Replace the body of `resolve_identity_from_headers` in `src/gateway/auth/identity.py`:

```python
def resolve_identity_from_headers(request: Request) -> CallerIdentity | None:
    """Extract caller identity from well-known request headers.

    Checks generic headers first (X-User-Id, X-User-Email, X-User-Roles, X-Team-Id),
    then falls back to OpenWebUI-specific headers (X-OpenWebUI-User-Name,
    X-OpenWebUI-User-Id, X-OpenWebUI-User-Email, X-OpenWebUI-User-Role).

    Returns None if no identity headers are present.
    """
    # User ID: generic → OpenWebUI-User-Name → OpenWebUI-User-Id
    user_id = (
        (request.headers.get("x-user-id") or "").strip()
        or (request.headers.get("x-openwebui-user-name") or "").strip()
        or (request.headers.get("x-openwebui-user-id") or "").strip()
    )
    if not user_id:
        return None

    # Email: generic → OpenWebUI
    email = (
        (request.headers.get("x-user-email") or "").strip()
        or (request.headers.get("x-openwebui-user-email") or "").strip()
    )

    # Roles: generic (comma-separated) → OpenWebUI (single role)
    roles_raw = (request.headers.get("x-user-roles") or "").strip()
    if roles_raw:
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    else:
        owui_role = (request.headers.get("x-openwebui-user-role") or "").strip()
        roles = [owui_role] if owui_role else []

    # Team: generic only (no OpenWebUI equivalent)
    team = (request.headers.get("x-team-id") or "").strip() or None

    return CallerIdentity(
        user_id=user_id,
        email=email,
        roles=roles,
        team=team,
        source="header_unverified",
    )
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_identity.py -v`
Expected: All 14 tests PASS (7 existing + 7 new)

**Step 5: Commit**

```bash
git add src/gateway/auth/identity.py tests/unit/test_identity.py
git commit -m "feat: OpenWebUI identity header fallbacks (user, email, role)"
```

---

### Task 4: caller_role in Policy Attestation Context

The policy engine evaluates rules against `att_ctx`. Currently it has `model_id`, `provider`, `status`, `verification_level`, `tenant_id`. We need to add `caller_role` so policies can do `caller_role equals admin`.

**Files:**
- Modify: `src/gateway/pipeline/orchestrator.py` (3 `att_ctx` construction sites + pre_policy_check)
- Test: `tests/unit/test_governance_headers.py` (add a test)

**Step 1: Write the failing test**

Add to `tests/unit/test_governance_headers.py`:

```python
class TestSummarizeContentAnalysis:
    def test_empty_decisions(self):
        from gateway.pipeline.orchestrator import _summarize_content_analysis
        assert _summarize_content_analysis([]) == "clean"

    def test_block_decision(self):
        from gateway.pipeline.orchestrator import _summarize_content_analysis
        assert _summarize_content_analysis([{"action": "block", "verdict": "toxic"}]) == "blocked"

    def test_pii_warn(self):
        from gateway.pipeline.orchestrator import _summarize_content_analysis
        assert _summarize_content_analysis([{"action": "warn", "verdict": "pii_detected"}]) == "pii_warn"

    def test_toxicity_warn(self):
        from gateway.pipeline.orchestrator import _summarize_content_analysis
        assert _summarize_content_analysis([{"action": "warn", "verdict": "toxic"}]) == "toxicity_warn"

    def test_pass_decisions(self):
        from gateway.pipeline.orchestrator import _summarize_content_analysis
        assert _summarize_content_analysis([{"action": "pass", "verdict": "pass"}]) == "clean"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_governance_headers.py::TestSummarizeContentAnalysis -v`
Expected: FAIL — `_summarize_content_analysis` not defined (not yet added)

**Step 3: Write minimal implementation**

The `_summarize_content_analysis` and `_compute_budget_percent` functions were already defined in Task 2 Step 5.

For `caller_role` in `att_ctx`, modify the 3 places in `orchestrator.py` where `att_ctx` is constructed inside `_attestation_check` (lines ~841, ~867, ~884).

In each `att_ctx` dict, add the caller_role by reading from the request. Since `_attestation_check` receives `request`, we can access `request.state.caller_identity`:

After each `att_ctx` dict is built, add:

```python
    # Inject caller_role for policy evaluation
    caller_identity = getattr(request.state, "caller_identity", None)
    if caller_identity is not None and caller_identity.roles:
        att_ctx["caller_role"] = caller_identity.roles[0]  # primary role
```

This goes in 3 places:
1. After line ~841 (audit_only att_ctx)
2. After line ~867 (auto-attest att_ctx)
3. After line ~889 (normal attestation att_ctx)

A cleaner approach: add it once right before the `return` at each exit point. Since all 3 `att_ctx` constructions are inside `_attestation_check` and it always returns `att_ctx`, inject it at the end as a helper:

Add a private helper right after `_add_governance_headers`:

```python
def _inject_caller_role(att_ctx: dict, request: Request) -> None:
    """Inject caller_role into attestation context for policy evaluation."""
    caller_identity = getattr(request.state, "caller_identity", None)
    if caller_identity is not None and caller_identity.roles:
        att_ctx["caller_role"] = caller_identity.roles[0]
```

Then call `_inject_caller_role(att_ctx, request)` before each `return` in `_attestation_check`.

**Step 4: Run tests**

Run: `python -m pytest tests/unit/test_governance_headers.py -v`
Expected: PASS

Run: `python -m pytest tests/unit/ -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/gateway/pipeline/orchestrator.py tests/unit/test_governance_headers.py
git commit -m "feat: caller_role in policy context + content analysis summarizer"
```

---

### Task 5: OpenWebUI Status Endpoint

New `GET /v1/openwebui/status` endpoint returning banners, budget, and model status.

**Files:**
- Create: `src/gateway/openwebui/__init__.py`
- Create: `src/gateway/openwebui/status_api.py`
- Modify: `src/gateway/main.py` (add route + import)
- Modify: `src/gateway/middleware/completeness.py` (skip path)
- Test: `tests/unit/test_openwebui_status.py`

**Step 1: Write the failing test**

Create `tests/unit/test_openwebui_status.py`:

```python
"""Tests for /v1/openwebui/status endpoint."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from starlette.requests import Request
from starlette.testclient import TestClient

from gateway.openwebui.status_api import openwebui_status


def _make_request() -> Request:
    scope = {"type": "http", "method": "GET", "path": "/v1/openwebui/status", "headers": []}
    return Request(scope)


class TestOpenWebUIStatus:
    @pytest.mark.anyio
    async def test_returns_banners_and_models(self):
        mock_ctx = MagicMock()
        mock_ctx.control_store = MagicMock()
        mock_ctx.control_store.list_attestations.return_value = [
            {"model_id": "qwen3:4b", "status": "active"},
            {"model_id": "gpt-4o", "status": "revoked"},
        ]
        mock_ctx.budget_tracker = None
        mock_ctx.wal_writer = None

        with patch("gateway.openwebui.status_api.get_pipeline_context", return_value=mock_ctx), \
             patch("gateway.openwebui.status_api.get_settings") as mock_settings:
            mock_settings.return_value.gateway_tenant_id = "test-tenant"
            mock_settings.return_value.token_budget_enabled = False
            mock_settings.return_value.disk_degraded_threshold = 0.8

            resp = await openwebui_status(_make_request())
            import json
            body = json.loads(resp.body)

            assert "banners" in body
            assert "models_status" in body
            assert "qwen3:4b" in body["models_status"]["active"]
            assert "gpt-4o" in body["models_status"]["revoked"]
            # Revoked model should generate a banner
            assert any("gpt-4o" in b["text"] for b in body["banners"])

    @pytest.mark.anyio
    async def test_budget_info_when_enabled(self):
        mock_ctx = MagicMock()
        mock_ctx.control_store = MagicMock()
        mock_ctx.control_store.list_attestations.return_value = []
        mock_ctx.budget_tracker = AsyncMock()
        mock_ctx.budget_tracker.get_snapshot = AsyncMock(return_value={
            "period": "monthly",
            "tokens_used": 9000,
            "max_tokens": 10000,
            "percent_used": 90.0,
        })
        mock_ctx.wal_writer = None

        with patch("gateway.openwebui.status_api.get_pipeline_context", return_value=mock_ctx), \
             patch("gateway.openwebui.status_api.get_settings") as mock_settings:
            mock_settings.return_value.gateway_tenant_id = "test-tenant"
            mock_settings.return_value.token_budget_enabled = True
            mock_settings.return_value.token_budget_max_tokens = 10000
            mock_settings.return_value.disk_degraded_threshold = 0.8

            resp = await openwebui_status(_make_request())
            import json
            body = json.loads(resp.body)

            assert body["budget"]["percent_used"] == 90.0
            assert body["budget"]["tokens_remaining"] == 1000
            # 90% should trigger a warning banner
            assert any("90%" in b.get("text", "") or "budget" in b.get("text", "").lower() for b in body["banners"])

    @pytest.mark.anyio
    async def test_no_control_store(self):
        mock_ctx = MagicMock()
        mock_ctx.control_store = None
        mock_ctx.budget_tracker = None
        mock_ctx.wal_writer = None

        with patch("gateway.openwebui.status_api.get_pipeline_context", return_value=mock_ctx), \
             patch("gateway.openwebui.status_api.get_settings") as mock_settings:
            mock_settings.return_value.gateway_tenant_id = "test-tenant"
            mock_settings.return_value.token_budget_enabled = False
            mock_settings.return_value.disk_degraded_threshold = 0.8

            resp = await openwebui_status(_make_request())
            import json
            body = json.loads(resp.body)
            assert body["models_status"]["active"] == []
            assert body["models_status"]["revoked"] == []
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_openwebui_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.openwebui'`

**Step 3: Create the package and implementation**

Create `src/gateway/openwebui/__init__.py`:

```python
```

Create `src/gateway/openwebui/status_api.py`:

```python
"""OpenWebUI integration status endpoint — banners, budget, model health."""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)


async def openwebui_status(request: Request) -> JSONResponse:
    """GET /v1/openwebui/status — returns banners, budget, and model status for OpenWebUI Pipeline consumption."""
    ctx = get_pipeline_context()
    settings = get_settings()
    banners: list[dict] = []

    # ── Model status ──
    active_models: list[str] = []
    revoked_models: list[str] = []
    if ctx.control_store:
        try:
            attestations = ctx.control_store.list_attestations(settings.gateway_tenant_id)
            for att in attestations:
                model_id = att.get("model_id", "")
                status = att.get("status", "active")
                if status == "active":
                    active_models.append(model_id)
                else:
                    revoked_models.append(model_id)
                    banners.append({
                        "type": "error",
                        "text": f"Model {model_id} attestation {status} — model unavailable",
                    })
        except Exception as e:
            logger.warning("openwebui_status: failed to list attestations: %s", e)

    # ── Budget ──
    budget_info: dict | None = None
    if ctx.budget_tracker and settings.token_budget_enabled:
        try:
            snapshot = await ctx.budget_tracker.get_snapshot(settings.gateway_tenant_id)
            if snapshot and snapshot.get("max_tokens", 0) > 0:
                pct = snapshot.get("percent_used", 0.0)
                remaining = snapshot["max_tokens"] - snapshot.get("tokens_used", 0)
                budget_info = {
                    "percent_used": pct,
                    "tokens_remaining": max(0, remaining),
                    "tokens_used": snapshot.get("tokens_used", 0),
                    "max_tokens": snapshot["max_tokens"],
                    "period": snapshot.get("period", "monthly"),
                }
                if pct >= 100:
                    banners.append({"type": "error", "text": f"Token budget exhausted — {snapshot['max_tokens']} tokens used this {snapshot.get('period', 'month')}"})
                elif pct >= 90:
                    banners.append({"type": "warning", "text": f"Token budget at {pct:.0f}% — {remaining:,} tokens remaining"})
                elif pct >= 70:
                    banners.append({"type": "info", "text": f"Token budget at {pct:.0f}% — {remaining:,} tokens remaining"})
        except Exception as e:
            logger.warning("openwebui_status: failed to get budget snapshot: %s", e)

    # ── WAL health ──
    if ctx.wal_writer:
        try:
            disk_bytes = ctx.wal_writer.disk_usage_bytes()
            max_bytes = int(settings.wal_max_size_gb * (1024 ** 3))
            if max_bytes > 0 and disk_bytes / max_bytes >= settings.disk_degraded_threshold:
                banners.append({"type": "warning", "text": "Gateway storage nearing capacity — audit log may be truncated"})
        except Exception:
            pass

    return JSONResponse({
        "banners": banners,
        "budget": budget_info,
        "models_status": {
            "active": active_models,
            "revoked": revoked_models,
        },
    })
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_openwebui_status.py -v`
Expected: All 3 tests PASS

**Step 5: Wire route into main.py**

In `src/gateway/main.py`:

Add import (near other control imports):
```python
from gateway.openwebui.status_api import openwebui_status
```

Add route (after the control plane routes, before sync-contract endpoints):
```python
        # OpenWebUI integration
        Route("/v1/openwebui/status", openwebui_status, methods=["GET"]),
```

In `src/gateway/middleware/completeness.py`, add `/v1/openwebui` to the skip path check (line ~27):
```python
    if request.url.path in ("/", "/health", "/metrics", "/v1/models") or request.url.path.startswith(("/lineage", "/v1/lineage", "/v1/control", "/v1/attestation-proofs", "/v1/policies", "/v1/compliance", "/v1/openwebui")):
```

**Step 6: Run full test suite**

Run: `python -m pytest tests/unit/ -v`
Expected: All tests PASS

**Step 7: Commit**

```bash
git add src/gateway/openwebui/__init__.py src/gateway/openwebui/status_api.py \
        src/gateway/main.py src/gateway/middleware/completeness.py \
        tests/unit/test_openwebui_status.py
git commit -m "feat: /v1/openwebui/status endpoint (banners, budget, model health)"
```

---

### Task 6: OpenWebUI Pipeline Plugin

Create the reference Pipeline plugin that OpenWebUI users can install. This is a standalone Python file — not part of the Gateway's import tree.

**Files:**
- Create: `plugins/openwebui/governance_pipeline.py`

**Step 1: Create the plugins directory**

```bash
mkdir -p plugins/openwebui
```

**Step 2: Write the Pipeline plugin**

Create `plugins/openwebui/governance_pipeline.py`:

```python
"""
Walacor Gateway Governance Pipeline for OpenWebUI.

Install: Copy this file into your OpenWebUI Pipelines server.
Requires: Gateway running at GATEWAY_URL with API key.

This outlet filter appends governance metadata (chain position, policy result,
content analysis verdict, budget status) to each assistant message.
The inlet filter polls /v1/openwebui/status for operational alerts.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

# ── Configuration ──────────────────────────────────────────────
GATEWAY_URL = os.environ.get("WALACOR_GATEWAY_URL", "http://gateway:8000")
GATEWAY_API_KEY = os.environ.get("WALACOR_GATEWAY_API_KEY", "")
STATUS_POLL_INTERVAL = 60  # seconds between status polls
STATUS_CACHE: dict[str, Any] = {"data": None, "fetched_at": 0}


class Pipeline:
    """OpenWebUI Pipeline: Walacor Governance Visibility."""

    class Valves:
        """Pipeline configuration (editable in OpenWebUI admin)."""
        gateway_url: str = GATEWAY_URL
        gateway_api_key: str = GATEWAY_API_KEY
        show_footer: bool = True
        show_alerts: bool = True

    def __init__(self):
        self.name = "Walacor Governance"
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: dict | None = None) -> dict:
        """Pre-request hook: check for operational alerts."""
        if not self.valves.show_alerts:
            return body

        status = self._get_cached_status()
        if status and status.get("banners"):
            # Prepend alert as a system message
            alert_text = " | ".join(
                f"{'⚠️' if b['type'] == 'warning' else '🔴' if b['type'] == 'error' else 'ℹ️'} {b['text']}"
                for b in status["banners"]
            )
            messages = body.get("messages", [])
            if messages and messages[0].get("role") != "system":
                messages.insert(0, {
                    "role": "system",
                    "content": f"[Gateway Alert] {alert_text}",
                })
            body["messages"] = messages

        return body

    async def outlet(self, body: dict, __user__: dict | None = None) -> dict:
        """Post-response hook: append governance footer to assistant message."""
        if not self.valves.show_footer:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        last_msg = messages[-1]
        if last_msg.get("role") != "assistant":
            return body

        # Read governance metadata from response info (headers stored by OpenWebUI)
        info = last_msg.get("info", {})
        headers = info.get("headers", {})

        execution_id = headers.get("x-walacor-execution-id", "")
        attestation_id = headers.get("x-walacor-attestation-id", "")
        chain_seq = headers.get("x-walacor-chain-seq", "")
        policy_result = headers.get("x-walacor-policy-result", "")
        content_analysis = headers.get("x-walacor-content-analysis", "")
        budget_remaining = headers.get("x-walacor-budget-remaining", "")
        budget_percent = headers.get("x-walacor-budget-percent", "")
        model_id = headers.get("x-walacor-model-id", "")

        if not execution_id and not chain_seq:
            return body  # No governance data available

        # Build footer
        parts = []
        if chain_seq:
            parts.append(f"🔒 Chain #{chain_seq}")
        if policy_result:
            icon = "✅" if policy_result == "pass" else "❌"
            parts.append(f"{icon} Policy: {policy_result}")
        if content_analysis:
            icon = "🛡️" if content_analysis == "clean" else "⚠️"
            parts.append(f"{icon} {content_analysis.replace('_', ' ').title()}")
        if budget_percent:
            remaining_str = f"{int(budget_remaining):,}" if budget_remaining else "?"
            parts.append(f"💰 {remaining_str} tokens remaining ({budget_percent}% used)")

        footer_line1 = "  ".join(parts)
        footer_parts = [f"\n\n---\n**Walacor Governance** {footer_line1}"]

        details = []
        if execution_id:
            details.append(f"Execution: `{execution_id[:12]}...`")
        if model_id:
            att_label = "attested" if attestation_id and "self-attested" not in attestation_id else "self-attested"
            details.append(f"Model: {model_id} ({att_label})")
        if details:
            footer_parts.append(" | ".join(details))

        footer = "\n".join(footer_parts)
        last_msg["content"] = last_msg.get("content", "") + footer

        return body

    def _get_cached_status(self) -> dict | None:
        """Fetch /v1/openwebui/status with caching."""
        now = time.time()
        if STATUS_CACHE["data"] is not None and now - STATUS_CACHE["fetched_at"] < STATUS_POLL_INTERVAL:
            return STATUS_CACHE["data"]
        try:
            url = f"{self.valves.gateway_url.rstrip('/')}/v1/openwebui/status"
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "X-API-Key": self.valves.gateway_api_key,
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                STATUS_CACHE["data"] = data
                STATUS_CACHE["fetched_at"] = now
                return data
        except Exception:
            return STATUS_CACHE.get("data")
```

**Step 3: Create a README for the plugin**

Create `plugins/openwebui/README.md`:

```markdown
# Walacor Governance Pipeline for OpenWebUI

Surfaces Gateway governance metadata (chain verification, policy results,
content analysis, budget status) directly in the OpenWebUI chat interface.

## Install

1. Copy `governance_pipeline.py` into your OpenWebUI Pipelines server
2. Set environment variables:
   - `WALACOR_GATEWAY_URL` — Gateway base URL (default: `http://gateway:8000`)
   - `WALACOR_GATEWAY_API_KEY` — Gateway API key
3. Enable the pipeline in OpenWebUI admin panel

## What You'll See

After each assistant message:

```
─── Walacor Governance ─────────────────────────
🔒 Chain #4  ✅ Policy: pass  🛡️ Clean  💰 8,200 tokens remaining (18% used)
Execution: abc123ef... | Model: qwen3:4b (attested)
```

Operational alerts appear as system messages when budget thresholds are
crossed or model attestations are revoked.

## Configuration

In OpenWebUI admin, the pipeline exposes these valves:
- `gateway_url` — Gateway endpoint
- `gateway_api_key` — API key for status endpoint
- `show_footer` — Enable/disable governance footer (default: true)
- `show_alerts` — Enable/disable operational alerts (default: true)
```

**Step 4: Commit**

```bash
git add plugins/openwebui/governance_pipeline.py plugins/openwebui/README.md
git commit -m "feat: OpenWebUI Pipeline plugin for governance visibility"
```

---

### Task 7: Update Documentation

Update the quickstart guide and .env.example to reflect the new integration features.

**Files:**
- Modify: `docs/OPENWEBUI-QUICKSTART.md`
- Modify: `.env.example`

**Step 1: Add Pipeline section to quickstart**

Append to `docs/OPENWEBUI-QUICKSTART.md`:

```markdown

## Governance Visibility (Optional)

Install the Walacor Governance Pipeline to see audit metadata in chat:

1. Copy `plugins/openwebui/governance_pipeline.py` to your Pipelines server
2. Set `WALACOR_GATEWAY_URL` and `WALACOR_GATEWAY_API_KEY` environment variables
3. Enable the pipeline in **Admin > Pipelines**

Each response will show chain position, policy result, content analysis verdict, and budget status.

## Enterprise RBAC

OpenWebUI forwards user roles to Gateway via `X-OpenWebUI-User-Role` header. Create policies in the Gateway control plane to restrict models by role:

```bash
# Allow only admins to use expensive models
curl -X POST http://localhost:8002/v1/control/policies \
  -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "admin-only-expensive-models",
    "rules": [
      {"field": "model_id", "op": "in", "value": ["gpt-4o", "claude-sonnet-4-20250514"]},
      {"field": "caller_role", "op": "equals", "value": "admin"}
    ],
    "action": "allow"
  }'
```
```

**Step 2: Commit**

```bash
git add docs/OPENWEBUI-QUICKSTART.md
git commit -m "docs: Pipeline plugin install + RBAC setup in quickstart"
```

---

### Task 8: Run Full Test Suite + Verify

**Step 1: Run all tests**

Run: `python -m pytest tests/unit/ -v --tb=short`
Expected: All tests PASS (existing + new)

**Step 2: Verify CORS headers**

```bash
curl -s -I -X OPTIONS http://localhost:8000/v1/chat/completions \
  -H "Origin: http://localhost:3000" | grep -i access-control
```

Expected: `Access-Control-Expose-Headers` includes `x-walacor-*` headers.

**Step 3: Verify OpenWebUI status endpoint**

```bash
curl -s http://localhost:8000/v1/openwebui/status -H "X-API-Key: test-key" | python -m json.tool
```

Expected: JSON with `banners`, `budget`, `models_status` fields.

**Step 4: Verify governance headers on a real request**

```bash
curl -s -D- http://localhost:8000/v1/chat/completions \
  -H "X-API-Key: test-key" -H "Content-Type: application/json" \
  -H "X-OpenWebUI-User-Name: alice" -H "X-OpenWebUI-User-Role: admin" \
  -d '{"model": "qwen3:4b", "messages": [{"role": "user", "content": "Hi"}]}' \
  2>&1 | grep -i x-walacor
```

Expected: Headers including `x-walacor-content-analysis`, `x-walacor-model-id`, etc.

**Step 5: Final commit**

```bash
git add -A
git commit -m "chore: OpenWebUI deep integration complete (headers + identity + status + pipeline)"
```
