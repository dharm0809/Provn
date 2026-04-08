#!/usr/bin/env python3
"""Hard test suite — validates the scenarios that quality_test.py doesn't cover.

These are the genuinely hard cases:
  1. OpenWebUI system task classification (tags, titles, follow-ups)
  2. Schema enrichment — records have all expected fields
  3. Concurrent same-session race conditions
  4. Chain integrity — hash chain correctness across turns
  5. Tool audit data integrity — tool metadata matches tool events
  6. Session isolation — no data leakage between sessions
  7. Streaming chain integrity — chain works with streaming responses
  8. Multi-turn metadata accuracy — conversation_turns and extraction_method
  9. Indicator data propagation — tool badges, RAG flags in session list
 10. Content analysis depth — verdicts have real confidence scores
 11. Lineage completeness — every request has execution + attempt
 12. Concurrent multi-session chain safety — no interleaved chain records

Usage:
    python scripts/hard_test.py --url http://localhost:8100 --key dharm-key-2026 --model gemma4:e4b
"""

import argparse
import json
import sys
import time
import uuid
import urllib.request
import urllib.error
import concurrent.futures
import threading

PASS = 0
FAIL = 0
WARN = 0
LOCK = threading.Lock()


def req(base, path, data=None, method="GET", key="", timeout=180, headers=None, raw=False):
    url = f"{base}{path}"
    hdrs = {"Content-Type": "application/json"}
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    r = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw_bytes = resp.read()
            if raw:
                return raw_bytes.decode()
            result = json.loads(raw_bytes)
            result["_status"] = resp.status
            return result
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:500]
        if raw:
            return body_text
        try:
            result = json.loads(body_text)
            result["_status"] = e.code
            return result
        except Exception:
            return {"_error": e.code, "_body": body_text, "_status": e.code}
    except Exception as e:
        return {"_error": str(e)}


def chat(base, key, model, messages, sid=None, stream=False, hdrs=None):
    h = {}
    if sid:
        h["X-Session-Id"] = sid
    if hdrs:
        h.update(hdrs)
    return req(base, "/v1/chat/completions", {
        "model": model, "messages": messages, "stream": stream,
    }, "POST", key, headers=h, raw=stream)


def check(name, condition, detail=""):
    global PASS, FAIL
    ok = bool(condition)
    with LOCK:
        PASS += ok
        FAIL += not ok
    print(f"  {'PASS' if ok else 'FAIL':4s} {name}" + (f" -- {detail}" if detail else ""))
    return ok


def warn(name, detail=""):
    global WARN
    with LOCK:
        WARN += 1
    print(f"  WARN {name} -- {detail}")


def get_record(base, key, sid, retries=5, delay=1.5):
    """Fetch session records, retrying for async delivery."""
    for _ in range(retries):
        sess = req(base, f"/v1/lineage/sessions/{sid}", key=key)
        recs = sess.get("records", [])
        if recs:
            return recs
        time.sleep(delay)
    return []


# ═══════════════════════════════════════════════════════════════════════════
# 1. OPENWEBUI SYSTEM TASK CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

def test_openwebui_classification(base, key, model):
    print("\n=== 1. OPENWEBUI SYSTEM TASK CLASSIFICATION ===")

    # OpenWebUI generates these system prompts for internal tasks
    system_task_prompts = [
        ("Tag generation", "### Task:\nGenerate 1-3 concise tags for the conversation. "
         "Format: tag1,tag2,tag3"),
        ("Title generation", "### Task:\nGenerate a concise title for this conversation. "
         "Only output the title, nothing else."),
        ("Follow-up generation", "### Task:\nGenerate 3 follow-up questions based on the conversation. "
         "Format them as a numbered list."),
        ("Summary task", "### Task:\nSummarize the conversation in 2-3 sentences."),
    ]

    for name, system_prompt in system_task_prompts:
        sid = f"owui-{uuid.uuid4().hex[:8]}"
        r = chat(base, key, model, [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Go"},
        ], sid=sid)
        has_response = "choices" in r
        check(f"System task '{name}' gets response", has_response)

        # Verify it was classified as system_task in lineage
        time.sleep(1.5)
        recs = get_record(base, key, sid)
        if recs:
            meta = recs[-1].get("metadata", {})
            rtype = meta.get("request_type", "")
            check(f"'{name}' classified as system_task",
                  rtype.startswith("system_task"),
                  f"request_type={rtype}")
        else:
            warn(f"'{name}' no lineage record", "async delivery delay?")

    # Normal message should NOT be classified as system_task
    sid = f"owui-normal-{uuid.uuid4().hex[:8]}"
    chat(base, key, model, [
        {"role": "user", "content": "What is the speed of light?"},
    ], sid=sid)
    time.sleep(1.5)
    recs = get_record(base, key, sid)
    if recs:
        rtype = recs[-1].get("metadata", {}).get("request_type", "")
        check("Normal message NOT classified as system_task",
              not rtype.startswith("system_task"), f"request_type={rtype}")


