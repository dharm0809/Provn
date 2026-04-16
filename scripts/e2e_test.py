#!/usr/bin/env python3
"""End-to-end test harness for TruzenAI Gateway.

Simulates real OpenWebUI traffic patterns and validates every pipeline layer:
  - Prompt extraction & intent classification
  - Response normalization & thinking strip
  - Schema validation & SchemaMapper
  - Content safety (PII, toxicity, safety classifier)
  - Session chain integrity
  - Anomaly detection
  - Consistency tracking
  - File/image tracking
  - Compliance readiness
  - Multi-model support

Usage:
    python scripts/e2e_test.py                          # local (localhost:8000)
    python scripts/e2e_test.py --url http://35.165.21.8:8100 --key dharm-key-2026
    python scripts/e2e_test.py --url http://localhost:8100 --key dharm-key-2026 --model gemma3:1b
"""

import argparse
import base64
import json
import sys
import time
import uuid
import urllib.request
import urllib.error

# ── Config ───────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
WARN = 0
RESULTS = []


def req(base_url, path, data=None, method="GET", api_key="", timeout=120, headers=None):
    """Make an HTTP request and return parsed JSON."""
    url = f"{base_url}{path}"
    hdrs = {"Content-Type": "application/json"}
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:500]}
    except Exception as e:
        return {"_error": str(e)}


def req_text(base_url, path, timeout=10):
    url = f"{base_url}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode()


def check(name, condition, detail=""):
    global PASS, FAIL
    ok = bool(condition)
    PASS += ok
    FAIL += not ok
    icon = "PASS" if ok else "FAIL"
    RESULTS.append({"name": name, "ok": ok, "detail": detail})
    print(f"  {icon:4s} {name}" + (f" -- {detail}" if detail else ""))


def warn(name, detail=""):
    global WARN
    WARN += 1
    RESULTS.append({"name": name, "ok": True, "detail": f"WARN: {detail}"})
    print(f"  WARN {name} -- {detail}")


def chat(base_url, api_key, model, messages, session_id=None, stream=False, extra_headers=None):
    """Send a chat completion request."""
    hdrs = {}
    if session_id:
        hdrs["X-Session-Id"] = session_id
    if extra_headers:
        hdrs.update(extra_headers)
    return req(base_url, "/v1/chat/completions", {
        "model": model, "messages": messages, "stream": stream,
    }, "POST", api_key, headers=hdrs)


# ── Test Suites ──────────────────────────────────────────────────────────────

def test_health(base_url):
    print("\n=== 1. GATEWAY HEALTH ===")
    h = req(base_url, "/health")
    check("Gateway healthy", h.get("status") == "healthy")
    check("WAL operational", h.get("wal", {}).get("pending_records") is not None)
    check("Content analyzers loaded", h.get("content_analyzers", 0) >= 1, f"count={h.get('content_analyzers')}")
    check("Session chain active", "session_chain" in h)
    return h


def test_single_turn(base_url, api_key, model):
    print(f"\n=== 2. SINGLE TURN ({model}) ===")
    r = chat(base_url, api_key, model, [{"role": "user", "content": "What is 2+2? One word answer."}])
    check("Response OK", "choices" in r, f"model={r.get('model')}")
    if "choices" in r:
        msg = r["choices"][0]["message"]
        check("Content not empty", bool(msg.get("content")))
        check("No <think> in content", "<think>" not in (msg.get("content") or ""))
        usage = r.get("usage", {})
        check("prompt_tokens > 0", (usage.get("prompt_tokens") or 0) > 0)
        check("completion_tokens > 0", (usage.get("completion_tokens") or 0) > 0)
        check("total = prompt + completion",
              usage.get("total_tokens") == (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)))
    return r


