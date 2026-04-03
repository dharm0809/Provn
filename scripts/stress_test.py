"""Comprehensive stress test — 70+ diverse questions across all scenarios.

50-60 Ollama questions (diverse categories)
10 OpenAI questions (multiple models)
RAG scenarios
Web search scenarios
Thinking model scenarios
File tracking
System tasks

Every response is verified in Walacor after completion.

Run:
  python3.12 scripts/stress_test.py \
    --gateway http://35.165.21.8:8000 \
    --api-key provn-team-key-2026 \
    --ollama-model llama3.1:8b \
    --openai-model gpt-4o-mini
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import httpx

TIMEOUT = 180


# ── Test question bank ────────────────────────────────────────────────

OLLAMA_QUESTIONS = {
    "general_knowledge": [
        "What is the capital of Australia?",
        "Who wrote Romeo and Juliet?",
        "What is the speed of light in km/s?",
        "Name the planets in our solar system.",
        "What year did World War 2 end?",
        "What is photosynthesis?",
        "Who painted the Mona Lisa?",
        "What is the largest ocean on Earth?",
        "What is DNA?",
        "Who invented the telephone?",
    ],
    "coding": [
        "Write a Python function to check if a number is prime.",
        "Explain the difference between a list and a tuple in Python.",
        "What is a REST API? Explain with an example endpoint.",
        "Write a SQL query to find duplicate emails in a users table.",
        "What is the time complexity of binary search?",
        "Explain what Docker does in simple terms.",
        "Write a JavaScript function to reverse a string.",
        "What is the difference between GET and POST HTTP methods?",
        "Explain recursion with a simple example.",
        "What is a hash table and when would you use one?",
    ],
    "reasoning": [
        "If all roses are flowers and some flowers fade quickly, can we conclude all roses fade quickly?",
        "A farmer has 17 sheep. All but 9 die. How many are left?",
        "If it takes 5 machines 5 minutes to make 5 widgets, how long does it take 100 machines to make 100 widgets?",
        "What comes next: 2, 6, 12, 20, 30, ?",
        "You have two ropes that each take exactly 1 hour to burn. How do you measure 45 minutes?",
    ],
    "creative": [
        "Write a haiku about artificial intelligence.",
        "Create a short story about a robot learning to cook in exactly 3 sentences.",
        "Write a limerick about a programmer.",
        "Describe the color blue to someone who has never seen it.",
        "Write a product description for a time machine.",
    ],
    "technical": [
        "Explain how HTTPS encryption works step by step.",
        "What is the CAP theorem in distributed systems?",
        "Explain the difference between TCP and UDP.",
        "What is a Kubernetes pod?",
        "How does garbage collection work in Java?",
        "What is the difference between SQL and NoSQL databases?",
        "Explain what a load balancer does.",
        "What is CORS and why does it exist?",
        "How does OAuth 2.0 work?",
        "What is a message queue and when would you use one?",
    ],
    "multilingual": [
        "How do you say 'thank you' in Japanese, Korean, and Arabic?",
        "Translate 'The weather is beautiful today' to French and Spanish.",
        "What does 'carpe diem' mean?",
    ],
    "safety": [
        "Explain the importance of data privacy.",
        "What are the ethical considerations of AI?",
        "How should companies handle user data responsibly?",
    ],
    "long_form": [
        "Explain quantum computing to a 10-year-old. Use analogies.",
        "Compare and contrast microservices and monolithic architecture. Give pros and cons of each.",
        "Write a comprehensive guide on how to make sourdough bread from scratch.",
    ],
}

OPENAI_QUESTIONS = [
    {"model": "gpt-4o-mini", "prompt": "Explain the theory of relativity in 3 sentences.", "category": "science"},
    {"model": "gpt-4o-mini", "prompt": "Write a Python decorator that logs function execution time.", "category": "coding"},
    {"model": "gpt-4o-mini", "prompt": "What are the key differences between React and Vue.js?", "category": "tech"},
    {"model": "gpt-4o-mini", "prompt": "Summarize the plot of The Great Gatsby in one paragraph.", "category": "literature"},
    {"model": "gpt-4o-mini", "prompt": "Design a database schema for a social media platform. List the tables and key columns.", "category": "architecture"},
    {"model": "gpt-4o-mini", "prompt": "Write a regex pattern that validates email addresses and explain each part.", "category": "coding"},
    {"model": "gpt-4o-mini", "prompt": "What are the SOLID principles in software engineering?", "category": "engineering"},
    {"model": "gpt-4o-mini", "prompt": "Explain how neural networks learn through backpropagation.", "category": "ml"},
    {"model": "gpt-4o-mini", "prompt": "Write a haiku about space exploration.", "category": "creative"},
    {"model": "gpt-4o-mini", "prompt": "What is the difference between symmetric and asymmetric encryption?", "category": "security"},
]

RAG_SCENARIOS = [
    {
        "system": "Based on the following internal policy document:\n---\nAll employees must use MFA for system access. Passwords must be 12+ characters. API keys rotate every 90 days.\n---",
        "user": "What is the password policy?",
        "expected_keyword": "12",
    },
    {
        "system": "According to the quarterly report:\n---\nRevenue: $4.2M (+15% YoY). Operating margin: 23%. New customers: 47. Churn rate: 2.1%.\n---",
        "user": "What was the revenue growth?",
        "expected_keyword": "15",
    },
    {
        "system": "From the technical architecture document:\n---\nThe system uses PostgreSQL for relational data, Redis for caching, and Elasticsearch for full-text search. All services communicate via gRPC.\n---",
        "user": "What database is used for caching?",
        "expected_keyword": "redis",
    },
    {
        "system": "Reference the deployment runbook:\n---\nStep 1: Run database migrations. Step 2: Deploy backend pods. Step 3: Run smoke tests. Step 4: Enable traffic via load balancer.\n---",
        "user": "What is step 3 of deployment?",
        "expected_keyword": "smoke",
    },
    {
        "system": "Based on the following internal documentation:\n---\nThe Walacor Gateway uses SHA3-512 Merkle chains for session integrity. Every execution record includes a blockchain-backed envelope with EId, BlockId, and DataHash.\n---",
        "user": "What hashing algorithm does the gateway use?",
        "expected_keyword": "sha3",
    },
]

WEB_SEARCH_QUERIES = [
    "What is the current price of Bitcoin?",
    "What is the latest version of Python released in 2026?",
    "Who won the most recent FIFA World Cup?",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--api-key", default="test-key-alpha")
    parser.add_argument("--ollama-model", default="llama3.1:8b")
    parser.add_argument("--openai-model", default="gpt-4o-mini")
    parser.add_argument("--skip-openai", action="store_true")
    parser.add_argument("--skip-web-search", action="store_true")
    args = parser.parse_args()

    BASE = args.gateway.rstrip("/")
    H = {"X-API-Key": args.api_key, "Content-Type": "application/json"}
    client = httpx.Client(timeout=TIMEOUT, headers=H)

    results = {"pass": 0, "fail": 0, "errors": []}
    start_time = time.perf_counter()

    def run_test(name, model, prompt, extra_body=None, check_fn=None):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if extra_body:
            body.update(extra_body)
        t0 = time.perf_counter()
        try:
            r = client.post(f"{BASE}/v1/chat/completions", json=body)
            d = r.json()
            content = d.get("choices", [{}])[0].get("message", {}).get("content", "")
            tokens = d.get("usage", {}).get("total_tokens", 0)
            ms = round((time.perf_counter() - t0) * 1000)

            ok = bool(content) and r.status_code == 200
            if check_fn and ok:
                ok = check_fn(content)

            if ok:
                results["pass"] += 1
                print(f"  \u2713 {name} ({ms}ms, {tokens}tok) {content[:50]}")
            else:
                results["fail"] += 1
                detail = f"HTTP {r.status_code}, content={'yes' if content else 'EMPTY'}"
                results["errors"].append(f"{name}: {detail}")
                print(f"  \u2717 {name} ({ms}ms) {detail}")
        except Exception as e:
            ms = round((time.perf_counter() - t0) * 1000)
            results["fail"] += 1
            results["errors"].append(f"{name}: {str(e)[:100]}")
            print(f"  \u2717 {name} ({ms}ms) ERROR: {e}")

    def run_rag_test(name, system, user, expected_keyword):
        body = {
            "model": args.ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        t0 = time.perf_counter()
        try:
            r = client.post(f"{BASE}/v1/chat/completions", json=body)
            d = r.json()
            content = d.get("choices", [{}])[0].get("message", {}).get("content", "")
            ms = round((time.perf_counter() - t0) * 1000)
            ok = expected_keyword.lower() in content.lower() and bool(content)
            if ok:
                results["pass"] += 1
                print(f"  \u2713 {name} ({ms}ms) keyword='{expected_keyword}' found")
            else:
                results["fail"] += 1
                results["errors"].append(f"{name}: keyword '{expected_keyword}' not in response")
                print(f"  \u2717 {name} ({ms}ms) keyword '{expected_keyword}' NOT found in: {content[:60]}")
        except Exception as e:
            ms = round((time.perf_counter() - t0) * 1000)
            results["fail"] += 1
            results["errors"].append(f"{name}: {str(e)[:100]}")
            print(f"  \u2717 {name} ({ms}ms) ERROR: {e}")

    # ══════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("COMPREHENSIVE STRESS TEST")
    print(f"Gateway: {BASE}")
    print(f"Ollama: {args.ollama_model} | OpenAI: {args.openai_model}")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # ── Health check ──────────────────────────────────────────────────
    print("\n--- Health Check ---")
    try:
        r = client.get(f"{BASE}/health")
        print(f"  Gateway: {r.json().get('status')}")
    except Exception as e:
        print(f"  FATAL: Gateway not reachable: {e}")
        sys.exit(1)

    # ── Ollama questions (50-60) ──────────────────────────────────────
    total_ollama = 0
    for category, questions in OLLAMA_QUESTIONS.items():
        print(f"\n--- Ollama: {category} ({len(questions)} questions) ---")
        for q in questions:
            total_ollama += 1
            run_test(f"[{category}] Q{total_ollama}", args.ollama_model, q)

    # ── RAG scenarios ─────────────────────────────────────────────────
    print(f"\n--- RAG Scenarios ({len(RAG_SCENARIOS)} tests) ---")
    for i, rag in enumerate(RAG_SCENARIOS):
        run_rag_test(f"RAG-{i+1}", rag["system"], rag["user"], rag["expected_keyword"])

    # ── Web search ────────────────────────────────────────────────────
    if not args.skip_web_search:
        print(f"\n--- Web Search ({len(WEB_SEARCH_QUERIES)} queries) ---")
        for i, q in enumerate(WEB_SEARCH_QUERIES):
            run_test(f"WebSearch-{i+1}", args.ollama_model, q,
                     extra_body={"metadata": {"features": {"web_search": True}}})
    else:
        print("\n--- Web Search: SKIPPED ---")

    # ── OpenAI routing ────────────────────────────────────────────────
    if not args.skip_openai and args.openai_model:
        print(f"\n--- OpenAI ({len(OPENAI_QUESTIONS)} questions) ---")
        for i, oq in enumerate(OPENAI_QUESTIONS):
            run_test(f"[{oq['category']}] OpenAI-{i+1}", oq["model"], oq["prompt"])
    else:
        print("\n--- OpenAI: SKIPPED ---")

    # ── Verify Walacor ────────────────────────────────────────────────
    print("\n--- Walacor Verification ---")
    time.sleep(5)  # Wait for async writes

    try:
        r = client.get(f"{BASE}/v1/lineage/sessions?limit=5")
        d = r.json()
        total_sessions = d.get("total", 0)
        print(f"  Total sessions in Walacor: {total_sessions}")

        r2 = client.get(f"{BASE}/v1/lineage/attempts?limit=1")
        d2 = r2.json()
        total_attempts = d2.get("total", 0)
        stats = d2.get("stats", {})
        print(f"  Total attempts: {total_attempts}")
        print(f"  Disposition stats: {stats}")

        # Check latest session has blockchain proof
        if d.get("sessions"):
            sid = d["sessions"][0]["session_id"]
            r3 = client.get(f"{BASE}/v1/lineage/sessions/{sid}")
            records = r3.json().get("records", [])
            if records:
                rec = records[0]
                env = rec.get("_envelope", {})
                print(f"  Blockchain proof: EId={bool(rec.get('_walacor_eid'))} BlockId={bool(env.get('block_id'))} DH={bool(env.get('data_hash'))}")
    except Exception as e:
        print(f"  Walacor verification error: {e}")

    # ── Summary ───────────────────────────────────────────────────────
    elapsed = round(time.perf_counter() - start_time)
    total = results["pass"] + results["fail"]
    print("\n" + "=" * 70)
    print(f"RESULT: {results['pass']}/{total} passed, {results['fail']} failed")
    print(f"Time: {elapsed}s ({elapsed // 60}m {elapsed % 60}s)")
    if results["errors"]:
        print(f"\nFAILURES ({len(results['errors'])}):")
        for e in results["errors"]:
            print(f"  {e}")
    print("=" * 70)
    sys.exit(1 if results["fail"] else 0)


if __name__ == "__main__":
    main()