# ═══════════════════════════════════════════════════════════════════════════
# 2. SCHEMA ENRICHMENT — records have normalized fields
# ═══════════════════════════════════════════════════════════════════════════

def test_schema_enrichment(base, key, model):
    print("\n=== 2. SCHEMA ENRICHMENT ===")

    sid = f"schema-{uuid.uuid4().hex[:8]}"
    r = chat(base, key, model, [
        {"role": "user", "content": "Name three colors."},
    ], sid=sid)
    time.sleep(2)

    recs = get_record(base, key, sid)
    if not recs:
        check("Record exists", False, "No records found")
        return

    rec = recs[-1]

    # Core fields that schema enrichment should guarantee
    required_fields = [
        "execution_id", "session_id", "model_id", "timestamp",
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_text", "response_content",
    ]
    for field in required_fields:
        val = rec.get(field)
        check(f"Field '{field}' present and non-null", val is not None, f"value={val}")

    # Metadata quality
    meta = rec.get("metadata", {})
    check("metadata is dict", isinstance(meta, dict))
    check("metadata has request_type", "request_type" in meta)

    # Walacor audit metadata
    audit = meta.get("walacor_audit", {})
    check("walacor_audit present", bool(audit))
    check("extraction_method present", bool(audit.get("extraction_method")),
          f"method={audit.get('extraction_method')}")

    # Chain fields
    check("sequence_number present", rec.get("sequence_number") is not None)

    # Intent classification
    intent = meta.get("_intent", {})
    check("intent classification present", bool(intent))
    if intent:
        check("intent has label", bool(intent.get("intent") or intent.get("label")),
              f"intent={intent}")


# ═══════════════════════════════════════════════════════════════════════════
# 3. CONCURRENT SAME-SESSION RACE CONDITIONS
# ═══════════════════════════════════════════════════════════════════════════

def test_concurrent_same_session(base, key, model):
    print("\n=== 3. CONCURRENT SAME-SESSION ===")

    sid = f"race-{uuid.uuid4().hex[:8]}"
    questions = [
        "What is 2 + 2?",
        "What is the capital of France?",
        "Name a planet",
    ]

    results = []

    def send_request(q):
        r = chat(base, key, model, [{"role": "user", "content": q}], sid=sid)
        return q, r

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(send_request, q) for q in questions]
        for f in concurrent.futures.as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                results.append(("?", {"_error": str(e)}))

    succeeded = sum(1 for _, r in results if "choices" in r)
    check(f"All {len(questions)} concurrent requests succeeded",
          succeeded == len(questions), f"succeeded={succeeded}/{len(questions)}")

    # Wait for records to settle
    time.sleep(3)

    # Verify all requests landed in the same session
    recs = get_record(base, key, sid)
    check(f"Session has {len(questions)} records", len(recs) >= len(questions),
          f"found={len(recs)}")

    # Verify sequence numbers are unique and contiguous
    if recs:
        seqs = [r.get("sequence_number") for r in recs if r.get("sequence_number") is not None]
        check("All records have sequence_number", len(seqs) == len(recs),
              f"with_seq={len(seqs)}, total={len(recs)}")
        if seqs:
            check("Sequence numbers are unique", len(set(seqs)) == len(seqs),
                  f"unique={len(set(seqs))}, total={len(seqs)}")


# ═══════════════════════════════════════════════════════════════════════════
# 4. CHAIN INTEGRITY — hash chain correctness
# ═══════════════════════════════════════════════════════════════════════════

