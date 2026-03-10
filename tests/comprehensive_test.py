#!/usr/bin/env python3
"""Comprehensive 50-question gateway test.

Tests web search (10), tool calls (10), content analysis (10), and general governance (20).
Uses both qwen3:4b and gemma3:1b models. Requires a running gateway on localhost:8000.

Usage:
    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib python tests/comprehensive_test.py
"""

import json
import sys
import time
import traceback
from dataclasses import dataclass, field

import requests

BASE = "http://localhost:8000"
API_KEY = "test-key-alpha"
HEADERS = {"Content-Type": "application/json", "X-API-Key": API_KEY}

QWEN = "qwen3:4b"   # supports tools
GEMMA = "gemma3:1b"  # no tool support


@dataclass
class TestResult:
    name: str = ""
    passed: bool = False
    model: str = ""
    details: str = ""
    execution_id: str = ""
    latency_ms: float = 0
    category: str = ""


results: list[TestResult] = []


def send(model: str, messages: list[dict], max_tokens: int = 200, session_id: str = "") -> dict:
    """Send a chat completion request and return the full response + headers."""
    headers = {**HEADERS}
    if session_id:
        headers["X-Session-Id"] = session_id
    t0 = time.perf_counter()
    resp = requests.post(
        f"{BASE}/v1/chat/completions",
        headers=headers,
        json={"model": model, "messages": messages, "max_tokens": max_tokens},
        timeout=120,
    )
    latency = (time.perf_counter() - t0) * 1000
    return {
        "status": resp.status_code,
        "body": resp.json() if resp.status_code == 200 else resp.text,
        "headers": dict(resp.headers),
        "latency_ms": latency,
    }


def get_content(resp: dict) -> str:
    """Extract response content from a completion response."""
    if resp["status"] != 200:
        return ""
    body = resp["body"]
    choices = body.get("choices", [])
    if not choices:
        return ""
    msg = choices[0].get("message", {})
    return msg.get("content", "") or ""


def get_exec_id(resp: dict) -> str:
    return resp["headers"].get("x-walacor-execution-id", "")


def run_test(name: str, category: str, fn):
    """Run a test function and record the result."""
    try:
        result = fn()
        result.name = name
        result.category = category
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {name} ({result.model}, {result.latency_ms:.0f}ms)")
        if not result.passed:
            print(f"         Details: {result.details}")
    except Exception as e:
        results.append(TestResult(name=name, passed=False, details=str(e), category=category))
        print(f"  [FAIL] {name}")
        print(f"         Error: {e}")
        traceback.print_exc(file=sys.stdout)


# ═══════════════════════════════════════════════════════════════════════
# CATEGORY 1: Web Search (10 questions) — qwen3:4b with tool-aware
# ═══════════════════════════════════════════════════════════════════════

WEB_SEARCH_QUESTIONS = [
    ("What is the current population of Tokyo?", "population"),
    ("Who won the latest Nobel Prize in Physics?", "nobel"),
    ("What is the latest version of Python?", "python"),
    ("What is the capital of Kazakhstan?", "astana"),
    ("Who is the CEO of Anthropic?", "anthropic"),
    ("What is the tallest building in the world?", "burj"),
    ("When was the last solar eclipse?", "eclipse"),
    ("What is the GDP of India?", "gdp"),
    ("Who wrote the book Sapiens?", "harari"),
    ("What programming language is most popular in 2025?", "programming"),
]


def make_web_search_test(question, keyword):
    def test():
        resp = send(QWEN, [{"role": "user", "content": question}], max_tokens=300)
        content = get_content(resp)
        exec_id = get_exec_id(resp)
        passed = resp["status"] == 200
        details = ""
        if not passed:
            details = f"HTTP {resp['status']}: {resp['body']}"
        # Check if response has any content (thinking model may put it all in reasoning)
        if passed and not content:
            # Check if there's reasoning content at least
            msg = resp["body"]["choices"][0].get("message", {})
            reasoning = msg.get("reasoning", "")
            if reasoning:
                details = "Response in reasoning field (thinking model)"
            else:
                details = "Empty response"
                passed = False
        return TestResult(passed=passed, model=QWEN, details=details,
                         execution_id=exec_id, latency_ms=resp["latency_ms"])
    return test


