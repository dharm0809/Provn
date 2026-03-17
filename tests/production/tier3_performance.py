#!/usr/bin/env python3
"""Tier 3 performance baseline — latency and throughput benchmarking.

Run ON the EC2 instance from ~/Gateway:
    python tests/production/tier3_performance.py           # full 30-min sustained
    python tests/production/tier3_performance.py --quick   # 5-min sustained (faster iteration)
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time

import aiohttp

sys.path.insert(0, "tests/production")
from config import CHAT_URL, HEADERS, MODEL, save_artifact

QUICK = "--quick" in sys.argv
SUSTAINED_DURATION = 5 * 60 if QUICK else 30 * 60

PROMPT = "What is the capital of France? One word answer only."
MAX_TOKENS = 5

RESULTS: dict = {
    "tier": "3_performance",
    "model": MODEL,
    "quick_mode": QUICK,
}


async def single_request(session: aiohttp.ClientSession) -> dict:
    start = time.monotonic()
    try:
        async with session.post(CHAT_URL, json={
            "model": MODEL,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": MAX_TOKENS,
        }, headers=HEADERS) as r:
            await r.read()
            latency = time.monotonic() - start
            return {"status": r.status, "latency": latency, "ok": r.status == 200}
    except Exception as e:
        return {"status": 0, "latency": time.monotonic() - start, "ok": False, "error": str(e)}


async def run_concurrent(n: int, count: int) -> dict:
    sem = asyncio.Semaphore(n)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def bounded(_):
            async with sem:
                return await single_request(session)
        results = await asyncio.gather(*[bounded(i) for i in range(count)])

    latencies = sorted(r["latency"] for r in results if r["ok"])
    errors = sum(1 for r in results if not r["ok"])
    if not latencies:
        return {"concurrency": n, "count": count, "error_rate": 1.0,
                "p50": None, "p95": None, "p99": None, "errors": errors}
    return {
        "concurrency": n, "count": count,
        "error_rate": round(errors / count, 4),
        "p50": round(latencies[int(len(latencies) * 0.50)], 3),
        "p95": round(latencies[int(len(latencies) * 0.95)], 3),
        "p99": round(latencies[int(len(latencies) * 0.99)], 3),
        "mean": round(statistics.mean(latencies), 3),
        "errors": errors,
    }


async def run_sustained(concurrency: int, duration: int) -> dict:
    print(f"  Running {duration // 60}-min sustained @ {concurrency} concurrent...")
    start = time.monotonic()
    total = errors = 0
    latencies: list[float] = []

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while time.monotonic() - start < duration:
            batch = await asyncio.gather(*[single_request(session) for _ in range(concurrency)])
            for r in batch:
                total += 1
                if r["ok"]:
                    latencies.append(r["latency"])
                else:
                    errors += 1
            elapsed = time.monotonic() - start
            if total % max(concurrency, 10) == 0:
                print(f"    {elapsed:.0f}s/{duration}s — {total} req, {errors} err")

    latencies.sort()
    return {
        "concurrency": concurrency, "duration_s": duration,
        "total_requests": total,
        "error_rate": round(errors / total, 4) if total else 0,
        "req_per_sec": round(total / duration, 2),
        "p50": round(latencies[int(len(latencies) * 0.50)], 3) if latencies else None,
        "p95": round(latencies[int(len(latencies) * 0.95)], 3) if latencies else None,
        "p99": round(latencies[int(len(latencies) * 0.99)], 3) if latencies else None,
        "errors": errors,
    }


async def main():
    mode = "QUICK (5 min)" if QUICK else "FULL (30 min)"
    print(f"\n=== Tier 3: Performance Baseline [{mode}] ===")
    print(f"  Model: {MODEL} | Prompt: '{PROMPT}'\n")

    # Baseline
    print("[1/4] Baseline (1 concurrent, 10 requests)")
    baseline = await run_concurrent(1, 10)
    RESULTS["baseline"] = baseline
    print(f"  p50={baseline['p50']}s  p95={baseline['p95']}s  p99={baseline['p99']}s  errors={baseline['errors']}")

    # Ramp
    print("\n[2/4] Ramp test")
    ramp_results = []
    saturation_at = None
    for n in [10, 50, 100]:
        print(f"  {n} concurrent × {n * 2} requests...")
        r = await run_concurrent(n, n * 2)
        ramp_results.append(r)
        print(f"    p50={r['p50']}s  p99={r['p99']}s  errors={r['error_rate']:.1%}")
        if r["error_rate"] > 0.05 and saturation_at is None:
            saturation_at = n
            print(f"    *** Saturation at {n} concurrent")
    RESULTS["ramp"] = ramp_results
    RESULTS["saturation_concurrency"] = saturation_at or "100+ (not saturated)"

    # Sustained
    sustained_n = max(5, (saturation_at or 100) // 2)
    print(f"\n[3/4] Sustained load @ {sustained_n} concurrent ({SUSTAINED_DURATION // 60} min)")
    sustained = await run_sustained(sustained_n, SUSTAINED_DURATION)
    RESULTS["sustained"] = sustained
    print(f"  req/s={sustained['req_per_sec']}  p99={sustained['p99']}s  errors={sustained['error_rate']:.1%}")

    # SLA card
    print("\n[4/4] SLA card")
    sla = {
        "model": MODEL,
        "instance": "m6a.xlarge",
        "baseline_p50_s": baseline["p50"],
        "baseline_p99_s": baseline["p99"],
        "max_stable_concurrency": RESULTS["saturation_concurrency"],
        "sustained_req_per_sec": sustained["req_per_sec"],
        "sustained_error_rate": f"{sustained['error_rate']:.2%}",
        "sustained_p99_s": sustained["p99"],
    }
    RESULTS["sla_card"] = sla

    gate = "PASS" if sustained["error_rate"] < 0.01 else "FAIL"
    RESULTS["gate"] = gate

    print(f"\n{'='*40}")
    print("Performance SLA Card:")
    for k, v in sla.items():
        print(f"  {k}: {v}")

    save_artifact("tier3_performance", RESULTS)
    save_artifact("sla_card", sla)

    if gate == "FAIL":
        print("\nGATE FAILED — sustained error rate > 1%")
        sys.exit(1)
    print("\nGATE PASSED")


if __name__ == "__main__":
    asyncio.run(main())