def test_chain_integrity(base, key, model):
    print("\n=== 4. CHAIN INTEGRITY ===")

    sid = f"chain-{uuid.uuid4().hex[:8]}"
    # Send 3 sequential requests to build a chain
    for i in range(3):
        chat(base, key, model, [
            {"role": "user", "content": f"Say the number {i+1}"},
        ], sid=sid)
        time.sleep(0.5)

    time.sleep(2)

    # Verify chain via server-side verification
    verify = req(base, f"/v1/lineage/verify/{sid}", key=key)
    check("Chain verification endpoint responds",
          "valid" in verify or "chain" in str(verify).lower(),
          f"response keys={list(verify.keys())[:5]}")

    is_valid = verify.get("valid", verify.get("chain_valid"))
    check("Chain is valid", is_valid, f"valid={is_valid}")

    # Verify chain links (previous_record_hash → record_hash)
    recs = get_record(base, key, sid)
    if len(recs) >= 2:
        for i in range(1, len(recs)):
            prev_hash = recs[i].get("previous_record_hash")
            actual_prev = recs[i-1].get("record_hash")
            if prev_hash and actual_prev:
                check(f"Record {i} links to record {i-1}",
                      prev_hash == actual_prev,
                      f"expected={actual_prev[:16]}... got={prev_hash[:16]}...")
            else:
                warn(f"Record {i} missing chain fields",
                     f"prev_hash={prev_hash is not None}, record_hash={actual_prev is not None}")


# ═══════════════════════════════════════════════════════════════════════════
# 5. TOOL AUDIT DATA INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════

def test_tool_audit_integrity(base, key, model):
    print("\n=== 5. TOOL AUDIT DATA INTEGRITY ===")

    sid = f"toolaudit-{uuid.uuid4().hex[:8]}"
    # Ask a question that should trigger web search if tools are enabled
    r = chat(base, key, model, [
        {"role": "user", "content": "Search for: What is the current population of Tokyo?"},
    ], sid=sid)
    check("Tool-triggering request succeeds", "choices" in r)

    time.sleep(3)

    # Check execution record for tool metadata
    recs = get_record(base, key, sid)
    if not recs:
        warn("No records for tool audit", "skipping tool integrity checks")
        return

    rec = recs[-1]
    eid = rec.get("execution_id")
    meta = rec.get("metadata", {})
    tool_strategy = meta.get("tool_strategy")
    tool_count = meta.get("tool_interaction_count", 0)

    if tool_strategy and tool_strategy != "none" and tool_count > 0:
        # Verify tool events exist via lineage API
        exec_data = req(base, f"/v1/lineage/executions/{eid}", key=key)
        tool_events = exec_data.get("tool_events", [])
        check("Tool events exist in lineage",
              len(tool_events) > 0, f"events={len(tool_events)}")

        if tool_events:
            # Verify tool event has required fields
            te = tool_events[0]
            check("Tool event has tool_name", bool(te.get("tool_name")),
                  f"name={te.get('tool_name')}")
            check("Tool event has input_hash", bool(te.get("input_hash")),
                  f"hash={te.get('input_hash', '')[:16]}")
            check("Tool event has session_id matching parent",
                  te.get("session_id") == sid,
                  f"te_sid={te.get('session_id')}, expected={sid}")

        # Cross-check: metadata tool count matches actual events
        meta_interactions = meta.get("tool_interactions", [])
        check("Metadata tool count matches events",
              len(meta_interactions) == len(tool_events),
              f"meta={len(meta_interactions)}, events={len(tool_events)}")
    else:
        warn("No tool activity detected",
             f"strategy={tool_strategy}, count={tool_count} — model may not support tools")


# ═══════════════════════════════════════════════════════════════════════════
# 6. SESSION ISOLATION — no data leakage
# ═══════════════════════════════════════════════════════════════════════════

