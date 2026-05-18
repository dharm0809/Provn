#!/usr/bin/env python3
"""
Schema-mapper accuracy test — 50 diverse Haiku questions.

Starts the gateway with .env.local-test config, fires 50 varied chat-completions
requests against claude-haiku-4-5, then queries the WAL and intelligence DBs
to report on:
  - Response success rate
  - Schema-mapper verdict distribution (prediction labels, overflow keys)
  - Rolling accuracy (rows with divergence_signal)
  - Execution record completeness (wal_records vs gateway_attempts)
  - Token usage captured correctly

Usage:
    python scripts/test_schema_mapper_50.py
    python scripts/test_schema_mapper_50.py --gateway-url http://localhost:8000 --key test-key-alpha
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

GATEWAY_PORT = 8000
ENV_FILE = ".env.local-test"
MODEL = "claude-haiku-4-5-20251001"
WAL_PATH = "/tmp/walacor-wal-localtest"

QUESTIONS: list[dict] = [
    # Science — Physics
    {"category": "physics",       "content": "What is the speed of light in a vacuum?"},
    {"category": "physics",       "content": "Explain the Heisenberg uncertainty principle in simple terms."},
    {"category": "physics",       "content": "Why do objects appear to fall at the same speed in a vacuum regardless of mass?"},
    {"category": "physics",       "content": "What is dark matter and why do physicists think it exists?"},
    # Science — Chemistry
    {"category": "chemistry",     "content": "What is the difference between an acid and a base?"},
    {"category": "chemistry",     "content": "Why does iron rust but gold does not?"},
    {"category": "chemistry",     "content": "What are covalent bonds and how do they differ from ionic bonds?"},
    # Science — Biology
    {"category": "biology",       "content": "How does CRISPR-Cas9 gene editing work?"},
    {"category": "biology",       "content": "What is the difference between mitosis and meiosis?"},
    {"category": "biology",       "content": "How do vaccines train the immune system?"},
    # Science — Astronomy
    {"category": "astronomy",     "content": "How do black holes form?"},
    {"category": "astronomy",     "content": "What is the difference between a solar eclipse and a lunar eclipse?"},
    {"category": "astronomy",     "content": "How long does light from the nearest star outside our solar system take to reach Earth?"},
    # Mathematics
    {"category": "math",          "content": "What is the Pythagorean theorem and give a real-world example?"},
    {"category": "math",          "content": "Explain the concept of a limit in calculus."},
    {"category": "math",          "content": "What is the difference between permutation and combination?"},
    {"category": "math",          "content": "How do you calculate compound interest? Give the formula."},
    {"category": "math",          "content": "What is the significance of the number e (Euler's number)?"},
    # Coding
    {"category": "coding",        "content": "What is the difference between a stack and a queue data structure?"},
    {"category": "coding",        "content": "Explain how a binary search tree works."},
    {"category": "coding",        "content": "What is the time complexity of merge sort and why?"},
    {"category": "coding",        "content": "Write a Python function to check if a string is a palindrome."},
    {"category": "coding",        "content": "What is the difference between REST and GraphQL APIs?"},
    # History
    {"category": "history",       "content": "What caused World War I?"},
    {"category": "history",       "content": "Who was Alan Turing and what was his contribution to computing?"},
    {"category": "history",       "content": "What was the significance of the Magna Carta?"},
    # General Knowledge
    {"category": "general",       "content": "What are the three branches of the United States government?"},
    {"category": "general",       "content": "How does the stock market work?"},
    {"category": "general",       "content": "What is the difference between a democracy and a republic?"},
    {"category": "general",       "content": "What causes inflation in an economy?"},
    {"category": "general",       "content": "How does GPS navigation work?"},
    # Language & Literature
    {"category": "language",      "content": "What is the difference between a simile and a metaphor? Give examples."},
    {"category": "language",      "content": "What is the passive voice and when should you avoid it?"},
    {"category": "language",      "content": "What are the key themes in George Orwell's 1984?"},
    # Philosophy & Ethics
    {"category": "philosophy",    "content": "What is the trolley problem and what ethical theories does it illustrate?"},
    {"category": "philosophy",    "content": "Explain Occam's Razor and give a modern example."},
    {"category": "philosophy",    "content": "What is the difference between deductive and inductive reasoning?"},
    # Health & Medicine
    {"category": "health",        "content": "What is the difference between type 1 and type 2 diabetes?"},
    {"category": "health",        "content": "How does the human heart pump blood through the body?"},
    {"category": "health",        "content": "What is the role of sleep in memory consolidation?"},
    # Business & Finance
    {"category": "business",      "content": "What is the difference between revenue and profit?"},
    {"category": "business",      "content": "Explain what a startup's burn rate means."},
    {"category": "business",      "content": "What is venture capital and how does it work?"},
    # Creative Writing
    {"category": "creative",      "content": "Write a haiku about artificial intelligence."},
    {"category": "creative",      "content": "Describe a futuristic city in exactly three sentences."},
    # Short-answer (one-liners expected)
    {"category": "short",         "content": "What year did the Berlin Wall fall?"},
    {"category": "short",         "content": "What element has the chemical symbol Au?"},
    {"category": "short",         "content": "How many planets are in our solar system?"},
    {"category": "short",         "content": "What does DNA stand for?"},
    {"category": "short",         "content": "Who wrote Romeo and Juliet?"},
]

assert len(QUESTIONS) == 50, f"Expected 50 questions, got {len(QUESTIONS)}"


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_env(path: str) -> dict[str, str]:
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def http_post(url: str, payload: dict, api_key: str, timeout: int = 60) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        return e.code, body
    except Exception as exc:
        return 0, str(exc)


def wait_ready(base_url: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ── WAL inspection ───────────────────────────────────────────────────────────

def inspect_wal(wal_path: str) -> dict:
    results: dict = {}
    wal_db = Path(wal_path) / "gateway.db"
    intel_db = Path(wal_path) / "intelligence.db"

    if wal_db.exists():
        conn = sqlite3.connect(str(wal_db))
        conn.row_factory = sqlite3.Row
        try:
            # Execution records
            rows = conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN signature IS NOT NULL THEN 1 ELSE 0 END) as signed, "
                "SUM(CASE WHEN previous_record_id IS NOT NULL THEN 1 ELSE 0 END) as chained "
                "FROM wal_records"
            ).fetchone()
            results["exec_total"] = rows["n"]
            results["exec_signed"] = rows["signed"]
            results["exec_chained"] = rows["chained"]

            # Token sums
            tok = conn.execute(
                "SELECT SUM(json_extract(record_data,'$.usage.prompt_tokens')) as pt, "
                "SUM(json_extract(record_data,'$.usage.completion_tokens')) as ct "
                "FROM wal_records"
            ).fetchone()
            results["total_prompt_tokens"] = tok["pt"] or 0
            results["total_completion_tokens"] = tok["ct"] or 0

            # Completeness attempts
            att = conn.execute(
                "SELECT COUNT(*) as n, "
                "COUNT(DISTINCT disposition) as dispositions "
                "FROM gateway_attempts"
            ).fetchone()
            results["attempts_total"] = att["n"]

            # Provider distribution
            prov = conn.execute(
                "SELECT json_extract(record_data,'$.provider') as p, COUNT(*) as n "
                "FROM wal_records GROUP BY p"
            ).fetchall()
            results["by_provider"] = {r["p"]: r["n"] for r in prov}

            # Sample latest 3 execution IDs
            recent = conn.execute(
                "SELECT record_id FROM wal_records ORDER BY created_at DESC LIMIT 3"
            ).fetchall()
            results["recent_ids"] = [r["record_id"] for r in recent]

        finally:
            conn.close()
    else:
        results["wal_db_missing"] = True

    if intel_db.exists():
        conn = sqlite3.connect(str(intel_db))
        conn.row_factory = sqlite3.Row
        try:
            # Schema mapper verdict distribution
            verdicts = conn.execute(
                "SELECT prediction, COUNT(*) as n "
                "FROM onnx_verdicts WHERE model_name='schema_mapper' "
                "GROUP BY prediction ORDER BY n DESC"
            ).fetchall()
            results["schema_mapper_verdicts"] = {r["prediction"]: r["n"] for r in verdicts}

            total_verdicts = sum(results["schema_mapper_verdicts"].values())
            results["schema_mapper_total_verdicts"] = total_verdicts

            # Overflow (UNKNOWN) rate
            unknown = results["schema_mapper_verdicts"].get("UNKNOWN", 0)
            results["schema_mapper_overflow_rate"] = (
                round(unknown / total_verdicts * 100, 1) if total_verdicts else 0
            )

            # Divergence accuracy (rows with ground truth)
            acc = conn.execute(
                "SELECT COUNT(*) as n, "
                "SUM(CASE WHEN prediction=divergence_signal THEN 1 ELSE 0 END) as correct "
                "FROM onnx_verdicts "
                "WHERE model_name='schema_mapper' AND divergence_signal IS NOT NULL"
            ).fetchone()
            results["schema_mapper_labelled_rows"] = acc["n"]
            results["schema_mapper_labelled_correct"] = acc["correct"]
            results["schema_mapper_accuracy"] = (
                round(acc["correct"] / acc["n"] * 100, 1) if acc["n"] else None
            )

            # Overflow keys breakdown (from divergence source)
            of = conn.execute(
                "SELECT divergence_source, COUNT(*) as n "
                "FROM onnx_verdicts WHERE model_name='schema_mapper' AND divergence_signal IS NOT NULL "
                "GROUP BY divergence_source"
            ).fetchall()
            results["divergence_sources"] = {r["divergence_source"]: r["n"] for r in of}

        finally:
            conn.close()
    else:
        results["intel_db_missing"] = True

    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-url", default=f"http://localhost:{GATEWAY_PORT}")
    parser.add_argument("--key", default="test-key-alpha")
    parser.add_argument("--no-start", action="store_true",
                        help="Skip starting gateway (assume already running)")
    parser.add_argument("--wal-path", default=WAL_PATH)
    args = parser.parse_args()

    base_url = args.gateway_url.rstrip("/")
    gw_proc = None

    # ── Start gateway ────────────────────────────────────────────────────────
    if not args.no_start:
        env_vars = load_env(ENV_FILE)
        env = {**os.environ, **env_vars}
        # Clean WAL for a fresh run
        import shutil
        if Path(args.wal_path).exists():
            shutil.rmtree(args.wal_path)
            print(f"[setup] cleared previous WAL at {args.wal_path}")

        print(f"[setup] starting gateway on port {GATEWAY_PORT} …")
        gw_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "gateway.main:app",
             "--host", "0.0.0.0", "--port", str(GATEWAY_PORT), "--log-level", "warning"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if not wait_ready(base_url, timeout=30):
            print("[ERROR] Gateway did not become ready in 30s")
            if gw_proc:
                gw_proc.terminate()
            sys.exit(1)
        print(f"[setup] gateway ready at {base_url}")

    # ── Fire 50 questions ────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Sending 50 questions → {MODEL}")
    print(f"{'─'*60}")

    successes = 0
    failures = 0
    errors: list[str] = []
    categories_seen: set[str] = set()

    for i, q in enumerate(QUESTIONS, 1):
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": q["content"]}],
            "max_tokens": 300,
        }
        status, body = http_post(
            f"{base_url}/v1/chat/completions", payload, args.key, timeout=60
        )

        cat = q["category"]
        categories_seen.add(cat)

        if status == 200 and isinstance(body, dict) and "choices" in body:
            choice = body["choices"][0]
            content_preview = (
                (choice.get("message", {}).get("content") or "")[:60].replace("\n", " ")
            )
            usage = body.get("usage", {})
            pt = usage.get("prompt_tokens", "?")
            ct = usage.get("completion_tokens", "?")
            finish = choice.get("finish_reason", "?")
            print(
                f"  [{i:02d}/{len(QUESTIONS)}] {cat:<12} OK  "
                f"finish={finish} p={pt} c={ct}  {content_preview!r}"
            )
            successes += 1
        else:
            err_preview = str(body)[:120]
            print(f"  [{i:02d}/{len(QUESTIONS)}] {cat:<12} FAIL HTTP {status}  {err_preview}")
            failures += 1
            errors.append(f"Q{i} ({cat}): HTTP {status} — {err_preview}")

        # Small backoff to avoid 429s
        time.sleep(0.4)

    # ── Wait for async workers to flush verdicts ─────────────────────────────
    print("\n[wait] allowing 5s for verdict flush worker …")
    time.sleep(5)

    # ── Inspect WAL ──────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  WAL + Intelligence DB Analysis")
    print(f"{'─'*60}")

    wal_data = inspect_wal(args.wal_path)

    print(f"\n  Requests sent      : {len(QUESTIONS)}")
    print(f"  HTTP 200 success   : {successes}")
    print(f"  HTTP failures      : {failures}")
    print(f"  Categories tested  : {sorted(categories_seen)}")

    if "wal_db_missing" not in wal_data:
        signed_pct = (
            round(wal_data["exec_signed"] / wal_data["exec_total"] * 100, 1)
            if wal_data["exec_total"] else 0
        )
        chained_pct = (
            round(wal_data["exec_chained"] / wal_data["exec_total"] * 100, 1)
            if wal_data["exec_total"] else 0
        )
        print(f"\n  Execution records  : {wal_data['exec_total']}")
        print(f"  Signed records     : {wal_data['exec_signed']}  ({signed_pct}%)")
        print(f"  Chained records    : {wal_data['exec_chained']}  ({chained_pct}%)")
        print(f"  Gateway attempts   : {wal_data['attempts_total']}")
        print(f"  Prompt tokens      : {wal_data['total_prompt_tokens']:,}")
        print(f"  Completion tokens  : {wal_data['total_completion_tokens']:,}")
        print(f"  By provider        : {wal_data['by_provider']}")
        if wal_data.get("recent_ids"):
            print(f"  Recent exec IDs    : {wal_data['recent_ids']}")
    else:
        print("  [WARN] WAL DB not found — gateway may have used a different path")

    if "intel_db_missing" not in wal_data:
        print(f"\n  Schema mapper total verdicts : {wal_data['schema_mapper_total_verdicts']}")
        print(f"  Overflow (UNKNOWN) rate      : {wal_data['schema_mapper_overflow_rate']}%")
        if wal_data["schema_mapper_labelled_rows"]:
            print(f"  Labelled rows (w/ ground truth): {wal_data['schema_mapper_labelled_rows']}")
            print(f"  Accuracy on labelled rows    : {wal_data['schema_mapper_accuracy']}%")
        else:
            print("  Labelled rows                : 0  (harvester needs overflow → rule match)")
        print("\n  Prediction distribution:")
        for label, count in sorted(
            wal_data["schema_mapper_verdicts"].items(), key=lambda x: -x[1]
        ):
            bar = "█" * min(count, 40)
            print(f"    {label:<25} {count:4d}  {bar}")
        if wal_data.get("divergence_sources"):
            print(f"\n  Divergence sources : {wal_data['divergence_sources']}")
    else:
        print("  [WARN] Intelligence DB not found")

    # ── Failures detail ──────────────────────────────────────────────────────
    if errors:
        print(f"\n  Failed requests:")
        for e in errors:
            print(f"    {e}")

    # ── Final verdict ────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    success_rate = round(successes / len(QUESTIONS) * 100, 1)
    overall = "PASS" if failures == 0 else ("WARN" if failures <= 3 else "FAIL")
    print(f"  RESULT: {overall}  —  {successes}/{len(QUESTIONS)} ({success_rate}%) requests succeeded")

    # Chain integrity
    if "exec_total" in wal_data and wal_data["exec_total"] > 0:
        signed_pct = round(wal_data["exec_signed"] / wal_data["exec_total"] * 100, 1)
        if signed_pct < 100:
            print(f"  WARN: only {signed_pct}% of execution records are signed")
        else:
            print(f"  Chain integrity   : PASS (100% signed)")

    # Schema mapper health
    if "schema_mapper_overflow_rate" in wal_data:
        overflow = wal_data["schema_mapper_overflow_rate"]
        sm_status = "PASS" if overflow < 10 else ("WARN" if overflow < 25 else "FAIL")
        print(f"  Schema mapper     : {sm_status}  ({overflow}% overflow / UNKNOWN rate)")
    print(f"{'─'*60}")

    # ── Teardown ─────────────────────────────────────────────────────────────
    if gw_proc:
        gw_proc.send_signal(signal.SIGTERM)
        try:
            gw_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gw_proc.kill()
        print("\n[teardown] gateway stopped")

    sys.exit(0 if overall != "FAIL" else 1)


if __name__ == "__main__":
    main()
