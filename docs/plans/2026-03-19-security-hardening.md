# Security Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all 27 code vulnerabilities and implement 12 industry best practices to make the Walacor Gateway production-secure.

**Architecture:** Defense-in-depth across 7 layers — authentication, input validation, output sanitization, SSRF protection, cryptographic hardening, resource limits, and supply chain integrity. Each fix is isolated so failures don't cascade.

**Tech Stack:** Python 3.12, Starlette/ASGI, SQLite, httpx, pyjwt, React (Vite build)

---

## Phase 1: Critical — Authentication and Access Control (Tasks 1-5)

### Task 1: Add Authentication to Lineage Endpoints (C1)

**Severity:** CRITICAL
**Files:**
- Modify: `src/gateway/main.py:152-153`
- Test: `tests/unit/test_lineage_auth.py` (create)

**Step 1: Write the failing test**

```python
# tests/unit/test_lineage_auth.py
import pytest
from starlette.testclient import TestClient

def test_lineage_sessions_requires_auth(monkeypatch):
    monkeypatch.setenv("WALACOR_GATEWAY_API_KEYS", "test-key-123")
    from gateway.main import app
    client = TestClient(app)
    r = client.get("/v1/lineage/sessions")
    assert r.status_code == 401

def test_lineage_sessions_with_key(monkeypatch):
    monkeypatch.setenv("WALACOR_GATEWAY_API_KEYS", "test-key-123")
    from gateway.main import app
    client = TestClient(app)
    r = client.get("/v1/lineage/sessions", headers={"X-API-Key": "test-key-123"})
    assert r.status_code in (200, 503)

def test_lineage_dashboard_static_no_auth():
    """Static files at /lineage/ should still work without auth."""
    from gateway.main import app
    client = TestClient(app)
    r = client.get("/lineage/")
    assert r.status_code == 200
```

**Step 2: Implement the fix**

In `src/gateway/main.py`, change the auth skip logic at line 152. Remove `/v1/lineage` from the skip list. Keep `/lineage/` (with trailing slash) for static dashboard files:

```python
# BEFORE:
request.url.path.startswith(("/lineage", "/v1/lineage", "/v1/control", ...))

# AFTER — remove /v1/lineage from the skip:
request.url.path.startswith(("/lineage/", "/v1/control", "/v1/attestation-proofs", "/v1/policies", "/v1/compliance", "/v1/openwebui", "/v1/attachments"))
```

**Step 3: Run test, commit**

```bash
python3.12 -m pytest tests/unit/test_lineage_auth.py -v
git commit -m "security: require auth for lineage API endpoints (C1)"
```

---

### Task 2: Require API Keys When Control Plane Enabled (C2)

**Severity:** CRITICAL
**Files:**
- Modify: `src/gateway/main.py:919-923`

**Step 1: Implement auto-key generation**

Replace the warning-only path with auto-generation:

```python
if settings.control_plane_enabled and not settings.api_keys_list:
    import secrets
    auto_key = f"wgk-{secrets.token_urlsafe(32)}"
    settings.api_keys_list = [auto_key]
    logger.warning(
        "SECURITY: Control plane enabled without API keys. "
        "Auto-generated key: %s — set WALACOR_GATEWAY_API_KEYS to use your own.",
        auto_key,
    )
```

**Step 2: Commit**

```bash
git commit -m "security: auto-generate API key when control plane has no keys (C2)"
```

---

### Task 3: Prevent Stored XSS in Dashboard (C3)

**Severity:** CRITICAL
**Files:**
- Modify: `src/gateway/main.py` (add CSP header)
- Audit: `src/gateway/lineage/dashboard/src/` for unsafe rendering
- Rebuild: `src/gateway/lineage/static/`

**Step 1: Audit for unsafe rendering**

```bash
grep -rn "dangerouslySetInnerHTML" src/gateway/lineage/dashboard/src/
```

The React JSX auto-escapes `{variable}` rendering. The main risk is any use of `dangerouslySetInnerHTML` or raw DOM manipulation.

**Step 2: Add Content-Security-Policy header**

In `src/gateway/main.py`, add CSP to the lineage static file mount or as a global response header:

```python
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
```

**Step 3: Rebuild and commit**

```bash
cd src/gateway/lineage/dashboard && npm run build
git commit -m "security: CSP header + XSS audit for dashboard (C3)"
```

---