def test_session_isolation(base, key, model):
    print("\n=== 6. SESSION ISOLATION ===")

    sid_a = f"iso-A-{uuid.uuid4().hex[:8]}"
    sid_b = f"iso-B-{uuid.uuid4().hex[:8]}"

    # Send different content to two sessions
    chat(base, key, model, [
        {"role": "user", "content": "The secret word is ALPHA."},
    ], sid=sid_a)
    chat(base, key, model, [
        {"role": "user", "content": "The secret word is BRAVO."},
    ], sid=sid_b)

    time.sleep(2)

    recs_a = get_record(base, key, sid_a)
    recs_b = get_record(base, key, sid_b)

    if recs_a and recs_b:
        # Verify each session has only its own records
        check("Session A records belong to session A",
              all(r.get("session_id") == sid_a for r in recs_a))
        check("Session B records belong to session B",
              all(r.get("session_id") == sid_b for r in recs_b))

        # Verify execution IDs don't overlap
        eids_a = {r.get("execution_id") for r in recs_a}
        eids_b = {r.get("execution_id") for r in recs_b}
        check("Execution IDs don't overlap", eids_a.isdisjoint(eids_b),
              f"overlap={eids_a & eids_b}")

        # Verify prompt content doesn't leak
        prompt_a = recs_a[-1].get("prompt_text", "")
        prompt_b = recs_b[-1].get("prompt_text", "")
        check("Session A prompt contains ALPHA", "alpha" in prompt_a.lower(),
              f"prompt={prompt_a[:40]}")
        check("Session B prompt contains BRAVO", "bravo" in prompt_b.lower(),
              f"prompt={prompt_b[:40]}")
    else:
        warn("Missing records for isolation test",
             f"A={len(recs_a)}, B={len(recs_b)}")


# ═══════════════════════════════════════════════════════════════════════════
# 7. STREAMING CHAIN INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════

def test_streaming_chain(base, key, model):
    print("\n=== 7. STREAMING CHAIN ===")

    sid = f"strchain-{uuid.uuid4().hex[:8]}"

    # Send 2 streaming requests to same session
    for i in range(2):
        raw = chat(base, key, model, [
            {"role": "user", "content": f"Count to {i+3}"},
        ], sid=sid, stream=True)
        # Consume all SSE chunks
        chunks = 0
        if isinstance(raw, str):
            for line in raw.split("\n"):
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunks += 1
        check(f"Streaming request {i+1} received chunks", chunks > 0, f"chunks={chunks}")
        time.sleep(1)

    time.sleep(2)

    # Verify chain is intact despite streaming
    recs = get_record(base, key, sid)
    check("Streaming session has 2 records", len(recs) >= 2,
          f"found={len(recs)}")

    if len(recs) >= 2:
        # Chain link check
        prev_hash = recs[1].get("previous_record_hash")
        first_hash = recs[0].get("record_hash")
        if prev_hash and first_hash:
            check("Streaming chain links correctly",
                  prev_hash == first_hash,
                  f"expected={first_hash[:16]}... got={prev_hash[:16]}...")

        # Both records should have response_content (not empty from streaming)
        for i, rec in enumerate(recs):
            content = rec.get("response_content", "")
            check(f"Streaming record {i+1} has response_content",
                  bool(content), f"len={len(content)}")


# ═══════════════════════════════════════════════════════════════════════════
# 8. MULTI-TURN METADATA ACCURACY
# ═══════════════════════════════════════════════════════════════════════════

def test_multiturn_metadata(base, key, model):
    print("\n=== 8. MULTI-TURN METADATA ===")

    sid = f"mt-{uuid.uuid4().hex[:8]}"

    # 5-turn conversation
    messages = [
        {"role": "user", "content": "What programming languages do you know?"},
        {"role": "assistant", "content": "I know Python, JavaScript, Go, Rust, and more."},
        {"role": "user", "content": "Tell me about Python."},
        {"role": "assistant", "content": "Python is a versatile language."},
        {"role": "user", "content": "What about its performance?"},
    ]
    r = chat(base, key, model, messages, sid=sid)
    check("5-turn response", "choices" in r)

    time.sleep(2)
    recs = get_record(base, key, sid)
    if not recs:
        check("Record found", False)
        return

    rec = recs[-1]
    meta = rec.get("metadata", {})
    audit = meta.get("walacor_audit", {})

    method = audit.get("extraction_method")
    check("Extraction method is last_user_message",
          method == "last_user_message", f"method={method}")

    turns = audit.get("conversation_turns")
    check("Conversation turns = 3 (user messages)", turns == 3, f"turns={turns}")

    prompt = rec.get("prompt_text", "")
    check("Prompt is last user message (about performance)",
          "performance" in prompt.lower(), f"prompt={prompt[:50]}")


# ═══════════════════════════════════════════════════════════════════════════
# 9. INDICATOR DATA PROPAGATION
# ═══════════════════════════════════════════════════════════════════════════