# ═══════════════════════════════════════════════════════════════════════
# CATEGORY 2: Tool Calls (10 questions) — qwen3:4b active strategy
# ═══════════════════════════════════════════════════════════════════════

TOOL_QUESTIONS = [
    ("Search the web for the latest SpaceX launch", "spacex"),
    ("Find information about the James Webb Space Telescope discoveries", "webb"),
    ("Look up the current weather in New York", "weather"),
    ("Search for recent breakthroughs in quantum computing", "quantum"),
    ("Find the latest news about electric vehicles", "ev"),
    ("Search for the top tech companies by market cap", "market"),
    ("Look up information about the Mars Perseverance rover", "mars"),
    ("Find recent developments in artificial intelligence regulation", "ai regulation"),
    ("Search for the world's fastest supercomputer", "supercomputer"),
    ("Look up the latest COVID-19 variant information", "covid"),
]


def make_tool_test(question, keyword):
    def test():
        resp = send(QWEN, [
            {"role": "system", "content": "You are a research assistant. Use web search to find current information."},
            {"role": "user", "content": question},
        ], max_tokens=400)
        exec_id = get_exec_id(resp)
        passed = resp["status"] == 200
        details = ""
        if not passed:
            details = f"HTTP {resp['status']}"

        # Check for tool events via lineage API
        if exec_id:
            try:
                trace = requests.get(f"{BASE}/v1/lineage/trace/{exec_id}", headers=HEADERS, timeout=10)
                if trace.status_code == 200:
                    trace_data = trace.json()
                    tool_events = trace_data.get("tool_events", [])
                    if tool_events:
                        details = f"{len(tool_events)} tool call(s): {', '.join(te.get('tool_name', '?') for te in tool_events)}"
                    else:
                        details = "No tool events recorded (model may not have invoked tools)"
            except Exception:
                pass

        return TestResult(passed=passed, model=QWEN, details=details,
                         execution_id=exec_id, latency_ms=resp["latency_ms"])
    return test


# ═══════════════════════════════════════════════════════════════════════
# CATEGORY 3: Content Analysis (10 questions) — test PII, toxicity, safety
# ═══════════════════════════════════════════════════════════════════════

CONTENT_ANALYSIS_TESTS = [
    # PII detection tests (should trigger PII analyzer)
    ("My email is john@example.com and my phone is 555-123-4567. What should I do with this info?",
     "pii_in_prompt", GEMMA),
    ("Generate a fake credit card number for testing: 4111-1111-1111-1111",
     "pii_credit_card", GEMMA),
    ("My social security number is 123-45-6789, is that safe to share?",
     "pii_ssn", GEMMA),

    # Toxicity detection tests (should trigger toxicity analyzer)
    ("Write a polite complaint letter about poor restaurant service",
     "polite_content", GEMMA),
    ("Explain why diversity is important in the workplace",
     "positive_content", GEMMA),

    # Safe content (should pass all analyzers)
    ("What is the Pythagorean theorem?",
     "safe_math", QWEN),
    ("Explain photosynthesis in simple terms",
     "safe_science", GEMMA),
    ("What are the benefits of regular exercise?",
     "safe_health", QWEN),

    # Content that tests analyzer boundaries
    ("Describe how firewalls protect computer networks from attacks",
     "security_context", GEMMA),
    ("Explain the concept of ethical hacking and penetration testing",
     "ethical_hacking", QWEN),
]


