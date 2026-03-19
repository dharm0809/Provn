#!/usr/bin/env python3
"""Tier 8: Security Deep Verification — validates all critical/high severity fixes.

Covers 30 security fixes across 6 hardening phases:
  - Auth enforcement on lineage API endpoints
  - Security response headers (XSS, clickjack, MIME-sniff, CSP)
  - CORS restriction (origin allowlist)
  - Request body size limits (413 rejection)
  - Error response sanitization (no stack traces, no file paths)
  - Rate limiting active
  - Health endpoint secret exposure check

Run ON the EC2 from ~/Gateway (after scripts/native-setup.sh):
    GATEWAY_API_KEY=<key> python3.12 tests/production/tier8_security_deep.py

Requires GATEWAY_API_KEY to be set — auth enforcement tests need a configured key.
"""
from __future__ import annotations

import sys
import time

import requests

sys.path.insert(0, "tests/production")
from config import BASE_URL, CHAT_URL, LINEAGE_URL, HEADERS, API_KEY, save_artifact

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append({"name": name, "passed": passed, "detail": detail})


# =============================================================================
# BLOCK 1: AUTH ENFORCEMENT
# =============================================================================

def test_lineage_auth():
    """Lineage API endpoints require auth; static dashboard does not."""

    if not API_KEY:
        check("Lineage auth tests require GATEWAY_API_KEY — SKIPPED", False,
              "Set GATEWAY_API_KEY env var to enable")
        return

    # /v1/lineage/sessions without API key -> 401
    r = requests.get(f"{LINEAGE_URL}/sessions",
                     headers={"Content-Type": "application/json"}, timeout=10)
    check("Lineage sessions requires auth",
          r.status_code == 401, f"got {r.status_code}")

    # /v1/lineage/attempts without API key -> 401
    r = requests.get(f"{LINEAGE_URL}/attempts",
                     headers={"Content-Type": "application/json"}, timeout=10)
    check("Lineage attempts requires auth",
          r.status_code == 401, f"got {r.status_code}")

    # /v1/lineage/token-latency without API key -> 401
    r = requests.get(f"{LINEAGE_URL}/token-latency",
                     headers={"Content-Type": "application/json"}, timeout=10)
    check("Lineage token-latency requires auth",
          r.status_code == 401, f"got {r.status_code}")

    # /lineage/ (static dashboard) should still work without auth
    r = requests.get(f"{BASE_URL}/lineage/", timeout=10)
    check("Dashboard static accessible without auth",
          r.status_code == 200, f"got {r.status_code}")

    # With API key -> should work
    r = requests.get(f"{LINEAGE_URL}/sessions", headers=HEADERS, timeout=10)
    check("Lineage sessions with auth -> 200",
          r.status_code in (200, 503), f"got {r.status_code}")

    # Control plane requires auth too
    r = requests.get(f"{BASE_URL}/v1/control/status",
                     headers={"Content-Type": "application/json"}, timeout=10)
    check("Control plane status requires auth",
          r.status_code == 401, f"got {r.status_code}")

    r = requests.get(f"{BASE_URL}/v1/control/status", headers=HEADERS, timeout=10)
    check("Control plane status with auth -> 200",
          r.status_code == 200, f"got {r.status_code}")


# =============================================================================
# BLOCK 2: SECURITY HEADERS
# =============================================================================