### Task 4: Validate MCP Command Allowlist (C4)

**Severity:** CRITICAL
**Files:**
- Modify: `src/gateway/mcp/client.py:79-85`
- Modify: `src/gateway/config.py`
- Test: `tests/unit/test_mcp_security.py` (create)

**Step 1: Add config**

```python
# config.py
mcp_allowed_commands: str = Field(default="", description="Comma-separated allowed MCP stdio commands (empty = defaults: python,python3,node,npx,uvx)")
```

**Step 2: Add validation in client.py**

```python
_DEFAULT_ALLOWED = {"python", "python3", "python3.12", "node", "npx", "uvx"}

def _validate_mcp_command(config, settings):
    if config.transport != "stdio":
        return
    custom = settings.mcp_allowed_commands
    allowed = {c.strip() for c in custom.split(",") if c.strip()} if custom else _DEFAULT_ALLOWED
    cmd_base = Path(config.command).name
    if cmd_base not in allowed:
        raise ValueError(f"MCP command '{cmd_base}' not in allowed list: {allowed}")
```

**Step 3: Strip sensitive env vars from MCP subprocess**

```python
_SENSITIVE_PATTERNS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL")

def _safe_env(config_env):
    base = dict(config_env or os.environ)
    return {k: v for k, v in base.items()
            if not any(p in k.upper() for p in _SENSITIVE_PATTERNS)}
```

**Step 4: Commit**

```bash
git commit -m "security: MCP command allowlist + env sanitization (C4)"
```

---

### Task 5: Restrict CORS Origins (H1)

**Severity:** HIGH
**Files:**
- Modify: `src/gateway/main.py:193`
- Modify: `src/gateway/config.py`

**Step 1: Add config**

```python
# config.py
cors_allowed_origins: str = Field(default="", description="Comma-separated CORS origins (empty = same-origin only)")
```

**Step 2: Replace wildcard CORS**

```python
# main.py — replace _CORS_HEADERS with dynamic function:
def _cors_origin(request):
    settings = get_settings()
    origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
    req_origin = request.headers.get("origin", "")
    if origins and req_origin in origins:
        return req_origin
    return None  # No CORS header = same-origin only
```

**Step 3: Commit**

```bash
git commit -m "security: restrict CORS to configured origins only (H1)"
```

---

## Phase 2: High — Input Validation and Error Handling (Tasks 6-11)

### Task 6: Request Body Size Limit (H5)

**Files:** `src/gateway/main.py`, `src/gateway/config.py`

```python
# config.py
max_request_body_mb: float = Field(default=50.0, description="Max request body size in MB")

# main.py — add before routing:
async def _body_limit_check(request, call_next):
    cl = request.headers.get("content-length")
    max_b = int(get_settings().max_request_body_mb * 1024 * 1024)
    if cl and int(cl) > max_b:
        return JSONResponse({"error": "Request body too large"}, status_code=413)
    return await call_next(request)
```

Commit: `"security: request body size limit (H5)"`

---

### Task 7: Generic Error Responses (H4)

**Files:** `src/gateway/control/api.py` (22 instances)

Replace all `return JSONResponse({"error": str(e)}, status_code=500)` with:

```python
logger.error("Handler error", exc_info=True)
return JSONResponse({"error": "Internal server error"}, status_code=500)
```

Keep `str(e)` only for 400 validation errors where the message is user-facing (e.g., "Missing field: model_id").

Commit: `"security: generic 500 responses, no internal leaks (H4)"`

---

### Task 8: API Key Constant-Time Check (L19)

**Files:** `src/gateway/auth/api_key.py:23`

```python
# BEFORE:
return any(hmac.compare_digest(key, valid_key) for valid_key in api_keys_list)

# AFTER:
result = False
for valid_key in api_keys_list:
    if hmac.compare_digest(key, valid_key):
        result = True
return result
```

Commit: `"security: constant-time key check without short-circuit (L19)"`

---

### Task 9: Validate Lineage Query Params (L20)

**Files:** `src/gateway/lineage/api.py:33-34, 112-113`

```python
def _safe_int(val, default):
    try: return int(val)
    except (ValueError, TypeError): return default
```

Replace all `int(request.query_params.get(...))` with `_safe_int(...)`.

Commit: `"security: safe int parsing for lineage params (L20)"`

---

### Task 10: Header Identity Spoofing Protection (H2)