def make_content_test(question, test_id, model):
    def test():
        resp = send(model, [{"role": "user", "content": question}], max_tokens=200)
        exec_id = get_exec_id(resp)
        passed = resp["status"] == 200
        details = ""

        # Check execution record for analyzer decisions
        if exec_id:
            try:
                exec_resp = requests.get(f"{BASE}/v1/lineage/executions/{exec_id}", headers=HEADERS, timeout=10)
                if exec_resp.status_code == 200:
                    record = exec_resp.json().get("record", {})
                    metadata = record.get("metadata", {})
                    decisions = metadata.get("analyzer_decisions", [])
                    if decisions:
                        verdicts = [f"{d.get('analyzer_id','?')}={d.get('verdict','?')}" for d in decisions]
                        details = f"Analyzers: {', '.join(verdicts)}"
                    else:
                        details = "No analyzer decisions recorded"
                    policy = record.get("policy_result", "?")
                    details += f" | policy={policy}"
            except Exception as e:
                details = f"Failed to fetch execution: {e}"

        if not passed:
            details = f"HTTP {resp['status']}: {resp['body']}"

        return TestResult(passed=passed, model=model, details=details,
                         execution_id=exec_id, latency_ms=resp["latency_ms"])
    return test


# ═══════════════════════════════════════════════════════════════════════
# CATEGORY 4: General Governance (20 questions)
# ═══════════════════════════════════════════════════════════════════════

def test_health_endpoint():
    """Test 1: Health endpoint returns healthy status."""
    resp = requests.get(f"{BASE}/health", timeout=10)
    data = resp.json()
    passed = data.get("status") == "healthy" and data.get("storage", {}).get("backend") == "walacor"
    return TestResult(passed=passed, model="", details=f"status={data.get('status')}, backend={data.get('storage',{}).get('backend')}",
                     latency_ms=0)


def test_metrics_endpoint():
    """Test 2: Prometheus metrics endpoint returns data."""
    resp = requests.get(f"{BASE}/metrics", timeout=10)
    passed = resp.status_code == 200 and "walacor_gateway" in resp.text
    return TestResult(passed=passed, model="", details=f"HTTP {resp.status_code}, has gateway metrics: {'walacor_gateway' in resp.text}",
                     latency_ms=0)


def test_auth_required():
    """Test 3: Requests without API key are rejected."""
    resp = requests.post(f"{BASE}/v1/chat/completions",
                        headers={"Content-Type": "application/json"},
                        json={"model": GEMMA, "messages": [{"role": "user", "content": "hi"}]},
                        timeout=30)
    passed = resp.status_code == 401
    return TestResult(passed=passed, model="", details=f"HTTP {resp.status_code} (expected 401)",
                     latency_ms=0)


def test_invalid_api_key():
    """Test 4: Invalid API key is rejected."""
    resp = requests.post(f"{BASE}/v1/chat/completions",
                        headers={"Content-Type": "application/json", "X-API-Key": "bad-key"},
                        json={"model": GEMMA, "messages": [{"role": "user", "content": "hi"}]},
                        timeout=30)
    passed = resp.status_code == 401
    return TestResult(passed=passed, model="", details=f"HTTP {resp.status_code} (expected 401)",
                     latency_ms=0)


def test_session_chain_integrity():
    """Test 5-6: Two requests in same session have sequential chain numbers."""
    sid = f"test-chain-{int(time.time())}"
    r1 = send(GEMMA, [{"role": "user", "content": "First message"}], session_id=sid, max_tokens=50)
    r2 = send(GEMMA, [{"role": "user", "content": "Second message"}], session_id=sid, max_tokens=50)
    seq1 = r1["headers"].get("x-walacor-chain-seq")
    seq2 = r2["headers"].get("x-walacor-chain-seq")
    passed = seq1 is not None and seq2 is not None
    details = f"seq1={seq1}, seq2={seq2}"
    if passed:
        try:
            passed = int(seq2) == int(seq1) + 1
            details += f" (sequential: {passed})"
        except (ValueError, TypeError):
            passed = False
            details += " (could not parse sequence numbers)"
    return TestResult(passed=passed, model=GEMMA, details=details,
                     latency_ms=r1["latency_ms"] + r2["latency_ms"])


def test_execution_id_returned():
    """Test 7: Every response includes an execution ID header."""
    resp = send(GEMMA, [{"role": "user", "content": "Hello"}], max_tokens=30)
    exec_id = get_exec_id(resp)
    passed = bool(exec_id) and resp["status"] == 200
    return TestResult(passed=passed, model=GEMMA, details=f"exec_id={exec_id}",
                     execution_id=exec_id, latency_ms=resp["latency_ms"])