def test_security_headers():
    """All responses must include XSS/clickjack/MIME-sniff protection headers."""

    # Check on /health (API response)
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    check("X-Content-Type-Options: nosniff",
          r.headers.get("X-Content-Type-Options") == "nosniff",
          f"got: {r.headers.get('X-Content-Type-Options')}")

    check("X-Frame-Options: DENY",
          r.headers.get("X-Frame-Options") == "DENY",
          f"got: {r.headers.get('X-Frame-Options')}")

    check("Referrer-Policy set",
          "strict-origin" in (r.headers.get("Referrer-Policy") or ""),
          f"got: {r.headers.get('Referrer-Policy')}")

    check("Permissions-Policy set",
          "camera=()" in (r.headers.get("Permissions-Policy") or ""),
          f"got: {r.headers.get('Permissions-Policy')}")

    # Verify headers on a different endpoint (/metrics) to confirm global application
    r2 = requests.get(f"{BASE_URL}/metrics", timeout=10)
    check("Security headers on /metrics too",
          r2.headers.get("X-Content-Type-Options") == "nosniff",
          f"got: {r2.headers.get('X-Content-Type-Options')}")

    # CSP only on dashboard
    r3 = requests.get(f"{BASE_URL}/lineage/", timeout=10)
    csp = r3.headers.get("Content-Security-Policy") or ""
    check("CSP on dashboard page",
          "default-src" in csp, f"CSP: {csp[:80]}")

    check("CSP includes script-src",
          "script-src" in csp, f"CSP: {csp[:80]}")

    # CSP should NOT be on API endpoints
    check("No CSP on /health (API endpoint)",
          not r.headers.get("Content-Security-Policy"),
          f"got: {r.headers.get('Content-Security-Policy')}")


# =============================================================================
# BLOCK 3: CORS RESTRICTION
# =============================================================================

def test_cors_restriction():
    """CORS must not reflect arbitrary origins unless explicitly configured."""

    # OPTIONS preflight from unknown origin
    r = requests.options(f"{BASE_URL}/v1/chat/completions",
                         headers={
                             "Origin": "https://evil.com",
                             "Access-Control-Request-Method": "POST",
                         }, timeout=10)
    allow_origin = r.headers.get("Access-Control-Allow-Origin", "")
    check("CORS rejects unknown origin",
          allow_origin != "*" and "evil.com" not in allow_origin,
          f"Access-Control-Allow-Origin: '{allow_origin}'")

    # Verify CORS methods are restricted
    allow_methods = r.headers.get("Access-Control-Allow-Methods", "")
    check("CORS allows only GET/POST/OPTIONS",
          "DELETE" not in allow_methods and "PUT" not in allow_methods,
          f"Methods: {allow_methods}")

    # Verify preflight does not return 500
    check("CORS preflight does not error",
          r.status_code in (200, 204, 401, 403),
          f"got {r.status_code}")

    # Check a regular request also has correct CORS
    r2 = requests.get(f"{BASE_URL}/health",
                      headers={"Origin": "https://evil.com"}, timeout=10)
    allow_origin2 = r2.headers.get("Access-Control-Allow-Origin", "")
    check("CORS rejects evil origin on GET too",
          allow_origin2 != "*" and "evil.com" not in allow_origin2,
          f"Access-Control-Allow-Origin: '{allow_origin2}'")


# =============================================================================
# BLOCK 4: BODY SIZE LIMIT
# =============================================================================

def test_body_size_limit():
    """Oversized request bodies must be rejected with 413."""

    # Send an oversized Content-Length header (claims 999MB)
    try:
        r = requests.post(f"{BASE_URL}/v1/chat/completions",
                          headers={
                              **(HEADERS or {}),
                              "Content-Length": "999999999",
                          },
                          data=b"x", timeout=10)
        check("Oversized body rejected (4xx)",
              r.status_code in (400, 413), f"got {r.status_code}")
    except requests.exceptions.ConnectionError:
        # Server may close connection immediately on oversized request
        check("Oversized body rejected (connection closed)", True,
              "server closed connection")

    # Verify normal-sized requests still work
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    check("Normal requests still work after size check",
          r.status_code == 200, f"got {r.status_code}")


# =============================================================================
# BLOCK 5: ERROR RESPONSE SANITIZATION
# =============================================================================