**Files:** `src/gateway/pipeline/orchestrator.py:317`

```python
# Only use verified identity for policy decisions:
if caller_identity and caller_identity.source in ("jwt",):
    att_ctx["caller_role"] = caller_identity.roles[0] if caller_identity.roles else None
# Unverified headers → audit only, not policy
```

Commit: `"security: unverified identity excluded from policy evaluation (H2)"`

---

### Task 11: Custom Class Loading Restriction (H6)

**Files:** `src/gateway/adaptive/__init__.py:11-15`

```python
_ALLOWED_PREFIXES = ("gateway.", "walacor.")

def load_custom_class(dotted_path: str) -> type:
    if not any(dotted_path.startswith(p) for p in _ALLOWED_PREFIXES):
        raise ValueError(f"Custom class '{dotted_path}' must be in {_ALLOWED_PREFIXES}")
    module_path, class_name = dotted_path.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), class_name)
```

Commit: `"security: restrict importlib to gateway/walacor packages (H6)"`

---

## Phase 3: Medium — SSRF, ReDoS, Resource Limits (Tasks 12-19)

### Task 12: SSRF URL Validator (M1)

**Files:** Create `src/gateway/security/url_validator.py`

```python
import ipaddress, socket
from urllib.parse import urlparse

_BLOCKED = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

def validate_outbound_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("No hostname")
    for _, _, _, _, addr in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(addr[0])
        for net in _BLOCKED:
            if ip in net:
                raise ValueError(f"Blocked: resolves to private IP {ip}")
    return url
```

Wire into MCP HTTP transport and web_search.py fetch calls.

Commit: `"security: SSRF protection blocks private IP access (M1)"`

---

### Task 13: Path Traversal Fix (M4)

**Files:** `src/gateway/control/api.py:663`

```python
path = (_TEMPLATES_DIR / f"{template_name}.json").resolve()
if not str(path).startswith(str(_TEMPLATES_DIR.resolve())):
    return JSONResponse({"error": "Invalid template name"}, status_code=400)
```

Commit: `"security: block path traversal in control templates (M4)"`

---

### Task 14: ReDoS Protection (M3)

**Files:** `src/gateway/content/toxicity_detector.py:26-27`

```python
# BEFORE:
re.compile("|".join(terms), re.IGNORECASE)

# AFTER:
re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
```

Commit: `"security: escape regex deny terms to prevent ReDoS (M3)"`

---

### Task 15: Bound Rate Limiter Memory (M5)

**Files:** `src/gateway/pipeline/rate_limiter.py:12`

```python
from collections import OrderedDict

class SlidingWindowRateLimiter:
    _MAX_KEYS = 10_000

    def __init__(self):
        self._windows: OrderedDict[str, list[float]] = OrderedDict()

    def _evict(self):
        while len(self._windows) > self._MAX_KEYS:
            self._windows.popitem(last=False)
```

Call `self._evict()` at the start of `check()`.

Commit: `"security: bound rate limiter memory with LRU eviction (M5)"`

---

### Task 16: Tool Output Size Limits (M6)

**Files:** `src/gateway/pipeline/orchestrator.py`, `src/gateway/config.py`

```python
# config.py
tool_max_output_bytes: int = Field(default=1_048_576, description="Max tool output (1MB default)")

# orchestrator.py in _execute_one_tool, after execution:
if len(result.content) > settings.tool_max_output_bytes:
    result = ToolResult(content=result.content[:settings.tool_max_output_bytes] + "\n[TRUNCATED]",
                        is_error=result.is_error, duration_ms=result.duration_ms, sources=result.sources)
```

Commit: `"security: enforce tool output size limits (M6)"`

---

### Task 17: TLS Warning for Sync Client (M7)

**Files:** `src/gateway/sync/sync_client.py`

```python
if self._url.startswith("http://") and "localhost" not in self._url:
    logger.warning("SECURITY: Control plane URL uses HTTP — API key transmitted in cleartext")
```

Commit: `"security: warn on HTTP sync client (M7)"`

---

### Task 18: Validate MCP Config File Paths (L23)

**Files:** `src/gateway/mcp/registry.py:148-153`

```python
path = Path(raw).resolve()
if not path.suffix == ".json":
    raise ValueError(f"MCP config must be .json: {path}")
```

Commit: `"security: validate MCP config file extensions (L23)"`

---