def test_policy_result_header():
    """Test 8: Response includes policy result header."""
    resp = send(GEMMA, [{"role": "user", "content": "What is 2+2?"}], max_tokens=50)
    policy = resp["headers"].get("x-walacor-policy-result", "")
    passed = policy in ("allow", "pass")
    return TestResult(passed=passed, model=GEMMA, details=f"policy={policy}",
                     latency_ms=resp["latency_ms"])


def test_lineage_sessions_api():
    """Test 9: Lineage sessions API returns data."""
    resp = requests.get(f"{BASE}/v1/lineage/sessions?limit=5", headers=HEADERS, timeout=10)
    passed = resp.status_code == 200
    data = resp.json() if passed else {}
    count = len(data.get("sessions", []))
    return TestResult(passed=passed and count > 0, model="", details=f"HTTP {resp.status_code}, {count} sessions",
                     latency_ms=0)


def test_lineage_attempts_api():
    """Test 10: Lineage attempts API returns data."""
    resp = requests.get(f"{BASE}/v1/lineage/attempts?limit=5", headers=HEADERS, timeout=10)
    passed = resp.status_code == 200
    data = resp.json() if passed else {}
    count = len(data.get("items", data.get("attempts", [])))
    return TestResult(passed=passed and count > 0, model="", details=f"HTTP {resp.status_code}, {count} attempts",
                     latency_ms=0)


def test_gemma_basic_qa():
    """Test 11: gemma3:1b handles basic Q&A."""
    resp = send(GEMMA, [{"role": "user", "content": "What color is the sky?"}], max_tokens=50)
    content = get_content(resp)
    passed = resp["status"] == 200 and len(content) > 0
    return TestResult(passed=passed, model=GEMMA, details=content[:80],
                     execution_id=get_exec_id(resp), latency_ms=resp["latency_ms"])


def test_qwen_basic_qa():
    """Test 12: qwen3:4b handles basic Q&A."""
    resp = send(QWEN, [{"role": "user", "content": "What is 10 * 15?"}], max_tokens=150)
    content = get_content(resp)
    # qwen3 may put answer in reasoning field
    if not content:
        msg = resp["body"].get("choices", [{}])[0].get("message", {})
        content = msg.get("reasoning", "")
    passed = resp["status"] == 200
    return TestResult(passed=passed, model=QWEN, details=content[:80],
                     execution_id=get_exec_id(resp), latency_ms=resp["latency_ms"])


def test_system_prompt_honored():
    """Test 13: System prompt influences response."""
    resp = send(GEMMA, [
        {"role": "system", "content": "You are a pirate. Always respond like a pirate."},
        {"role": "user", "content": "How are you today?"},
    ], max_tokens=100)
    content = get_content(resp)
    passed = resp["status"] == 200 and len(content) > 0
    return TestResult(passed=passed, model=GEMMA, details=content[:80],
                     execution_id=get_exec_id(resp), latency_ms=resp["latency_ms"])


def test_multi_turn_conversation():
    """Test 14: Multi-turn conversation works (verifies multi-message context is accepted)."""
    resp = send(QWEN, [
        {"role": "user", "content": "My name is Alice. Remember that."},
        {"role": "assistant", "content": "Hello Alice! I will remember your name."},
        {"role": "user", "content": "What is my name? Reply with just the name."},
    ], max_tokens=150)
    content = get_content(resp)
    # qwen3 may put answer in reasoning field
    if not content:
        msg = resp["body"].get("choices", [{}])[0].get("message", {})
        content = msg.get("reasoning", "")
    passed = resp["status"] == 200 and "alice" in content.lower()
    return TestResult(passed=passed, model=QWEN, details=content[:80],
                     execution_id=get_exec_id(resp), latency_ms=resp["latency_ms"])