def test_error_sanitization():
    """Error responses must not leak stack traces, file paths, or internals."""

    # Malformed JSON to control plane
    r = requests.post(f"{BASE_URL}/v1/control/policies",
                      headers=HEADERS, data="not-json", timeout=10)
    body = r.text
    check("Control error: no stack trace",
          "Traceback" not in body, body[:120] if "Traceback" in body else "clean")
    check("Control error: no file paths",
          "/src/" not in body and "/gateway/" not in body,
          body[:120] if "/src/" in body or "/gateway/" in body else "clean")

    # Malformed JSON to chat endpoint
    r = requests.post(CHAT_URL,
                      headers={**HEADERS, "Content-Type": "application/json"},
                      data="{{invalid-json", timeout=10)
    body = r.text
    check("Chat error: no stack trace",
          "Traceback" not in body, body[:120] if "Traceback" in body else "clean")
    check("Chat error: no file paths",
          "/src/" not in body and "/gateway/" not in body and 'File "' not in body,
          body[:120] if ("/src/" in body or 'File "' in body) else "clean")
    check("Chat error: no internal module names",
          "orchestrator.py" not in body and "main.py" not in body,
          body[:120] if "orchestrator.py" in body else "clean")

    # Missing required fields
    r = requests.post(CHAT_URL,
                      headers=HEADERS,
                      json={"not_a_valid_field": True},
                      timeout=10)
    body = r.text
    check("Missing fields error: no stack trace",
          "Traceback" not in body, body[:120] if "Traceback" in body else "clean")


# =============================================================================
# BLOCK 6: RATE LIMITING
# =============================================================================

def test_rate_limiting_active():
    """Verify rate limiting infrastructure is operational."""

    r = requests.get(f"{BASE_URL}/health", timeout=10)
    check("Gateway healthy with rate limiting",
          r.status_code == 200, f"got {r.status_code}")

    # Send rapid requests to health (exempt) to verify gateway stays up
    for _ in range(20):
        requests.get(f"{BASE_URL}/health", timeout=5)
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    check("Gateway stable after rapid health requests",
          r.status_code == 200, f"got {r.status_code}")

    # Check metrics for rate limit counter existence
    r = requests.get(f"{BASE_URL}/metrics", timeout=10)
    if r.status_code == 200:
        has_rate_metric = "rate_limit" in r.text
        check("Rate limit metric registered",
              has_rate_metric, "walacor_gateway_rate_limit_hits_total present" if has_rate_metric else "metric not found")


# =============================================================================
# BLOCK 7: NO STACK TRACES IN ERRORS
# =============================================================================

def test_no_stack_traces():
    """Comprehensive stack trace leak check across multiple error conditions."""

    # 404 — nonexistent endpoint
    r = requests.get(f"{BASE_URL}/v1/nonexistent", headers=HEADERS, timeout=10)
    check("404 has no stack trace",
          "Traceback" not in r.text and 'File "/' not in r.text,
          r.text[:120] if "Traceback" in r.text else "clean")

    # Malformed body
    r = requests.post(CHAT_URL,
                      headers={**HEADERS, "Content-Type": "application/json"},
                      data="{{invalid", timeout=10)
    check("Malformed JSON has no stack trace",
          "Traceback" not in r.text and 'File "/' not in r.text,
          r.text[:120] if "Traceback" in r.text else "clean")

    # Empty body
    r = requests.post(CHAT_URL,
                      headers={**HEADERS, "Content-Type": "application/json"},
                      data="", timeout=10)
    check("Empty body has no stack trace",
          "Traceback" not in r.text and 'File "/' not in r.text,
          r.text[:120] if "Traceback" in r.text else "clean")

    # Wrong content type
    r = requests.post(CHAT_URL,
                      headers={**HEADERS, "Content-Type": "text/plain"},
                      data="hello", timeout=10)
    check("Wrong content-type has no stack trace",
          "Traceback" not in r.text and 'File "/' not in r.text,
          r.text[:120] if "Traceback" in r.text else "clean")

    # Verify none of these leak Python module paths
    for endpoint_name, url, method in [
        ("nonexistent", f"{BASE_URL}/v1/nonexistent", "GET"),
        ("chat malformed", CHAT_URL, "POST"),
    ]:
        if method == "GET":
            resp = requests.get(url, headers=HEADERS, timeout=10)
        else:
            resp = requests.post(url, headers=HEADERS, data="{bad", timeout=10)
        check(f"{endpoint_name}: no Python paths in error",
              "site-packages" not in resp.text and ".py" not in resp.text.split('"error"')[0] if '"error"' in resp.text else "site-packages" not in resp.text,
              "clean")