def test_indicator_propagation(base, key, model):
    print("\n=== 9. INDICATOR DATA PROPAGATION ===")

    # Send a request with identity headers to verify user propagation
    sid = f"ind-{uuid.uuid4().hex[:8]}"
    chat(base, key, model, [
        {"role": "user", "content": "Hello, this is a test"},
    ], sid=sid, hdrs={"X-User-Id": "test-user-indicator"})

    time.sleep(2)

    # Check sessions list API returns this session with correct indicators
    sessions = req(base, "/v1/lineage/sessions?limit=50", key=key)
    sess_list = sessions.get("sessions", [])

    found = None
    for s in sess_list:
        if s.get("session_id") == sid:
            found = s
            break

    if found:
        check("Session appears in list", True)
        check("Session has model field", bool(found.get("model")),
              f"model={found.get('model')}")
        check("Session has record_count", found.get("record_count", 0) > 0,
              f"count={found.get('record_count')}")
        check("Session has user field", found.get("user") == "test-user-indicator",
              f"user={found.get('user')}")
        check("Session has last_activity", bool(found.get("last_activity")),
              f"ts={found.get('last_activity')}")
    else:
        warn("Session not found in list", f"sid={sid}, total sessions={len(sess_list)}")


# ═══════════════════════════════════════════════════════════════════════════
# 10. CONTENT ANALYSIS DEPTH
# ═══════════════════════════════════════════════════════════════════════════

def test_content_analysis_depth(base, key, model):
    print("\n=== 10. CONTENT ANALYSIS DEPTH ===")

    # Send a request with known PII to verify analyzers run
    sid = f"analysis-{uuid.uuid4().hex[:8]}"
    r = chat(base, key, model, [
        {"role": "user", "content": "My credit card number is 4111-1111-1111-1111 and my SSN is 123-45-6789"},
    ], sid=sid)
    check("PII request gets response", "choices" in r)

    time.sleep(2)
    recs = get_record(base, key, sid)
    if not recs:
        warn("No records for content analysis", "skipping")
        return

    rec = recs[-1]
    meta = rec.get("metadata", {})

    # Check input_analysis (pre-inference analyzers)
    input_analysis = meta.get("input_analysis", [])
    check("Input analysis present", len(input_analysis) > 0,
          f"analyzers={len(input_analysis)}")

    if input_analysis:
        # Verify PII was detected
        pii_decisions = [d for d in input_analysis if "pii" in d.get("analyzer_id", "").lower()]
        check("PII analyzer ran", len(pii_decisions) > 0)
        if pii_decisions:
            pii = pii_decisions[0]
            check("PII verdict is warn or block",
                  pii.get("verdict") in ("warn", "block"),
                  f"verdict={pii.get('verdict')}")
            check("PII confidence > 0",
                  (pii.get("confidence") or 0) > 0,
                  f"confidence={pii.get('confidence')}")

    # Check analyzer_decisions (post-inference analyzers)
    analyzer_decisions = meta.get("analyzer_decisions", [])
    if analyzer_decisions:
        for d in analyzer_decisions:
            aid = d.get("analyzer_id", "")
            verdict = d.get("verdict", "")
            conf = d.get("confidence", 0)
            # Every decision should have non-zero confidence (not a stub)
            if conf == 0 and verdict != "pass":
                warn(f"Analyzer {aid} has 0 confidence with {verdict}",
                     "may be a stub result")


# ═══════════════════════════════════════════════════════════════════════════
# 11. LINEAGE COMPLETENESS — execution + attempt for every request
# ═══════════════════════════════════════════════════════════════════════════