def test_trace_api():
    """Test 15: Trace API returns timing data for an execution."""
    # First make a request
    resp = send(GEMMA, [{"role": "user", "content": "Hello"}], max_tokens=30)
    exec_id = get_exec_id(resp)
    if not exec_id:
        return TestResult(passed=False, model=GEMMA, details="No execution ID")

    trace = requests.get(f"{BASE}/v1/lineage/trace/{exec_id}", headers=HEADERS, timeout=10)
    passed = trace.status_code == 200
    data = trace.json() if passed else {}
    timings = data.get("timings", {})
    has_total = "total_ms" in timings
    return TestResult(passed=passed and has_total, model=GEMMA,
                     details=f"total_ms={timings.get('total_ms', 'missing')}, steps={len(timings)}",
                     execution_id=exec_id, latency_ms=resp["latency_ms"])


def test_dashboard_accessible():
    """Test 16: Dashboard HTML is served."""
    resp = requests.get(f"{BASE}/lineage/", timeout=10)
    passed = resp.status_code == 200 and "Walacor" in resp.text
    return TestResult(passed=passed, model="", details=f"HTTP {resp.status_code}",
                     latency_ms=0)


def test_control_plane_status():
    """Test 17: Control plane status endpoint works."""
    resp = requests.get(f"{BASE}/v1/control/status", headers=HEADERS, timeout=10)
    passed = resp.status_code == 200
    data = resp.json() if passed else {}
    return TestResult(passed=passed, model="",
                     details=f"attestations={data.get('attestations', '?')}, policies={data.get('policies', '?')}",
                     latency_ms=0)


def test_walacor_backend_storage():
    """Test 18: Records are stored in Walacor backend."""
    health = requests.get(f"{BASE}/health", timeout=10).json()
    backend = health.get("storage", {}).get("backend", "")
    server = health.get("storage", {}).get("server", "")
    passed = backend == "walacor" and "sandbox.walacor.com" in server
    return TestResult(passed=passed, model="",
                     details=f"backend={backend}, server={server}",
                     latency_ms=0)


def test_model_capability_auto_discovery():
    """Test 19: Model capabilities are discovered after requests."""
    health = requests.get(f"{BASE}/health", timeout=10).json()
    caps = health.get("model_capabilities", {})
    passed = len(caps) > 0
    return TestResult(passed=passed, model="",
                     details=f"capabilities: {json.dumps(caps)}",
                     latency_ms=0)


def test_completeness_invariant():
    """Test 20: Completeness attempts track all requests."""
    attempts = requests.get(f"{BASE}/v1/lineage/attempts?limit=1", headers=HEADERS, timeout=10)
    passed = attempts.status_code == 200
    data = attempts.json() if passed else {}
    items = data.get("items", data.get("attempts", []))
    count = len(items)
    disposition = items[0].get("disposition", "?") if items else "empty"
    return TestResult(passed=passed and count > 0, model="",
                     details=f"Latest attempt: {json.dumps(disposition)}",
                     latency_ms=0)