def test_multi_turn(base_url, api_key, model):
    print(f"\n=== 3. MULTI-TURN SESSION ({model}) ===")
    sid = f"e2e-multi-{uuid.uuid4().hex[:8]}"

    # Turn 1
    r1 = chat(base_url, api_key, model,
              [{"role": "user", "content": "Name a color"}], session_id=sid)
    check("Turn 1 OK", "choices" in r1)

    # Turn 2
    r2 = chat(base_url, api_key, model, [
        {"role": "user", "content": "Name a color"},
        {"role": "assistant", "content": r1.get("choices", [{}])[0].get("message", {}).get("content", "Blue")},
        {"role": "user", "content": "Name a different one"},
    ], session_id=sid)
    check("Turn 2 OK", "choices" in r2)

    # Check lineage
    time.sleep(2)
    sess = req(base_url, f"/v1/lineage/sessions/{sid}")
    records = sess.get("records", [])
    check("Session has 2 records", len(records) == 2, f"got={len(records)}")
    if len(records) >= 2:
        check("Records share session_id", all(r.get("session_id") == sid for r in records))

    return sid


def test_system_task_detection(base_url, api_key, model):
    print(f"\n=== 4. SYSTEM TASK DETECTION ({model}) ===")
    # Simulate OpenWebUI tag generation
    r = chat(base_url, api_key, model, [{"role": "user", "content":
        '### Task:\nGenerate 1-3 tags.\n### Chat History:\n<chat_history>\n'
        'USER: hello\nASSISTANT: hi\n</chat_history>\n### Output:\n'
        'JSON: { "tags": ["General"] }'}])
    check("System task response OK", "choices" in r)

    # Check lineage — system task should appear in recent sessions
    time.sleep(2)
    sessions = req(base_url, "/v1/lineage/sessions?limit=20&sort=last_activity&order=desc")
    found_systask = False
    for s in sessions.get("sessions", []):
        q = s.get("user_question") or ""
        rt = s.get("request_type") or ""
        if rt.startswith("system_task") or "### Task:" in q:
            found_systask = True
            break
    check("System task classified", found_systask)


def test_thinking_strip(base_url, api_key, model):
    print(f"\n=== 5. THINKING STRIP ({model}) ===")
    r = chat(base_url, api_key, model, [{"role": "user", "content": "Step by step: 3*4. One number."}])
    check("Response OK", "choices" in r)
    if "choices" in r:
        msg = r["choices"][0]["message"]
        content = msg.get("content", "")
        reasoning = msg.get("reasoning", "")
        check("Content clean (no <think>)", "<think>" not in content)
        if reasoning:
            check("Reasoning captured separately", len(reasoning) > 0, f"len={len(reasoning)}")
        else:
            warn("No reasoning field", "Model may not support thinking mode")


def test_content_safety(base_url, api_key, model):
    print(f"\n=== 6. CONTENT SAFETY ===")
    # Safe prompt
    r_safe = chat(base_url, api_key, model, [{"role": "user", "content": "What is photosynthesis?"}])
    check("Safe prompt accepted", "choices" in r_safe)

    # Check lineage for content analysis
    time.sleep(2)
    sessions = req(base_url, "/v1/lineage/sessions?limit=3&sort=last_activity&order=desc")
    if sessions.get("sessions"):
        sid = sessions["sessions"][0]["session_id"]
        detail = req(base_url, f"/v1/lineage/sessions/{sid}")
        recs = detail.get("records", [])
        if recs:
            meta = recs[-1].get("metadata", {})
            decisions = meta.get("analyzer_decisions", [])
            check("Content analysis ran", len(decisions) > 0, f"analyzers={len(decisions)}")
            # Check safety classifier specifically
            safety_found = any(d.get("analyzer_id", "").startswith("truzenai.safety") for d in decisions)
            check("Safety classifier ran", safety_found)
            pii_found = any(d.get("analyzer_id", "").startswith("walacor.pii") for d in decisions)
            check("PII detector ran", pii_found)


