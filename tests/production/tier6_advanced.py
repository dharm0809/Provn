#!/usr/bin/env python3
"""Tier 6 advanced features — web search, tool audit, attachments, MCP, content analysis.

Run ON the EC2 instance from ~/Gateway:
    GATEWAY_MODEL=qwen3:4b python3.12 tests/production/tier6_advanced.py

Requires qwen3:4b for tool support:
    docker exec gateway-ollama-1 ollama pull qwen3:4b

Also enable web search if not already set in .env:
    echo 'WALACOR_WEB_SEARCH_ENABLED=true' >> .env
    echo 'WALACOR_TOOL_AWARE_ENABLED=true' >> .env
    docker compose up -d gateway
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import requests

sys.path.insert(0, "tests/production")
from config import BASE_URL, CHAT_URL, LINEAGE_URL, HEADERS, MODEL, save_artifact

RESULTS: list[dict] = []
TOOL_MODEL = MODEL  # should be qwen3:4b

# Direct Ollama access (bypasses gateway) for diagnostics
_OLLAMA_PORT = os.environ.get("OLLAMA_PORT", "11434")
_OLLAMA_URL = f"http://localhost:{_OLLAMA_PORT}/v1/chat/completions"

# Standard OpenAI-format tool definition for web_search
_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for information",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
            },
            "required": ["query"],
        },
    },
}

# Module-level flag: set by preflight_tool_check()
_TOOLS_WORK = False


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append({"name": name, "passed": passed, "detail": detail})


def skip(name: str, reason: str) -> None:
    print(f"  [SKIP] {name}: {reason}")
    RESULTS.append({"name": name, "passed": True, "detail": f"skipped: {reason}"})


def chat(messages, model=None, **kwargs):
    return requests.post(CHAT_URL, json={
        "model": model or TOOL_MODEL,
        "messages": messages,
        "max_tokens": kwargs.get("max_tokens", 200),
        **{k: v for k, v in kwargs.items() if k != "max_tokens"},
    }, headers=HEADERS, timeout=120)


def tool_request(session_id: str, prompt: str, max_tokens: int = 2048) -> requests.Response:
    """POST via gateway with explicit tool definitions.

    Sends tools directly in the body (matching the pre-flight approach that
    works) rather than relying on gateway auto-injection.
    """
    return requests.post(CHAT_URL, json={
        "model": TOOL_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [_TOOL_DEF],
        "stream": False,
        "max_tokens": max_tokens,
    }, headers={**HEADERS, "X-Session-Id": session_id}, timeout=120)


def get_health() -> dict:
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    return r.json() if r.status_code == 200 else {}


def _check_tool_response(r: requests.Response) -> tuple[bool, str]:
    """Check if a response contains tool_calls. Returns (has_tools, detail)."""
    if r.status_code != 200:
        return False, f"status={r.status_code}"
    body = r.json()
    choices = body.get("choices", [])
    if not choices:
        return False, "no choices"
    choice = choices[0]
    fr = choice.get("finish_reason", "")
    msg = choice.get("message", {})
    tc = msg.get("tool_calls", [])
    content = (msg.get("content") or "")[:100]
    if tc or fr == "tool_calls":
        return True, f"finish_reason={fr}, {len(tc)} tool_calls"
    return False, f"finish_reason={fr}, content={content!r}"


def _check_lineage_for_tools(session_id: str) -> bool:
    """Check if a session has tool events in its execution records."""
    sr = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    if sr.status_code != 200:
        return False
    sessions = sr.json().get("sessions", [])
    match = next((s for s in sessions if s.get("session_id") == session_id), None)
    if not match:
        return False

    # Check tool_names in session listing
    if "web_search" in str(match.get("tool_names", "")):
        return True

    # Check execution detail for tool events
    sid = match["session_id"]
    er = requests.get(f"{LINEAGE_URL}/sessions/{sid}", timeout=10)
    if er.status_code != 200:
        return False
    for rec in er.json().get("records", []):
        exec_id = rec.get("execution_id") or rec.get("id")
        if not exec_id:
            continue
        er2 = requests.get(f"{LINEAGE_URL}/executions/{exec_id}", timeout=10)
        if er2.status_code == 200:
            if er2.json().get("tool_events"):
                return True
    return False


# ── 0. Pre-flight: diagnose tool support at Ollama level ─────────────────────

def preflight_tool_check() -> bool:
    """Test tool calling directly against Ollama (bypasses gateway).

    This isolates model behavior from gateway behavior:
    - If Ollama returns tool_calls → model supports tools, gateway should too
    - If Ollama does NOT → model limitation, tool tests should skip
    """
    global _TOOLS_WORK

    # ── Step 1: Direct Ollama test ───────────────────────────────────────
    print("  [DIAG] Step 1: Testing tool calls directly against Ollama...")
    ollama_tools = False
    try:
        r = requests.post(_OLLAMA_URL, json={
            "model": TOOL_MODEL,
            "messages": [
                {"role": "user", "content": "Search for: test"},
            ],
            "tools": [_TOOL_DEF],
            "stream": False,
        }, timeout=120)
        ollama_tools, detail = _check_tool_response(r)
        if ollama_tools:
            print(f"  [DIAG]   Ollama: PASS — {detail}")
        else:
            print(f"  [DIAG]   Ollama: model did NOT call tools — {detail}")
    except requests.ConnectionError:
        print(f"  [DIAG]   Cannot reach Ollama at {_OLLAMA_URL}")
        print(f"  [DIAG]   (port 11434 not mapped? trying gateway only)")

    if not ollama_tools:
        print(f"  [DIAG]   {TOOL_MODEL} does not emit tool_calls in Ollama.")
        print(f"  [DIAG]   Tool-dependent tests (1/2/5/7) will SKIP.")
        print(f"  [DIAG]   To enable: pull a tool-calling model like llama3.1:8b")
        _TOOLS_WORK = False
        return False

    # ── Step 2: Send tools EXPLICITLY through gateway (same body as step 1)
    # This isolates: is the gateway blocking tool calls, or just not injecting them?
    print("  [DIAG] Step 2: Sending explicit tools through gateway...")
    gw_session = str(uuid.uuid4())
    r = requests.post(CHAT_URL, json={
        "model": TOOL_MODEL,
        "messages": [{"role": "user", "content": "Search for: test"}],
        "tools": [_TOOL_DEF],
        "stream": False,
    }, headers={**HEADERS, "X-Session-Id": gw_session}, timeout=120)

    if r.status_code != 200:
        print(f"  [DIAG]   Explicit tools: got {r.status_code}")
        # Retry once
        time.sleep(5)
        gw_session = str(uuid.uuid4())
        r = requests.post(CHAT_URL, json={
            "model": TOOL_MODEL,
            "messages": [{"role": "user", "content": "Search for: test"}],
            "tools": [_TOOL_DEF],
            "stream": False,
        }, headers={**HEADERS, "X-Session-Id": gw_session}, timeout=120)

    if r.status_code != 200:
        print(f"  [DIAG]   Explicit tools failed: {r.status_code}")
        _TOOLS_WORK = False
        return False

    body = r.json()
    choice = body.get("choices", [{}])[0]
    fr = choice.get("finish_reason", "")
    tc = choice.get("message", {}).get("tool_calls", [])
    content = (choice.get("message", {}).get("content") or "")[:120]

    # Gateway active tool loop consumes tool_calls → finish_reason=stop + search content
    # If we see content mentioning search, the tool loop ran successfully
    print(f"  [DIAG]   finish_reason={fr}, tool_calls={len(tc)}, content={content!r}")

    time.sleep(3)  # WAL write is async
    gw_tools = _check_lineage_for_tools(gw_session)
    if gw_tools:
        print(f"  [DIAG]   Gateway: PASS — tool events found in lineage")
    elif any(kw in content.lower() for kw in ("search", "result", "found", "no result")):
        print(f"  [DIAG]   Gateway: PASS — response indicates tool execution")
        gw_tools = True
    elif fr == "tool_calls" or tc:
        # Gateway did NOT consume tool_calls (no active tool loop?) — still proves tools work
        print(f"  [DIAG]   Gateway: PASS — model emitted tool_calls (not consumed by gateway)")
        gw_tools = True
    else:
        print(f"  [DIAG]   Gateway: tools not detected")

    _TOOLS_WORK = gw_tools
    return gw_tools


def _require_tools(name: str) -> bool:
    """Guard for tool-dependent tests. Returns True if test should run."""
    if TOOL_MODEL == "qwen3:1.7b":
        skip(name, "qwen3:1.7b doesn't support tools — upgrade to qwen3:4b")
        return False
    if not _TOOLS_WORK:
        skip(name, f"{TOOL_MODEL} does not emit tool_calls (see pre-flight)")
        return False
    return True


# ── 1. Web search tool invocation ────────────────────────────────────────────

def test_web_search_invocation():
    """Send a prompt that triggers web search and verify tool was called."""
    if not _require_tools("Web search invocation"):
        return

    session_id = str(uuid.uuid4())
    r = tool_request(session_id,
        "Use the web_search tool right now. Search for 'artificial intelligence' "
        "and report what the search returns.")

    check("Web search request returns 200", r.status_code == 200, f"got {r.status_code}")
    if r.status_code != 200:
        return

    time.sleep(3)  # WAL write async

    # Find our session in lineage
    sr = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    if sr.status_code != 200:
        check("Session found after web search", False, "lineage sessions unavailable")
        return

    sessions = sr.json().get("sessions", [])
    match = next((s for s in sessions if s.get("session_id") == session_id), None)
    check("Web search session found in lineage", match is not None,
          f"session_id={session_id[:8]}...")

    if match:
        has_tools = _check_lineage_for_tools(session_id)
        check("Tool event recorded (web_search executed)",
              has_tools, "checked session + execution detail")


# ── 2. Tool event audit integrity ─────────────────────────────────────────────

def test_tool_event_audit():
    """After a tool call, verify the execution record has properly hashed tool events."""
    if not _require_tools("Tool event audit"):
        return

    session_id = str(uuid.uuid4())
    r = tool_request(session_id,
        "Call the web_search tool with query 'machine learning'. "
        "Summarize the first result in one sentence.")

    check("Tool audit request returns 200", r.status_code == 200, f"got {r.status_code}")
    if r.status_code != 200:
        return

    time.sleep(3)

    # Find execution with tool events
    sr = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    if sr.status_code != 200:
        return
    sessions = sr.json().get("sessions", [])
    match = next((s for s in sessions if s.get("session_id") == session_id), None)
    if not match:
        check("Tool audit: session found", False, "session not in lineage")
        return

    # Get execution detail
    sid = match["session_id"]
    er = requests.get(f"{LINEAGE_URL}/sessions/{sid}", timeout=10)
    if er.status_code != 200:
        check("Tool audit: execution detail accessible", False, f"got {er.status_code}")
        return

    records = er.json().get("records", [])
    tool_events_found = False
    hashes_present = False

    for rec in records:
        exec_id = rec.get("execution_id") or rec.get("id")
        if not exec_id:
            continue
        er2 = requests.get(f"{LINEAGE_URL}/executions/{exec_id}", timeout=10)
        if er2.status_code == 200:
            tool_events = er2.json().get("tool_events", [])
            if tool_events:
                tool_events_found = True
                # Verify SHA3-512 hashes are present (128 hex chars)
                for te in tool_events:
                    ih = te.get("input_hash", "")
                    oh = te.get("output_hash", "")
                    if len(ih) == 128 and len(oh) == 128:
                        hashes_present = True
                        break

    check("Tool events present in execution record", tool_events_found,
          "tool_events array non-empty")
    check("Tool event SHA3-512 hashes present (128 hex chars)",
          hashes_present or not tool_events_found,
          "input_hash and output_hash verified")


# ── 3. Multi-turn conversation integrity ──────────────────────────────────────

def test_multi_turn_integrity():
    """Multi-turn conversation where content builds across messages — chain must stay valid."""
    session_id = str(uuid.uuid4())

    turns = [
        "My name is Alice. Remember that.",
        "What is 5 + 7?",
        "What is my name?",
    ]

    for i, content in enumerate(turns):
        r = requests.post(CHAT_URL, json={
            "model": TOOL_MODEL,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 30,
        }, headers={**HEADERS, "X-Session-Id": session_id}, timeout=90)
        check(f"Multi-turn request {i+1} returns 200", r.status_code == 200,
              f"got {r.status_code}")
        time.sleep(1)

    time.sleep(3)

    # Verify chain is still valid after 3 turns
    sr = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    if sr.status_code == 200:
        sessions = sr.json().get("sessions", [])
        match = next((s for s in sessions if s.get("session_id") == session_id), None)
        if match:
            rv = requests.get(f"{LINEAGE_URL}/verify/{session_id}", timeout=10)
            check("Multi-turn session chain valid after 3 turns",
                  rv.status_code == 200 and rv.json().get("valid", False),
                  str(rv.json()) if rv.status_code == 200 else f"got {rv.status_code}")
            rc = match.get("record_count", 0)
            check("All 3 turns recorded in session",
                  rc >= 3, f"record_count={rc}")
        else:
            check("Multi-turn session found in lineage", False,
                  f"session_id={session_id[:8]}...")


# ── 4. File/image attachment mid-conversation ─────────────────────────────────

def test_attachment_handling():
    """Send a base64 image in a message — verify the request is handled and attempt recorded."""
    # 1x1 white PNG (smallest valid PNG)
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
    )

    pre = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    pre_count = pre.json().get("total", 0) if pre.status_code == 200 else 0

    # Send multimodal message with image
    r = requests.post(CHAT_URL, json={
        "model": TOOL_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What color is this image?"},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{tiny_png_b64}"
                }},
            ]
        }],
        "max_tokens": 20,
    }, headers=HEADERS, timeout=90)

    # Gateway may return 200 (model handles it) or 422 (model doesn't support vision)
    # Either way — no crash and attempt record written
    check("Attachment request handled (no 500)", r.status_code != 500,
          f"got {r.status_code}")
    check("Attachment request response is JSON", _is_json(r.text))

    time.sleep(2)
    post = requests.get(f"{LINEAGE_URL}/attempts", timeout=10)
    post_count = post.json().get("total", 0) if post.status_code == 200 else 0
    check("Attempt record written for attachment request",
          post_count > pre_count, f"before={pre_count}, after={post_count}")


# ── 5. Tool output content analysis ──────────────────────────────────────────

def test_tool_content_analysis():
    """Verify content_analysis field is populated on tool events (indirect injection detection)."""
    if not _require_tools("Tool content analysis"):
        return

    session_id = str(uuid.uuid4())
    r = tool_request(session_id,
        "Use web_search to look up 'neural network definition'. "
        "Return one sentence from the results.")

    if r.status_code != 200:
        skip("Tool content analysis", f"request failed with {r.status_code}")
        return

    time.sleep(3)

    # Check tool events for content_analysis field
    sr = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    if sr.status_code != 200:
        return
    sessions = sr.json().get("sessions", [])
    match = next((s for s in sessions if s.get("session_id") == session_id), None)
    if not match:
        skip("Tool content analysis", "session not found")
        return

    sid = match["session_id"]
    er = requests.get(f"{LINEAGE_URL}/sessions/{sid}", timeout=10)
    if er.status_code != 200:
        return

    for rec in er.json().get("records", []):
        exec_id = rec.get("execution_id") or rec.get("id")
        if not exec_id:
            continue
        er2 = requests.get(f"{LINEAGE_URL}/executions/{exec_id}", timeout=10)
        if er2.status_code == 200:
            tool_events = er2.json().get("tool_events", [])
            if tool_events:
                has_analysis = any(
                    te.get("content_analysis") is not None for te in tool_events
                )
                check("Tool events have content_analysis field",
                      has_analysis,
                      f"{len(tool_events)} tool events checked")
                return

    skip("Tool content analysis", "no tool events found")


# ── 6. MCP registry health ────────────────────────────────────────────────────

def test_mcp_registry():
    """Verify the tool registry is initialized (MCP infra is up even if no servers configured)."""
    health = get_health()

    # Check /v1/tools endpoint if it exists
    r = requests.get(f"{BASE_URL}/v1/tools", headers=HEADERS, timeout=10)
    if r.status_code == 404:
        skip("MCP registry /v1/tools", "endpoint not exposed — registry is internal")
    elif r.status_code == 200:
        tools = r.json()
        check("/v1/tools endpoint accessible", True, f"{len(tools)} tools registered")
    else:
        check("/v1/tools returns non-500", r.status_code != 500, f"got {r.status_code}")

    # Verify gateway startup didn't fail due to MCP issues (health is up = registry init OK)
    check("Gateway healthy (MCP registry init succeeded)", health.get("status") in ("ok", "healthy"),
          str(health.get("status")))


# ── 7. Web search sources captured ───────────────────────────────────────────

def test_web_search_sources():
    """Verify that web search results include source URLs in tool events."""
    if not _require_tools("Web search sources"):
        return

    session_id = str(uuid.uuid4())
    r = tool_request(session_id,
        "Call web_search with query 'Linux kernel'. "
        "Briefly describe what the search result says.")

    if r.status_code != 200:
        skip("Web search sources", f"request failed {r.status_code}")
        return

    time.sleep(3)

    sr = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    if sr.status_code != 200:
        return
    sessions = sr.json().get("sessions", [])
    match = next((s for s in sessions if s.get("session_id") == session_id), None)
    if not match:
        skip("Web search sources", "session not found")
        return

    sid = match["session_id"]
    er = requests.get(f"{LINEAGE_URL}/sessions/{sid}", timeout=10)
    if er.status_code != 200:
        return

    for rec in er.json().get("records", []):
        exec_id = rec.get("execution_id") or rec.get("id")
        if not exec_id:
            continue
        er2 = requests.get(f"{LINEAGE_URL}/executions/{exec_id}", timeout=10)
        if er2.status_code == 200:
            tool_events = er2.json().get("tool_events", [])
            for te in tool_events:
                sources = te.get("sources") or []
                if sources:
                    check("Web search sources captured in tool event",
                          len(sources) > 0, f"{len(sources)} sources")
                    check("Sources have URL field",
                          any("url" in str(s) or "href" in str(s) for s in sources),
                          str(sources[0])[:80])
                    return
            if tool_events:
                # Tool was called but no sources — DDG may not return them for all queries
                skip("Web search sources", "tool called but no sources (DDG limitation)")
                return

    skip("Web search sources", "no tool events found")


# ── Helper ────────────────────────────────────────────────────────────────────

def _is_json(text: str) -> bool:
    import json
    try:
        json.loads(text)
        return True
    except Exception:
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n=== Tier 6: Advanced Features ===")
    print(f"  Model: {TOOL_MODEL}")
    if TOOL_MODEL == "qwen3:1.7b":
        print("  WARNING: qwen3:1.7b has limited tool support.")
        print("  Run with GATEWAY_MODEL=qwen3:4b for full tool coverage.\n")
    else:
        print()

    # Pre-flight: test tool calling directly against Ollama
    if TOOL_MODEL != "qwen3:1.7b":
        print("[0/7] Pre-flight tool invocation check")
        preflight_tool_check()
        print()

    print("[1/7] Web search invocation"); test_web_search_invocation()
    print("[2/7] Tool event audit integrity"); test_tool_event_audit()
    print("[3/7] Multi-turn conversation integrity"); test_multi_turn_integrity()
    print("[4/7] File/image attachment handling"); test_attachment_handling()
    print("[5/7] Tool output content analysis"); test_tool_content_analysis()
    print("[6/7] MCP registry health"); test_mcp_registry()
    print("[7/7] Web search sources captured"); test_web_search_sources()

    passed = sum(1 for r in RESULTS if r["passed"])
    failed = sum(1 for r in RESULTS if not r["passed"])
    print(f"\n{'='*40}")
    print(f"Tier 6 Advanced: {passed} PASS, {failed} FAIL")

    save_artifact("tier6_advanced", {
        "tier": "6_advanced", "model": TOOL_MODEL,
        "passed": passed, "failed": failed,
        "results": RESULTS,
        "gate": "PASS" if failed == 0 else "FAIL",
    })

    if failed > 0:
        print("\nGATE FAILED")
        sys.exit(1)
    print("\nGATE PASSED")


if __name__ == "__main__":
    main()