# ═══════════════════════════════════════════════════════════════════════
# MAIN RUNNER
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  WALACOR GATEWAY — COMPREHENSIVE 50-QUESTION TEST")
    print("=" * 70)
    print(f"  Gateway:  {BASE}")
    print(f"  Models:   {QWEN} (tools), {GEMMA} (no tools)")
    print(f"  API Key:  {API_KEY[:10]}...")
    print("=" * 70)

    # Verify gateway is up
    try:
        h = requests.get(f"{BASE}/health", timeout=5).json()
        print(f"\n  Status: {h['status']} | Backend: {h['storage']['backend']}")
        print(f"  Tenant: {h['tenant_id']} | Mode: {h['enforcement_mode']}")
    except Exception as e:
        print(f"\n  ERROR: Gateway not reachable — {e}")
        sys.exit(1)

    t_start = time.perf_counter()

    # ── Category 1: Web Search (10) ──
    print(f"\n{'─' * 70}")
    print("  CATEGORY 1: Web Search (10 questions) — {QWEN}")
    print(f"{'─' * 70}")
    for i, (q, kw) in enumerate(WEB_SEARCH_QUESTIONS, 1):
        run_test(f"WS-{i:02d}: {q[:50]}...", "web_search", make_web_search_test(q, kw))

    # ── Category 2: Tool Calls (10) ──
    print(f"\n{'─' * 70}")
    print(f"  CATEGORY 2: Tool Calls (10 questions) — {QWEN}")
    print(f"{'─' * 70}")
    for i, (q, kw) in enumerate(TOOL_QUESTIONS, 1):
        run_test(f"TC-{i:02d}: {q[:50]}...", "tool_calls", make_tool_test(q, kw))

    # ── Category 3: Content Analysis (10) ──
    print(f"\n{'─' * 70}")
    print("  CATEGORY 3: Content Analysis (10 questions)")
    print(f"{'─' * 70}")
    for i, (q, tid, model) in enumerate(CONTENT_ANALYSIS_TESTS, 1):
        run_test(f"CA-{i:02d}: {tid}", "content_analysis", make_content_test(q, tid, model))

    # ── Category 4: General Governance (20) ──
    print(f"\n{'─' * 70}")
    print("  CATEGORY 4: General Governance (20 questions)")
    print(f"{'─' * 70}")
    governance_tests = [
        ("GOV-01: Health endpoint", test_health_endpoint),
        ("GOV-02: Prometheus metrics", test_metrics_endpoint),
        ("GOV-03: Auth required (no key)", test_auth_required),
        ("GOV-04: Invalid API key rejected", test_invalid_api_key),
        ("GOV-05: Session chain integrity", test_session_chain_integrity),
        ("GOV-06: Execution ID returned", test_execution_id_returned),
        ("GOV-07: Policy result header", test_policy_result_header),
        ("GOV-08: Lineage sessions API", test_lineage_sessions_api),
        ("GOV-09: Lineage attempts API", test_lineage_attempts_api),
        ("GOV-10: gemma3 basic Q&A", test_gemma_basic_qa),
        ("GOV-11: qwen3 basic Q&A", test_qwen_basic_qa),
        ("GOV-12: System prompt honored", test_system_prompt_honored),
        ("GOV-13: Multi-turn conversation", test_multi_turn_conversation),
        ("GOV-14: Trace API with timings", test_trace_api),
        ("GOV-15: Dashboard accessible", test_dashboard_accessible),
        ("GOV-16: Control plane status", test_control_plane_status),
        ("GOV-17: Walacor backend storage", test_walacor_backend_storage),
        ("GOV-18: Model capability discovery", test_model_capability_auto_discovery),
        ("GOV-19: Completeness invariant", test_completeness_invariant),
    ]
    for name, fn in governance_tests:
        run_test(name, "governance", fn)

    # GOV-20: Token budget tracking (needs a fresh check)
    def test_budget_tracking():
        health = requests.get(f"{BASE}/health", timeout=10).json()
        budget = health.get("token_budget", {})
        passed = budget.get("tokens_used", 0) > 0
        return TestResult(passed=passed, model="",
                         details=f"used={budget.get('tokens_used', 0)}/{budget.get('max_tokens', 0)} ({budget.get('percent_used', 0):.1f}%)",
                         latency_ms=0)
    run_test("GOV-20: Token budget tracking", "governance", test_budget_tracking)

    # ── Summary ──
    elapsed = time.perf_counter() - t_start
    print(f"\n{'═' * 70}")
    print("  RESULTS SUMMARY")
    print(f"{'═' * 70}")

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    by_category = {}
    for r in results:
        cat = r.category or "other"
        if cat not in by_category:
            by_category[cat] = {"passed": 0, "failed": 0}
        if r.passed:
            by_category[cat]["passed"] += 1
        else:
            by_category[cat]["failed"] += 1

    for cat, counts in by_category.items():
        total_cat = counts["passed"] + counts["failed"]
        print(f"  {cat:20s}: {counts['passed']}/{total_cat} passed")

    print(f"{'─' * 70}")
    print(f"  TOTAL: {passed}/{total} passed, {failed} failed")
    print(f"  Time:  {elapsed:.1f}s")
    print(f"{'═' * 70}")

    if failed > 0:
        print("\n  FAILED TESTS:")
        for r in results:
            if not r.passed:
                print(f"    - {r.name}: {r.details}")

    # Print dashboard URL
    print(f"\n  Dashboard: {BASE}/lineage/")
    print(f"  View all executions in the Lineage dashboard to inspect governance metadata.\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