def test_pii_detection(base_url, api_key, model):
    print(f"\n=== 7. PII DETECTION ===")
    r = chat(base_url, api_key, model, [{"role": "user", "content":
        "My credit card number is 4111-1111-1111-1111 and my SSN is 123-45-6789. Is this safe to share?"}])
    # PII may be blocked (no choices) or warned (choices present) — both are correct
    check("PII prompt handled", "choices" in r or r.get("_error") in (400, 403) or "blocked" in str(r).lower(),
          "blocked" if "choices" not in r else "allowed with warning")

    # Check if PII was flagged
    time.sleep(2)
    sessions = req(base_url, "/v1/lineage/sessions?limit=3&sort=last_activity&order=desc")
    if sessions.get("sessions"):
        sid = sessions["sessions"][0]["session_id"]
        detail = req(base_url, f"/v1/lineage/sessions/{sid}")
        recs = detail.get("records", [])
        if recs:
            meta = recs[-1].get("metadata", {})
            decisions = meta.get("analyzer_decisions", [])
            pii_decision = [d for d in decisions if "pii" in d.get("analyzer_id", "").lower()]
            if pii_decision:
                verdict = pii_decision[0].get("verdict", "")
                check("PII detected", verdict in ("warn", "block"), f"verdict={verdict}")
            else:
                warn("PII analyzer not in decisions")


def test_prompt_extraction(base_url, api_key, model):
    print(f"\n=== 8. PROMPT EXTRACTION ===")
    sid = f"e2e-extract-{uuid.uuid4().hex[:8]}"
    # Multi-turn with system prompt
    r = chat(base_url, api_key, model, [
        {"role": "system", "content": "You are a helpful math tutor."},
        {"role": "user", "content": "What is algebra?"},
        {"role": "assistant", "content": "Algebra is a branch of mathematics."},
        {"role": "user", "content": "Give me a simple example"},
    ], session_id=sid)
    check("Multi-turn response OK", "choices" in r)

    time.sleep(2)
    sess = req(base_url, f"/v1/lineage/sessions/{sid}")
    recs = sess.get("records", [])
    if recs:
        rec = recs[-1]
        prompt = rec.get("prompt_text", "")
        check("Prompt is actual question (not full convo)",
              "Give me a simple example" in prompt and "What is algebra" not in prompt,
              f"prompt={prompt[:60]}")
        meta = rec.get("metadata", {})
        audit = meta.get("walacor_audit", {})
        check("extraction_method present", audit.get("extraction_method") in
              ("single_turn", "last_user_message", "system_prompt_only", "fallback"),
              f"method={audit.get('extraction_method')}")
        check("conversation_turns = 2", audit.get("conversation_turns") == 2,
              f"turns={audit.get('conversation_turns')}")


def test_multiple_models(base_url, api_key, models):
    print(f"\n=== 9. MULTIPLE MODELS ===")
    for m in models:
        r = chat(base_url, api_key, m, [{"role": "user", "content": "Say hi"}])
        ok = "choices" in r
        check(f"Model {m} responds", ok, f"error={r.get('_error')}" if not ok else "")


def test_lineage_api(base_url):
    print(f"\n=== 10. LINEAGE API ===")
    sessions = req(base_url, "/v1/lineage/sessions?limit=5")
    check("Sessions endpoint OK", "sessions" in sessions, f"total={sessions.get('total')}")

    attempts = req(base_url, "/v1/lineage/attempts?limit=5")
    check("Attempts endpoint OK", "attempts" in attempts or attempts.get("total", 0) > 0,
          f"total={attempts.get('total')}")

    if sessions.get("sessions"):
        s = sessions["sessions"][0]
        check("Session has user_question", bool(s.get("user_question")),
              f"q={str(s.get('user_question',''))[:40]}")
        check("Session has model", bool(s.get("model")))
        check("Session has user", bool(s.get("user")))


def test_consistency(base_url, api_key, model):
    print(f"\n=== 11. CONSISTENCY TRACKING ===")
    # Send similar questions in different sessions
    q = "What is the capital of France?"
    r1 = chat(base_url, api_key, model,
              [{"role": "user", "content": q}],
              session_id=f"e2e-con1-{uuid.uuid4().hex[:8]}")
    r2 = chat(base_url, api_key, model,
              [{"role": "user", "content": "Tell me the capital of France"}],
              session_id=f"e2e-con2-{uuid.uuid4().hex[:8]}")
    check("Consistency query 1 OK", "choices" in r1)
    check("Consistency query 2 OK", "choices" in r2)
    # Consistency results will accumulate — checked via control status below


