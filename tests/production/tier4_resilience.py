#!/usr/bin/env python3
"""Tier 4 resilience tests — runs ON the EC2, uses docker compose directly.

Run from ~/Gateway on the EC2:
    python tests/production/tier4_resilience.py

WARNING: This test intentionally stops/restarts Ollama and the gateway.
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid



import requests

sys.path.insert(0, "tests/production")
from config import BASE_URL, CHAT_URL, LINEAGE_URL, HEADERS, MODEL, save_artifact

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append({"name": name, "passed": passed, "detail": detail})


def docker(cmd: str) -> str:
    """Run a docker compose command directly on this machine."""
    result = subprocess.run(
        f"docker compose {cmd}",
        shell=True, capture_output=True, text=True, timeout=120
    )
    return result.stdout.strip()


def chat(content="Say ok.", session_id=None, stream=False):
    h = {**HEADERS}
    if session_id:
        h["X-Session-Id"] = session_id
    return requests.post(CHAT_URL, json={
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 15,
        "stream": stream,
    }, headers=h, timeout=30)


def wait_healthy(max_wait=60) -> bool:
    for _ in range(max_wait):
        try:
            r = requests.get(f"{BASE_URL}/health", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ── 1. Baseline ───────────────────────────────────────────────────────────────

def test_baseline():
    r = chat()
    check("Baseline request succeeds", r.status_code == 200, f"got {r.status_code}")


# ── 2. Ollama down → graceful error + attempt record written ─────────────────

def test_ollama_down():
    pre = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    pre_count = pre.json().get("total", 0) if pre.status_code == 200 else 0

    print("  Stopping Ollama...")
    docker("stop ollama")
    time.sleep(5)

    r = chat()
    check("Request while Ollama down → non-200 error", r.status_code != 200,
          f"got {r.status_code}")
    check("Error response not 200 (not a fake success)", r.status_code in (502, 503, 504, 500, 422),
          f"got {r.status_code}")

    time.sleep(2)  # WAL write is async — wait for finally block to complete
    post = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    post_count = post.json().get("total", 0) if post.status_code == 200 else 0
    check("Attempt record written even when Ollama is down",
          post_count > pre_count, f"before={pre_count}, after={post_count}")

    print("  Restarting Ollama...")
    docker("start ollama")
    time.sleep(40)  # Ollama needs time to reload model

    r2 = chat()
    check("Request succeeds after Ollama restart", r2.status_code == 200, f"got {r2.status_code}")


# ── 3. Gateway restart → WAL preserved ───────────────────────────────────────

def test_gateway_restart():
    session_id = str(uuid.uuid4())
    r = chat("Name three planets.", session_id=session_id)
    check("Request before gateway restart succeeds", r.status_code == 200)

    pre = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    pre_count = len(pre.json().get("sessions", [])) if pre.status_code == 200 else 0

    print("  Restarting gateway container...")
    docker("restart gateway")
    time.sleep(35)

    healthy = wait_healthy(60)
    check("Gateway healthy after restart", healthy)

    post = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    post_count = len(post.json().get("sessions", [])) if post.status_code == 200 else 0
    check("Session records preserved across restart",
          post_count >= pre_count, f"before={pre_count}, after={post_count}")

    r2 = chat("Say hello.")
    check("New request works after restart", r2.status_code == 200, f"got {r2.status_code}")


# ── 4. Streaming safety ───────────────────────────────────────────────────────

def test_stream_safety():
    pre = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    pre_count = pre.json().get("total", 0) if pre.status_code == 200 else 0

    r = requests.post(CHAT_URL, json={
        "model": MODEL,
        "messages": [{"role": "user", "content": "Count: one, two, three."}],
        "max_tokens": 30,
        "stream": True,
    }, headers=HEADERS, stream=True, timeout=90)

    check("Streaming request → 200", r.status_code == 200, f"got {r.status_code}")
    # Consume raw bytes and split manually — more reliable than iter_lines() with chunked SSE
    raw = b"".join(r.iter_content(chunk_size=None))
    text = raw.decode("utf-8", errors="replace")
    chunks = sum(1 for line in text.split("\n")
                 if line.startswith("data: ") and line.strip() != "data: [DONE]")
    check("Streaming has multiple SSE chunks", chunks > 1, f"{chunks} chunks")

    time.sleep(3)  # WAL write is async — wait for stream background task
    post = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    post_count = post.json().get("total", 0) if post.status_code == 200 else 0
    check("Attempt record written after stream completes",
          post_count > pre_count, f"before={pre_count}, after={post_count}")


# ── 5. System healthy after all disruptions ───────────────────────────────────

def test_final_health():
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    check("Health → 200 after all resilience tests", r.status_code == 200)
    r2 = chat()
    check("Normal request works after all resilience tests", r2.status_code == 200)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Tier 4: Resilience Tests ===")
    print("  WARNING: Will stop/restart Ollama and gateway containers.\n")

    print("[1/5] Baseline"); test_baseline()
    print("[2/5] Ollama down → graceful error + audit record"); test_ollama_down()
    print("[3/5] Gateway restart → WAL preserved"); test_gateway_restart()
    print("[4/5] Streaming safety"); test_stream_safety()
    print("[5/5] Final health check"); test_final_health()

    passed = sum(1 for r in RESULTS if r["passed"])
    failed = sum(1 for r in RESULTS if not r["passed"])
    print(f"\n{'='*40}")
    print(f"Tier 4 Resilience: {passed} PASS, {failed} FAIL")

    save_artifact("tier4_resilience", {
        "tier": "4_resilience", "passed": passed, "failed": failed,
        "results": RESULTS,
        "gate": "PASS" if failed == 0 else "FAIL",
    })

    if failed > 0:
        print("\nGATE FAILED — fix before Tier 5")
        sys.exit(1)
    print("\nGATE PASSED")


if __name__ == "__main__":
    main()
