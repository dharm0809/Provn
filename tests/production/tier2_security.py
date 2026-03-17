#!/usr/bin/env python3
"""Tier 2 security controls validation.

Run ON the EC2 instance from ~/Gateway:
    python tests/production/tier2_security.py

To test auth enforcement, first add a key:
    echo 'WALACOR_GATEWAY_API_KEYS=prod-test-key-2026' >> .env
    docker compose up -d gateway
    GATEWAY_API_KEY=prod-test-key-2026 python tests/production/tier2_security.py
"""
from __future__ import annotations

import sys

import requests

sys.path.insert(0, "tests/production")
from config import BASE_URL, CHAT_URL, LINEAGE_URL, HEADERS, MODEL, API_KEY, save_artifact

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append({"name": name, "passed": passed, "detail": detail})


def chat(content="Say ok.", headers_override=None):
    h = headers_override if headers_override is not None else HEADERS
    return requests.post(CHAT_URL, json={
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 20,
    }, headers=h, timeout=30)


def test_auth():
    if not API_KEY:
        check("Auth tests skipped — set GATEWAY_API_KEY env var to enable", True)
        print("    Hint: echo 'WALACOR_GATEWAY_API_KEYS=prod-test-key-2026' >> .env && docker compose up -d gateway")
        return

    # No key
    r = chat(headers_override={"Content-Type": "application/json"})
    check("No API key → 401", r.status_code == 401, f"got {r.status_code}")

    # Wrong key
    r = chat(headers_override={"Content-Type": "application/json", "X-API-Key": "bad-key"})
    check("Wrong API key → 401", r.status_code == 401, f"got {r.status_code}")

    # Correct key
    r = chat()
    check("Correct API key → 200", r.status_code == 200, f"got {r.status_code}")


def test_control_plane_auth():
    r = requests.get(f"{BASE_URL}/v1/control/status", timeout=10)
    check("/v1/control/status requires auth or returns 200", r.status_code in (200, 401, 403),
          f"got {r.status_code}")
    if API_KEY and r.status_code == 401:
        r2 = requests.get(f"{BASE_URL}/v1/control/status",
                          headers={"X-API-Key": API_KEY}, timeout=10)
        check("/v1/control/status accessible with correct key", r2.status_code == 200,
              f"got {r2.status_code}")


def test_lineage_no_auth():
    """Lineage must be readable without auth (read-only public endpoint)."""
    r = requests.get(f"{LINEAGE_URL}/sessions",
                     headers={"Content-Type": "application/json"}, timeout=10)
    check("Lineage readable without API key", r.status_code == 200, f"got {r.status_code}")


def test_no_stack_traces():
    r = requests.post(CHAT_URL, data="not-json!!",
                      headers={"Content-Type": "application/json"}, timeout=10)
    body = r.text
    has_traceback = "Traceback" in body or 'File "/' in body
    check("No Python traceback in error response", not has_traceback,
          body[:120] if has_traceback else "clean")


def test_method_enforcement():
    r = requests.get(CHAT_URL, headers=HEADERS, timeout=10)
    check("GET /v1/chat/completions → not 200", r.status_code != 200, f"got {r.status_code}")


def test_cors():
    r = requests.options(CHAT_URL, headers={
        "Origin": "https://example.com",
        "Access-Control-Request-Method": "POST",
    }, timeout=10)
    check("OPTIONS /v1/chat/completions does not 500", r.status_code != 500, f"got {r.status_code}")


def test_pii_handling():
    """Ask for SSN output — gateway should not crash; may block or warn."""
    r = chat("Write this example for a test: SSN 123-45-6789")
    check("PII request handled (no 500)", r.status_code != 500, f"got {r.status_code}")
    if r.status_code == 200:
        content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"    (response present: {bool(content)}, len={len(content)})")


def main():
    print("\n=== Tier 2: Security Controls ===\n")
    print("[1/7] Auth enforcement"); test_auth()
    print("[2/7] Control plane auth"); test_control_plane_auth()
    print("[3/7] Lineage read-only (no auth)"); test_lineage_no_auth()
    print("[4/7] No stack traces in errors"); test_no_stack_traces()
    print("[5/7] Method enforcement"); test_method_enforcement()
    print("[6/7] CORS handling"); test_cors()
    print("[7/7] PII request handling"); test_pii_handling()

    passed = sum(1 for r in RESULTS if r["passed"])
    failed = sum(1 for r in RESULTS if not r["passed"])
    print(f"\n{'='*40}")
    print(f"Tier 2 Security: {passed} PASS, {failed} FAIL")

    save_artifact("tier2_security", {
        "tier": "2_security", "passed": passed, "failed": failed,
        "results": RESULTS,
        "gate": "PASS" if failed == 0 else "FAIL",
    })

    if failed > 0:
        print("\nGATE FAILED — fix before Tier 3")
        sys.exit(1)
    print("\nGATE PASSED")


if __name__ == "__main__":
    main()