def test_compliance(base_url):
    print(f"\n=== 12. COMPLIANCE ===")
    # Use recent date range to ensure data exists
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    r = req(base_url, f"/v1/compliance/export?format=json&framework=eu_ai_act&start={start}&end={end}")
    check("Compliance API OK", "report" in r)
    ar = r.get("audit_readiness")
    if ar:
        check("Audit readiness score present", ar.get("score") is not None, f"score={ar.get('score')}")
        check("Dimensions present", len(ar.get("dimensions", [])) >= 5)
        check("Gaps/recommendations present", "gaps" in ar)
    else:
        warn("No audit_readiness in response")


def test_metrics(base_url):
    print(f"\n=== 13. METRICS ===")
    try:
        text = req_text(base_url, "/metrics")
        check("Prometheus metrics accessible", len(text) > 100)
        check("Has request metrics", "request" in text.lower())
    except Exception as e:
        check("Metrics endpoint", False, str(e))


def test_dashboard(base_url):
    print(f"\n=== 14. DASHBOARD ===")
    try:
        text = req_text(base_url, "/lineage/")
        check("Dashboard loads", "<html" in text.lower() or "<!doctype" in text.lower())
        check("TruzenAI branding", "TruzenAI" in text or "truzenai" in text.lower() or "truzen" in text.lower())
    except Exception as e:
        check("Dashboard", False, str(e))


def test_control_status(base_url, api_key):
    print(f"\n=== 15. CONTROL PLANE STATUS ===")
    r = req(base_url, "/v1/control/status", api_key=api_key,
            headers={"X-API-Key": api_key})
    if r.get("_error"):
        # Try with X-API-Key header directly
        url = f"{base_url}/v1/control/status"
        hdrs = {"X-API-Key": api_key}
        rq = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(rq, timeout=10) as resp:
                r = json.loads(resp.read())
        except Exception as e:
            check("Control status accessible", False, str(e))
            return

    check("Control status OK", "gateway_id" in r)

    # ONNX models
    onnx = r.get("onnx_models", [])
    check("ONNX models reported", len(onnx) >= 1, f"count={len(onnx)}")
    for m in onnx:
        check(f"  {m['name']} loaded", m.get("loaded"), f"type={m.get('type')}")

    # Intelligence
    intel = r.get("intelligence", {})
    if intel:
        check("Anomaly detector active", "anomaly_detector" in intel)
        check("Consistency tracker active", "consistency_tracker" in intel)
        check("Field registry active", "field_registry" in intel)
        if intel.get("background_worker"):
            check("LLM worker running", intel["background_worker"].get("running"))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TruzenAI Gateway E2E Test")
    parser.add_argument("--url", default="http://localhost:8000", help="Gateway URL")
    parser.add_argument("--key", default="test-key-alpha", help="API key")
    parser.add_argument("--model", default="gemma3:1b", help="Primary model")
    parser.add_argument("--models", default="", help="Comma-separated models to test")
    parser.add_argument("--skip-slow", action="store_true", help="Skip slow tests")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else [args.model]

    print(f"TruzenAI Gateway E2E Test")
    print(f"URL: {base}")
    print(f"Model: {args.model}")
    print(f"Models: {models}")
    print(f"{'=' * 60}")

    t_start = time.time()

    # Run all tests
    test_health(base)
    test_single_turn(base, args.key, args.model)
    test_multi_turn(base, args.key, args.model)
    test_system_task_detection(base, args.key, args.model)
    test_thinking_strip(base, args.key, args.model)
    test_content_safety(base, args.key, args.model)
    test_pii_detection(base, args.key, args.model)

    if not args.skip_slow:
        test_prompt_extraction(base, args.key, args.model)
        test_consistency(base, args.key, args.model)

    if len(models) > 1:
        test_multiple_models(base, args.key, models)

    test_lineage_api(base)
    test_compliance(base)
    test_metrics(base)
    test_dashboard(base)
    test_control_status(base, args.key)

    # Summary
    elapsed = time.time() - t_start
    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {PASS}/{total} passed ({PASS/total*100:.0f}%), {FAIL} failed, {WARN} warnings")
    print(f"Time: {elapsed:.1f}s")

    if FAIL > 0:
        print(f"\nFAILED:")
        for r in RESULTS:
            if not r["ok"]:
                print(f"  {r['name']}: {r['detail']}")

    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