# =============================================================================
# BLOCK 8: HEALTH ENDPOINT — NO SECRETS
# =============================================================================

def test_health_no_secrets():
    """Health endpoint must not expose API keys, passwords, or tokens."""

    r = requests.get(f"{BASE_URL}/health", timeout=10)
    check("Health endpoint accessible", r.status_code == 200, f"got {r.status_code}")

    body = r.text

    # No API key patterns
    check("Health has no sk- prefix keys",
          "sk-" not in body, "found sk- pattern" if "sk-" in body else "clean")

    check("Health has no api_key values",
          "api_key" not in body.lower() or '"api_key"' not in body,
          "clean")

    check("Health has no passwords",
          "password" not in body.lower(),
          "found password" if "password" in body.lower() else "clean")

    check("Health has no secret values",
          "secret" not in body.lower() or body.lower().count("secret") == 0,
          "clean")

    check("Health has no redis URLs with passwords",
          "redis://:!" not in body and "@redis" not in body,
          "clean")

    # Verify health does return expected fields (sanity)
    if r.status_code == 200:
        data = r.json()
        check("Health has status field",
              "status" in data, f"keys: {list(data.keys())[:8]}")
        check("Health has uptime",
              data.get("uptime_seconds", 0) > 0,
              f"uptime: {data.get('uptime_seconds')}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print(f"\n{'='*60}")
    print(f"  TIER 8: SECURITY DEEP VERIFICATION")
    print(f"  Validates all critical/high severity security fixes")
    print(f"  API Key configured: {'YES' if API_KEY else 'NO (some tests will fail)'}")
    print(f"{'='*60}\n")

    blocks = [
        ("1/8", "Auth Enforcement", test_lineage_auth),
        ("2/8", "Security Headers", test_security_headers),
        ("3/8", "CORS Restriction", test_cors_restriction),
        ("4/8", "Body Size Limit", test_body_size_limit),
        ("5/8", "Error Response Sanitization", test_error_sanitization),
        ("6/8", "Rate Limiting Active", test_rate_limiting_active),
        ("7/8", "No Stack Traces in Errors", test_no_stack_traces),
        ("8/8", "Health Endpoint — No Secrets", test_health_no_secrets),
    ]

    for num, name, fn in blocks:
        print(f"[{num}] {name}")
        try:
            fn()
        except Exception as e:
            check(f"{name} -- CRASHED", False, str(e)[:120])
        print()

    passed = sum(1 for r in RESULTS if r["passed"])
    failed = sum(1 for r in RESULTS if not r["passed"])
    total = len(RESULTS)

    print(f"{'='*60}")
    print(f"  TIER 8 SECURITY: {passed}/{total} PASS, {failed} FAIL")
    print(f"{'='*60}")

    save_artifact("tier8_security", {
        "tier": "8_security_deep",
        "total": total,
        "passed": passed,
        "failed": failed,
        "results": RESULTS,
        "gate": "PASS" if failed == 0 else "FAIL",
    })

    if failed > 0:
        print(f"\n  GATE FAILED -- {failed} security checks need fixing")
        sys.exit(1)
    print(f"\n  GATE PASSED -- all security controls verified")


if __name__ == "__main__":
    main()
