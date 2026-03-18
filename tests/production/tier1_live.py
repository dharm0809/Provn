#!/usr/bin/env python3
"""Tier 1 live integrity checks — session chain, WAL, lineage API, completeness.

Run ON the EC2 instance from ~/Gateway:
    python3.12 tests/production/tier1_live.py

API response shapes (from lineage/api.py + reader.py):
  GET /v1/lineage/sessions  → {"sessions": [{"session_id": ..., ...}], "limit": N, "offset": N}
  GET /v1/lineage/attempts  → {"items": [...], "stats": {...}, "total": N}
  GET /v1/lineage/verify/{session_id} → {"valid": bool, ...}
"""
from __future__ import annotations

import sys
import uuid

import requests

sys.path.insert(0, "tests/production")
from config import BASE_URL, CHAT_URL, LINEAGE_URL, HEADERS, MODEL, save_artifact

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append({"name": name, "passed": passed, "detail": detail})


def chat(content: str, session_id: str | None = None) -> requests.Response:
    h = {**HEADERS}
    if session_id:
        h["X-Session-Id"] = session_id
    return requests.post(CHAT_URL, json={
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 20,
    }, headers=h, timeout=90)


def test_health():
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    check("Health returns 200", r.status_code == 200, f"got {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("Health response non-empty", len(data) > 1, str(list(data.keys())))


def test_completeness():
    """Every request must produce an attempt record — check via total count."""
    pre = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    pre_total = pre.json().get("total", 0) if pre.status_code == 200 else 0

    r = chat("Say hello.")
    check("Valid request returns 200", r.status_code == 200, f"got {r.status_code}")

    post = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    post_total = post.json().get("total", 0) if post.status_code == 200 else 0
    check("Attempt record written after request",
          post_total > pre_total, f"before={pre_total}, after={post_total}")


def test_session_chain():
    """3 requests in one session → chain verifies as valid."""
    session_id = str(uuid.uuid4())
    for i in range(3):
        r = chat(f"What is {i+1} + {i+1}?", session_id=session_id)
        if r.status_code != 200:
            check(f"Session chain request {i+1}", False, f"got {r.status_code}")
            return
        check(f"Session chain request {i+1} succeeds", True)

    r = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    check("Sessions endpoint returns 200", r.status_code == 200)
    if r.status_code != 200:
        return

    sessions = r.json().get("sessions", [])
    match = next((s for s in sessions if s.get("session_id") == session_id), None)
    check("Our session found in lineage", match is not None, f"session_id={session_id[:8]}...")

    if match:
        sid = match["session_id"]
        rv = requests.get(f"{LINEAGE_URL}/verify/{sid}", timeout=10)
        check("Chain verify endpoint returns 200", rv.status_code == 200, f"got {rv.status_code}")
        if rv.status_code == 200:
            v = rv.json()
            valid = bool(v.get("valid") or v.get("chain_valid") or v.get("result") == "valid")
            check("Session chain is cryptographically valid", valid, str(v))


def test_lineage_endpoints():
    """All lineage endpoints return 200."""
    for name, url in [
        ("sessions", f"{LINEAGE_URL}/sessions"),
        ("attempts", f"{LINEAGE_URL}/attempts"),
        ("token-latency 1h", f"{LINEAGE_URL}/token-latency?range=1h"),
    ]:
        r = requests.get(url, timeout=10)
        check(f"Lineage /{name} → 200", r.status_code == 200, f"got {r.status_code}")

    # execution detail — need a real session_id first
    r = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    if r.status_code == 200:
        sessions = r.json().get("sessions", [])
        if sessions:
            sid = sessions[0]["session_id"]
            r2 = requests.get(f"{LINEAGE_URL}/sessions/{sid}", timeout=10)
            check("Lineage /sessions/{id} timeline → 200", r2.status_code == 200,
                  f"got {r2.status_code}")


def test_wal():
    r = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    if r.status_code == 200:
        total = r.json().get("total", 0)
        check("WAL has attempt records", total > 0, f"{total} total")
    else:
        check("WAL accessible via attempts endpoint", False, f"got {r.status_code}")


def test_metrics():
    r = requests.get(f"{BASE_URL}/metrics", timeout=10)
    check("Metrics endpoint → 200", r.status_code == 200, f"got {r.status_code}")
    if r.status_code == 200:
        check("Metrics response non-empty", len(r.text) > 50)


def main():
    print("\n=== Tier 1: Live Integrity Checks ===\n")
    print("[1/6] Health"); test_health()
    print("[2/6] Completeness invariant"); test_completeness()
    print("[3/6] Session chain + verification"); test_session_chain()
    print("[4/6] Lineage API (all endpoints)"); test_lineage_endpoints()
    print("[5/6] WAL integrity"); test_wal()
    print("[6/6] Metrics endpoint"); test_metrics()

    passed = sum(1 for r in RESULTS if r["passed"])
    failed = sum(1 for r in RESULTS if not r["passed"])
    print(f"\n{'='*40}")
    print(f"Tier 1 Live: {passed} PASS, {failed} FAIL")

    save_artifact("tier1_live", {
        "tier": "1_live", "passed": passed, "failed": failed,
        "results": RESULTS,
        "gate": "PASS" if failed == 0 else "FAIL",
    })

    if failed > 0:
        print("\nGATE FAILED — fix before Tier 2")
        sys.exit(1)
    print("\nGATE PASSED")


if __name__ == "__main__":
    main()
