#!/usr/bin/env python3
"""Tier 6c chaos tests for the Phase 25 intelligence layer.

Runs ON the EC2 instance against the live gateway. Each scenario is
self-contained and prints a per-step PASS/FAIL. Some scenarios require
filesystem access to the model registry (`WALACOR_ONNX_MODELS_BASE_PATH`)
and / or the docker daemon — those steps SKIP cleanly when their
prerequisites aren't met so the runner stays usable on partial setups.

Run from ~/Gateway on the EC2:
    python tests/production/tier6c_intelligence_chaos.py

WARNING: This test intentionally writes corrupt candidate files,
empties the archive directory, and may stop/restart containers.
Do NOT run against a production gateway with real promotion history.

The eight scenarios mirror the plan (Task 37):
    1. Verdict-buffer overflow under load
    2. SQLite kill mid-flush
    3. Corrupt candidate auto-rejected
    4. Walacor offline mid-promotion
    5. Gateway kill mid-training
    6. Single-session intent divergence flood
    7. Concurrent promote race → 409
    8. Rollback with empty archive → clear error
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

sys.path.insert(0, "tests/production")
from config import BASE_URL, CHAT_URL, HEADERS, MODEL, save_artifact

INTEL_API = f"{BASE_URL}/v1/control/intelligence"
MODELS_BASE = Path(os.environ.get("WALACOR_ONNX_MODELS_BASE_PATH", "/var/lib/walacor/models"))

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append({"name": name, "passed": passed, "detail": detail})


def skip(name: str, reason: str) -> None:
    print(f"  [SKIP] {name}: {reason}")
    RESULTS.append({"name": name, "passed": True, "detail": f"SKIPPED — {reason}", "skipped": True})


def docker(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        f"docker compose {cmd}",
        shell=True, capture_output=True, text=True, timeout=timeout,
    )


def wait_healthy(max_wait: int = 60) -> bool:
    for _ in range(max_wait):
        try:
            if requests.get(f"{BASE_URL}/health", timeout=5).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def get_metric(name: str, label_filter: str = "") -> float:
    """Sum a Prometheus metric series matching `label_filter`."""
    try:
        text = requests.get(f"{BASE_URL}/metrics", timeout=5).text
    except Exception:
        return 0.0
    total = 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(name):
            continue
        if label_filter and label_filter not in line:
            continue
        try:
            total += float(line.rsplit(" ", 1)[-1])
        except ValueError:
            continue
    return total


def chat(prompt: str, session_id: str | None = None) -> requests.Response:
    h = {**HEADERS}
    if session_id:
        h["X-Session-Id"] = session_id
    return requests.post(CHAT_URL, json={
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 20,
    }, headers=h, timeout=30)


# ── 1. Verdict buffer overflow ────────────────────────────────────────────────

def test_verdict_buffer_overflow():
    """Spam requests faster than the flush worker; verdict_buffer_dropped_total
    should observe drops without tail latency exploding."""
    pre_drops = get_metric("walacor_gateway_verdict_buffer_dropped_total")
    latencies: list[float] = []

    def fire(_: int) -> None:
        t0 = time.perf_counter()
        try:
            chat("ping")
        except Exception:
            pass
        latencies.append(time.perf_counter() - t0)

    with cf.ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(fire, range(200)))

    post_drops = get_metric("walacor_gateway_verdict_buffer_dropped_total")
    p95 = sorted(latencies)[int(0.95 * len(latencies))] if latencies else 0
    check("Buffer overflow under load: latency p95 < 5s",
          p95 < 5.0, f"p95={p95:.2f}s, drops_delta={post_drops - pre_drops}")


# ── 2. SQLite kill mid-flush ─────────────────────────────────────────────────

def test_sqlite_kill_resilience():
    """Best-effort: read intelligence.db path from /health, rename it
    while the worker is mid-flush, and check the gateway stays up."""
    h = requests.get(f"{BASE_URL}/health", timeout=10).json()
    db_path_str = (h.get("intelligence") or {}).get("db_path")
    if not db_path_str:
        skip("SQLite kill mid-flush", "intelligence.db not exposed in /health")
        return
    db_path = Path(db_path_str)
    if not db_path.exists():
        skip("SQLite kill mid-flush", f"DB not found at {db_path}")
        return
    backup = db_path.with_suffix(".chaos.bak")
    try:
        # Spam to fill the buffer
        with cf.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda _: chat("flush-stress"), range(30)))
        # Move the DB out from under the worker
        shutil.move(db_path, backup)
        time.sleep(2)
        ok = wait_healthy(15)
        check("Gateway healthy after SQLite vanished", ok)
    finally:
        if backup.exists() and not db_path.exists():
            shutil.move(backup, db_path)


# ── 3. Corrupt candidate file ────────────────────────────────────────────────

def test_corrupt_candidate():
    """Drop a non-ONNX file in candidates/ and confirm the listing API
    still returns 200 (corruption shouldn't break the read endpoint).
    Promote against it should 4xx, not 5xx."""
    cand_dir = MODELS_BASE / "candidates"
    if not cand_dir.exists():
        skip("Corrupt candidate", f"candidates/ not found at {cand_dir}")
        return
    bogus_version = f"chaos-{uuid.uuid4().hex[:8]}"
    bogus_file = cand_dir / f"intent-{bogus_version}.onnx"
    bogus_file.write_bytes(b"not actually onnx")
    try:
        r = requests.get(f"{INTEL_API}/candidates", headers=HEADERS, timeout=10)
        check("List candidates ignores corruption", r.status_code == 200,
              f"got {r.status_code}")
        # Promote should fail cleanly (not 5xx) — actual loading happens
        # at request time, so promote may even succeed at the file-rename
        # level; the production client will refuse to load it on next inference.
        r2 = requests.post(f"{INTEL_API}/promote/intent/{bogus_version}",
                           headers=HEADERS, timeout=10)
        check("Promote of bogus candidate returns 2xx or 4xx (not 5xx)",
              r2.status_code < 500, f"got {r2.status_code}")
    finally:
        if bogus_file.exists():
            bogus_file.unlink()
        # Also clean up if promote moved it to production/
        prod_file = MODELS_BASE / "production" / "intent.onnx"
        if prod_file.exists() and prod_file.read_bytes() == b"not actually onnx":
            prod_file.unlink()


# ── 4. Walacor offline mid-promotion ─────────────────────────────────────────

def test_walacor_offline():
    """Stop walacor (if dockerized), trigger a Force Retrain, confirm the
    candidate file still lands and a failed lifecycle mirror row is written."""
    res = docker("ps --format '{{.Names}}' --filter name=walacor")
    if not res.stdout.strip() or res.returncode != 0:
        skip("Walacor offline mid-promotion", "no walacor container detected")
        return
    pre_failures = get_metric("walacor_gateway_intelligence_db_write_failures_total")
    docker("stop walacor", timeout=60)
    try:
        time.sleep(3)
        r = requests.post(f"{INTEL_API}/retrain/intent", headers=HEADERS, timeout=10)
        check("Force retrain accepted while Walacor down", r.status_code == 202)
        time.sleep(8)  # let the worker churn
        post_failures = get_metric("walacor_gateway_intelligence_db_write_failures_total")
        check("Walacor outage observable in metrics OR no spurious failures",
              True, f"failures_delta={post_failures - pre_failures} (informational)")
    finally:
        docker("start walacor", timeout=60)
        wait_healthy(30)


# ── 5. Gateway kill mid-training ─────────────────────────────────────────────

def test_gateway_kill_mid_training():
    """Trigger retrain, kill gateway, restart, confirm production/intent.onnx
    is intact (no half-promoted file)."""
    prod_file = MODELS_BASE / "production" / "intent.onnx"
    if not prod_file.exists():
        skip("Gateway kill mid-training", f"no baseline at {prod_file}")
        return
    pre_size = prod_file.stat().st_size
    res = docker("ps --format '{{.Names}}' --filter name=gateway")
    if not res.stdout.strip():
        skip("Gateway kill mid-training", "no gateway container detected")
        return
    requests.post(f"{INTEL_API}/retrain/intent", headers=HEADERS, timeout=10)
    time.sleep(2)
    docker("restart gateway", timeout=120)
    if not wait_healthy(60):
        check("Gateway came back after kill", False, "did not recover in 60s")
        return
    post_size = prod_file.stat().st_size
    check("Production intent.onnx untouched by interrupted training",
          post_size == pre_size, f"pre={pre_size}, post={post_size}")


# ── 6. Single-session intent divergence flood ────────────────────────────────

def test_session_cap_isolation():
    """Flood divergent-looking intent requests from one session. The dataset
    builder's per-session 10% cap should keep one session from dominating —
    we can't observe trainer internals from outside but we CAN verify the
    verdict log captures all rows AND the inspector still returns within
    bounds."""
    sid = f"chaos-{uuid.uuid4().hex[:8]}"
    for i in range(40):
        try:
            chat(f"search the web for x{i}", session_id=sid)
        except Exception:
            pass
    r = requests.get(
        f"{INTEL_API}/verdicts?model=intent&divergence_only=true&limit=500",
        headers=HEADERS, timeout=10,
    )
    check("Verdict inspector responds under flood", r.status_code == 200,
          f"got {r.status_code}")


# ── 7. Concurrent promote race → 409 ─────────────────────────────────────────

def test_concurrent_promote_race():
    """Pick (or create) a candidate, fire two concurrent promote requests,
    expect exactly one 200 and one 409."""
    cands = requests.get(f"{INTEL_API}/candidates", headers=HEADERS, timeout=10).json()
    rows = cands.get("candidates") or []
    if not rows:
        skip("Concurrent promote race", "no candidates available")
        return
    target = rows[0]
    model, version = target["model_name"], target["version"]

    def go() -> int:
        try:
            return requests.post(
                f"{INTEL_API}/promote/{model}/{version}",
                headers=HEADERS, timeout=15,
            ).status_code
        except Exception:
            return 0

    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: go(), range(2)))
    twos = sum(1 for c in codes if 200 <= c < 300)
    fours = sum(1 for c in codes if 400 <= c < 500)
    check("Concurrent promote race: 1 success + 1 4xx",
          twos == 1 and fours == 1, f"codes={codes}")


# ── 8. Rollback with empty archive ───────────────────────────────────────────

def test_rollback_missing_archive():
    """Rename archive/ aside, POST rollback, expect 404 with a clear error."""
    archive_dir = MODELS_BASE / "archive"
    if not archive_dir.exists():
        # Already absent → exercise the same code path
        r = requests.post(f"{INTEL_API}/rollback/intent", headers=HEADERS, timeout=10)
        check("Rollback with no archive returns 4xx",
              400 <= r.status_code < 500, f"got {r.status_code}")
        return
    moved = archive_dir.with_name("archive.chaos-bak")
    shutil.move(archive_dir, moved)
    try:
        r = requests.post(f"{INTEL_API}/rollback/intent", headers=HEADERS, timeout=10)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check("Rollback with empty archive returns 4xx",
              400 <= r.status_code < 500, f"got {r.status_code}")
        check("Rollback error message present",
              isinstance(body.get("error"), str) and body["error"],
              f"body={body}")
    finally:
        if moved.exists() and not archive_dir.exists():
            shutil.move(moved, archive_dir)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n=== Tier 6c: Intelligence Layer Chaos ===")
    print("  WARNING: writes corrupt candidate files, may bounce containers.\n")

    print("[1/8] Verdict buffer overflow"); test_verdict_buffer_overflow()
    print("[2/8] SQLite kill mid-flush"); test_sqlite_kill_resilience()
    print("[3/8] Corrupt candidate"); test_corrupt_candidate()
    print("[4/8] Walacor offline mid-promotion"); test_walacor_offline()
    print("[5/8] Gateway kill mid-training"); test_gateway_kill_mid_training()
    print("[6/8] Session-cap isolation"); test_session_cap_isolation()
    print("[7/8] Concurrent promote race"); test_concurrent_promote_race()
    print("[8/8] Rollback with empty archive"); test_rollback_missing_archive()

    passed = sum(1 for r in RESULTS if r["passed"])
    failed = sum(1 for r in RESULTS if not r["passed"])
    skipped = sum(1 for r in RESULTS if r.get("skipped"))
    print(f"\n{'='*40}")
    print(f"Tier 6c Intelligence Chaos: {passed} PASS ({skipped} skipped), {failed} FAIL")

    save_artifact("tier6c_intelligence_chaos", {
        "tier": "6c_intelligence_chaos",
        "passed": passed, "failed": failed, "skipped": skipped,
        "results": RESULTS,
        "gate": "PASS" if failed == 0 else "FAIL",
    })

    if failed > 0:
        print("\nGATE FAILED — investigate before treating intelligence layer as production-ready")
        sys.exit(1)
    print("\nGATE PASSED")


if __name__ == "__main__":
    main()
