#!/usr/bin/env python3
"""Walacor Gateway quickstart demo.

Demonstrates:
1. Pulling qwen3:1.7b from Ollama (if not already cached)
2. Sending a chat request through the gateway
3. Showing the audit record fields, execution_id, and tool interactions

Usage (local):
    python demo/quickstart.py

Usage (Docker Compose demo profile):
    docker compose --profile demo up
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEMO_MODEL = os.environ.get("DEMO_MODEL", "qwen3:1.7b")


def _request(url: str, method: str = "GET", data: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    payload = json.dumps(data).encode() if data else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        return exc.code, json.loads(body) if body else {}
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc)}


def wait_for_gateway(max_wait: int = 60) -> bool:
    print(f"[demo] Waiting for gateway at {GATEWAY_URL}/health ...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        code, body = _request(f"{GATEWAY_URL}/health")
        if code == 200:
            print(f"[demo] Gateway is ready: status={body.get('status')}")
            return True
        time.sleep(2)
    print("[demo] Timed out waiting for gateway.")
    return False


def pull_model(model: str) -> bool:
    """Pull model from Ollama if not already available."""
    print(f"[demo] Checking if {model} is available in Ollama ...")
    code, body = _request(f"{OLLAMA_URL}/api/tags")
    if code != 200:
        print(f"[demo] Ollama not reachable at {OLLAMA_URL}: {body}")
        return False

    models = [m.get("name", "") for m in body.get("models", [])]
    if any(model in m for m in models):
        print(f"[demo] Model {model} already present.")
        return True

    print(f"[demo] Pulling {model} from Ollama (this may take a few minutes) ...")
    # Use streaming pull
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/pull",
        data=json.dumps({"name": model}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for line in resp:
                progress = json.loads(line.decode())
                status = progress.get("status", "")
                if "error" in progress:
                    print(f"[demo] Pull error: {progress['error']}")
                    return False
                if status == "success":
                    print(f"[demo] {model} pulled successfully.")
                    return True
                if "completed" in progress and "total" in progress:
                    pct = round(progress["completed"] / progress["total"] * 100)
                    print(f"\r[demo] Pulling {model}: {status} {pct}%", end="", flush=True)
        print()
        return True
    except Exception as exc:
        print(f"[demo] Pull failed: {exc}")
        return False


def send_chat(prompt: str, model: str) -> dict:
    """Send a chat request through the Walacor gateway."""
    print(f"\n[demo] Sending request → {GATEWAY_URL}/v1/chat/completions")
    print(f"[demo] Model: {model}")
    print(f"[demo] Prompt: {prompt!r}")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    t0 = time.time()
    code, body = _request(
        f"{GATEWAY_URL}/v1/chat/completions",
        method="POST",
        data=payload,
        timeout=60,
    )
    elapsed = round((time.time() - t0) * 1000)
    print(f"[demo] Response: HTTP {code} ({elapsed}ms)")
    return body


def show_result(body: dict) -> None:
    if "error" in body:
        print(f"[demo] Error: {body['error']}")
        return

    choices = body.get("choices", [])
    if choices:
        content = (choices[0].get("message") or {}).get("content", "")
        print(f"\n[demo] Model response:\n{'-' * 60}\n{content}\n{'-' * 60}")

    usage = body.get("usage", {})
    if usage:
        print(f"[demo] Token usage: prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')} "
              f"total={usage.get('total_tokens')}")

    request_id = body.get("id")
    if request_id:
        print(f"[demo] Provider request_id: {request_id}")


def show_audit_info() -> None:
    """Show what the gateway records in the audit trail."""
    print("\n[demo] Audit trail fields written to WAL/Walacor:")
    print("  • execution_id   — UUID for this inference event")
    print("  • prompt_text    — full prompt sent to the model")
    print("  • response_content — model response (thinking stripped if <think> blocks present)")
    print("  • thinking_content — extracted <think>...</think> reasoning (Phase 17)")
    print("  • model_hash     — Ollama model digest (integrity proof)")
    print("  • policy_result  — governance decision (pass/deny/audit_only)")
    print("  • record_hash    — Merkle chain hash (tamper-proof session chain)")
    print("  • tool_interactions — list of MCP tool calls + hashed input/output")
    print("\n[demo] OTel span attributes (when WALACOR_OTEL_ENABLED=true):")
    print("  gen_ai.system, gen_ai.request.model, gen_ai.usage.input_tokens,")
    print("  gen_ai.usage.output_tokens, walacor.execution_id, walacor.policy_result")


def show_env_tip() -> None:
    print("\n[demo] Tip — to point any OpenAI-compatible client at this gateway:")
    print(f"  export OPENAI_BASE_URL={GATEWAY_URL}/v1")
    print("  export OPENAI_API_KEY=any-value  # gateway uses WALACOR_GATEWAY_API_KEYS")
    print("\n[demo] Llama Guard safety classifier (Phase 17):")
    print("  docker exec <ollama-container> ollama pull llama-guard3")
    print("  WALACOR_LLAMA_GUARD_ENABLED=true docker compose --profile demo up")


def main() -> int:
    print("=" * 60)
    print("  Walacor Gateway — Phase 17 Demo")
    print("=" * 60)

    if not wait_for_gateway():
        return 1

    if not pull_model(DEMO_MODEL):
        print(f"[demo] Could not pull {DEMO_MODEL}. Proceeding anyway (model may already be cached).")

    body = send_chat("What is the capital of France? Be brief.", DEMO_MODEL)
    show_result(body)
    show_audit_info()
    show_env_tip()

    print("\n[demo] Demo complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