### Task 19: Provider URL Validation (M1 extended)

**Files:** `src/gateway/config.py`

Add pydantic validator for provider URLs — reject non-http(s) schemes.

Commit: `"security: validate provider URL schemes (M1b)"`

---

## Phase 4: Cryptographic and Storage Hardening (Tasks 20-23)

### Task 20: WAL File Permissions 0600

**Files:** `src/gateway/wal/writer.py`

```python
import os, stat
# After DB creation:
os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)
```

Also add `PRAGMA secure_delete=ON`.

Commit: `"security: WAL file permissions 0600 + secure_delete"`

---

### Task 21: WAL synchronous=FULL

**Files:** `src/gateway/wal/writer.py:100`

```python
conn.execute("PRAGMA synchronous=FULL")
```

Commit: `"security: WAL synchronous=FULL for audit durability"`

---

### Task 22: JWT Startup Validation Warnings

**Files:** `src/gateway/main.py`

Warn at startup when:
- `auth_mode=jwt` but `jwt_issuer` not set
- `auth_mode=jwt` but `jwt_audience` not set
- `jwt_secret` shorter than 32 chars

Commit: `"security: warn on weak JWT config at startup"`

---

### Task 23: OPA Fail-Closed Option

**Files:** `src/gateway/pipeline/opa_evaluator.py:44-49`, `config.py`

```python
# config.py
opa_fail_closed: bool = Field(default=False, description="Block when OPA unavailable")
```

Commit: `"security: add OPA fail-closed option"`

---

## Phase 5: Industry Best Practices (Tasks 24-30)

### Task 24: Indirect Prompt Injection Scanning on Tool Output

Scan tool output with prompt guard before feeding back to model. Block if injection score exceeds threshold.

Commit: `"security: scan tool output for prompt injection (OWASP LLM01)"`

---

### Task 25: Enable Rate Limiting by Default

Change `rate_limit_enabled` default to `True`, `rate_limit_rpm` default to `120`.

Commit: `"security: enable rate limiting by default (OWASP LLM10)"`

---

### Task 26: Pin Dependencies

Generate `requirements.lock` with `pip-compile --generate-hashes`. Use in Dockerfile.

Commit: `"security: pin dependencies with hashes"`

---

### Task 27: Per-IP Pre-Auth Rate Limiting

Create `src/gateway/middleware/ip_rate_limiter.py` with bounded OrderedDict. Apply before auth middleware.

Commit: `"security: per-IP rate limiting (LLMjacking defense)"`

---

### Task 28: Total Tool Loop Timeout

Add `tool_loop_total_timeout_ms` config (default 120s). Break loop if wall-clock exceeded.

Commit: `"security: total tool loop timeout (MCP best practice)"`

---

### Task 29: Security Response Headers

Add `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` to all responses.

Commit: `"security: standard security response headers"`

---

### Task 30: Sensitive Config Redaction in Logs

Audit all logger calls. Ensure provider keys, JWT secrets, Redis URLs never logged above DEBUG.

Commit: `"security: redact sensitive values in logs"`

---

## Phase 6: Verification (Tasks 31-32)

### Task 31: Security Test Suite (Tier 8)

Create `tests/production/tier8_security_deep.py` covering:
- Auth on all lineage endpoints
- CORS rejects unknown origins
- 413 on oversized body
- Path traversal blocked
- Generic error responses
- Rate limiting triggers 429
- MCP command injection blocked
- SSRF blocked

Commit: `"security: Tier 8 security deep test suite"`

---

### Task 32: Dependency Audit

```bash
pip-audit --format json
```

Fix any CVEs. Add to CI.

Commit: `"security: dependency audit clean"`

---

## Summary

| Phase | Tasks | Coverage | Effort |
|-------|-------|----------|--------|
| 1: Auth/Access | 1-5 | 4 CRITICAL + 1 HIGH | 4h |
| 2: Input/Errors | 6-11 | 4 HIGH + 2 LOW | 3h |
| 3: SSRF/Resources | 12-19 | 6 MEDIUM + 2 LOW | 4h |
| 4: Crypto/Storage | 20-23 | 2 LOW + 2 Industry | 2h |
| 5: Best Practices | 24-30 | 7 Industry | 5h |
| 6: Verification | 31-32 | All | 3h |
| **Total** | **32 tasks** | **27 vulns + 12 practices** | **~21h** |