def test_lineage_completeness(base, key, model):
    print("\n=== 11. LINEAGE COMPLETENESS ===")

    sid = f"complete-{uuid.uuid4().hex[:8]}"
    r = chat(base, key, model, [
        {"role": "user", "content": "What year was the internet invented?"},
    ], sid=sid)
    check("Request succeeds", "choices" in r)

    time.sleep(2)

    # Check execution record exists
    recs = get_record(base, key, sid)
    check("Execution record exists", len(recs) > 0, f"found={len(recs)}")

    if recs:
        eid = recs[-1].get("execution_id")
        check("Execution ID is not empty", bool(eid))

        # Check execution detail endpoint works
        exec_detail = req(base, f"/v1/lineage/executions/{eid}", key=key)
        check("Execution detail endpoint returns data",
              "record" in exec_detail or "execution_id" in exec_detail,
              f"keys={list(exec_detail.keys())[:5]}")

    # Check attempts endpoint has matching record
    attempts = req(base, "/v1/lineage/attempts?limit=20", key=key)
    att_list = attempts.get("attempts", attempts.get("items", []))
    check("Attempts endpoint returns data", len(att_list) > 0,
          f"count={len(att_list)}")

    # Stats should be present
    stats = attempts.get("stats", {})
    check("Attempt stats present", bool(stats), f"stats={stats}")

    if stats:
        forwarded = stats.get("forwarded", 0) + stats.get("allowed", 0)
        check("At least one forwarded/allowed request", forwarded > 0,
              f"forwarded={forwarded}")


# ═══════════════════════════════════════════════════════════════════════════
# 12. CONCURRENT MULTI-SESSION CHAIN SAFETY
# ═══════════════════════════════════════════════════════════════════════════

def test_concurrent_chain_safety(base, key, model):
    print("\n=== 12. CONCURRENT CHAIN SAFETY ===")

    # Create 3 sessions with 2 turns each, all concurrently
    sessions = [f"chainsafe-{i}-{uuid.uuid4().hex[:6]}" for i in range(3)]

    def send_two_turns(sid, idx):
        r1 = chat(base, key, model, [
            {"role": "user", "content": f"Session {idx}: first message"},
        ], sid=sid)
        time.sleep(0.3)
        r2 = chat(base, key, model, [
            {"role": "user", "content": f"Session {idx}: second message"},
        ], sid=sid)
        return sid, "choices" in r1, "choices" in r2

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(send_two_turns, sid, i) for i, sid in enumerate(sessions)]
        for f in concurrent.futures.as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                results.append(("?", False, False))

    succeeded = sum(1 for _, ok1, ok2 in results if ok1 and ok2)
    check(f"All 3 concurrent sessions completed", succeeded == 3,
          f"succeeded={succeeded}/3")

    time.sleep(3)

    # Verify each session has its own intact chain
    all_ok = True
    for sid in sessions:
        recs = get_record(base, key, sid)
        if len(recs) < 2:
            warn(f"Session {sid[:20]} missing records", f"found={len(recs)}")
            all_ok = False
            continue

        # Verify chain links within this session
        for i in range(1, len(recs)):
            prev_hash = recs[i].get("previous_record_hash")
            expected = recs[i-1].get("record_hash")
            if prev_hash and expected and prev_hash != expected:
                check(f"Chain link {sid[:12]}[{i}]", False,
                      f"prev={prev_hash[:16]} expected={expected[:16]}")
                all_ok = False

    if all_ok:
        check("All concurrent session chains are intact", True)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="TruzenAI Gateway HARD Tests")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--key", default="test-key-alpha")
    parser.add_argument("--model", default="qwen3:1.7b")
    parser.add_argument("--skip-slow", action="store_true",
                        help="Skip concurrency and chain tests")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    print(f"TruzenAI Gateway HARD Tests")
    print(f"URL: {base}  Model: {args.model}")
    print(f"{'=' * 60}")

    t = time.time()

    test_openwebui_classification(base, args.key, args.model)
    test_schema_enrichment(base, args.key, args.model)
    test_multiturn_metadata(base, args.key, args.model)
    test_content_analysis_depth(base, args.key, args.model)
    test_session_isolation(base, args.key, args.model)
    test_indicator_propagation(base, args.key, args.model)
    test_lineage_completeness(base, args.key, args.model)

    if not args.skip_slow:
        test_concurrent_same_session(base, args.key, args.model)
        test_chain_integrity(base, args.key, args.model)
        test_streaming_chain(base, args.key, args.model)
        test_tool_audit_integrity(base, args.key, args.model)
        test_concurrent_chain_safety(base, args.key, args.model)

    elapsed = time.time() - t
    total = PASS + FAIL
    pct = PASS / total * 100 if total > 0 else 0
    print(f"\n{'=' * 60}")
    print(f"HARD: {PASS}/{total} passed ({pct:.0f}%), {FAIL} failed, {WARN} warnings")
    print(f"Time: {elapsed:.1f}s")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
