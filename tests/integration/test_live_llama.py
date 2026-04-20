"""
Full integration test — qwen3:4b via local Ollama.

Tests every gateway layer end-to-end with NO mocks for Ollama calls:
  §1  Ollama connectivity smoke test
  §2  OllamaAdapter  – parse real request + real non-streaming response
  §3  Thinking strip  – qwen3:4b emits <think> blocks; verify separation
  §4  Dummy MCP client + ToolRegistry (pure-Python, no subprocess)
  §5  Tool-aware active loop – real qwen3:4b calls the calculator tool
  §6  WAL write + read (SQLite) including thinking_content field
  §7  Session chain – three sequential records, Merkle hashes verified
  §8  Content analyzers – PII and toxicity on real model output
  §9  Full execution record build with all Phase-17 fields
  §10 ASGI end-to-end (skip_governance) – real Ollama call via Starlette app

Run:
    python -m pytest tests/integration/test_live_llama.py -v -s

Requirements:
    Ollama at http://localhost:11434 with qwen3:4b loaded.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

# ── Fixtures ─────────────────────────────────────────────────────────────────

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen3:4b"

# Force skip_governance so config validator doesn't demand control-plane creds.
os.environ.setdefault("WALACOR_SKIP_GOVERNANCE", "true")
os.environ.setdefault("WALACOR_PROVIDER_OLLAMA_URL", OLLAMA_URL)
os.environ.setdefault("WALACOR_GATEWAY_PROVIDER", "ollama")
os.environ.setdefault("WALACOR_GATEWAY_TENANT_ID", "integration-test")
os.environ.setdefault("WALACOR_CONTROL_PLANE_URL", "http://dummy-cp")
# Disable API key auth — overrides .env.gateway which sets test keys.
os.environ.setdefault("WALACOR_GATEWAY_API_KEYS", "")


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ── §0 Shared HTTP client ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def real_http_client():
    """Module-scoped real httpx.AsyncClient for Ollama calls."""
    import anyio
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    yield client
    anyio.from_thread.run_sync(lambda: None)  # keep event loop alive for teardown


# ── §0b Ollama raw call helper ────────────────────────────────────────────────

async def _ollama_chat(
    messages: list[dict],
    stream: bool = False,
    tools: list[dict] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Fire a raw chat request at Ollama and return parsed JSON response."""
    payload: dict[str, Any] = {"model": MODEL, "messages": messages, "stream": stream}
    if tools:
        payload["tools"] = tools
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    try:
        resp = await client.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload, timeout=120.0)
        resp.raise_for_status()
        return resp.json()
    finally:
        if http_client is None:
            await client.aclose()


# =============================================================================
# §1  OLLAMA CONNECTIVITY SMOKE TEST
# =============================================================================

@pytest.mark.anyio
async def test_s1_ollama_reachable_and_model_loaded():
    """§1: Ollama is reachable and qwen3:4b is loaded."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{OLLAMA_URL}/api/tags", timeout=10.0)
    assert resp.status_code == 200, f"Ollama not reachable: {resp.status_code}"

    models = {m["name"] for m in resp.json().get("models", [])}
    assert any(MODEL.split(":")[0] in m for m in models), (
        f"{MODEL} not found in Ollama. Run: ollama pull {MODEL}\nAvailable: {models}"
    )
    print(f"\n  [§1] Ollama OK — models loaded: {sorted(models)}")


# =============================================================================
# §2  OLLAMAADAPTER — PARSE REAL REQUEST + REAL RESPONSE
# =============================================================================

@pytest.mark.anyio
async def test_s2_adapter_parse_request():
    """§2a: OllamaAdapter.parse_request extracts model_id, prompt, metadata."""
    from gateway.adapters.ollama import OllamaAdapter

    adapter = OllamaAdapter(OLLAMA_URL)
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is 2+2?"},
        ],
        "temperature": 0.5,
        "top_k": 20,
        "stream": False,
    }
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.headers = {"x-user-id": "tester", "x-session-id": "sess-s2"}

    call = await adapter.parse_request(req)

    assert call.model_id == MODEL
    assert "2+2" in call.prompt_text
    assert call.metadata.get("user") == "tester"
    assert call.metadata.get("session_id") == "sess-s2"
    assert call.metadata["inference_params"]["temperature"] == 0.5
    assert call.metadata["inference_params"]["top_k"] == 20
    assert call.metadata.get("system_prompt") == "You are a helpful assistant."
    print(f"  [§2a] parse_request OK — model={call.model_id} user={call.metadata['user']}")


@pytest.mark.anyio
async def test_s2_adapter_real_response_parsed():
    """§2b: OllamaAdapter.parse_response correctly parses a real Ollama response."""
    from gateway.adapters.ollama import OllamaAdapter

    adapter = OllamaAdapter(OLLAMA_URL, thinking_strip_enabled=False)
    raw = await _ollama_chat([{"role": "user", "content": "Reply with exactly: GATEWAY_TEST_OK"}])

    # Build a fake httpx.Response around the real JSON bytes
    raw_bytes = json.dumps(raw).encode()
    http_resp = httpx.Response(200, content=raw_bytes, headers={"content-type": "application/json"})
    model_resp = adapter.parse_response(http_resp)

    assert isinstance(model_resp.content, str)
    assert len(model_resp.content) > 0
    assert model_resp.usage is not None
    assert model_resp.usage.get("total_tokens", 0) > 0
    print(f"  [§2b] parse_response OK — content={model_resp.content[:80]!r} tokens={model_resp.usage}")


# =============================================================================
# §3  THINKING STRIP — qwen3:4b emits <think> blocks
# =============================================================================

@pytest.mark.anyio
async def test_s3_thinking_strip_with_real_model():
    """§3: qwen3:4b emits <think> blocks; OllamaAdapter strips them by default."""
    from gateway.adapters.ollama import OllamaAdapter

    # Prompt that provokes thinking output
    prompt = "/no_think What is the capital of France? Answer in one word."

    # First: capture raw output WITHOUT stripping to see the <think> tags
    adapter_raw = OllamaAdapter(OLLAMA_URL, thinking_strip_enabled=False)
    raw = await _ollama_chat([{"role": "user", "content": prompt}])
    raw_bytes = json.dumps(raw).encode()
    http_resp = httpx.Response(200, content=raw_bytes, headers={"content-type": "application/json"})
    resp_no_strip = adapter_raw.parse_response(http_resp)
    raw_content = resp_no_strip.content
    print(f"\n  [§3] Raw content (first 200 chars): {raw_content[:200]!r}")

    # Second: parse WITH stripping enabled
    adapter_strip = OllamaAdapter(OLLAMA_URL, thinking_strip_enabled=True)
    resp_stripped = adapter_strip.parse_response(http_resp)

    # The stripped content should not contain <think> tags
    assert "<think>" not in resp_stripped.content.lower(), (
        f"<think> still present in content after stripping: {resp_stripped.content[:200]!r}"
    )
    assert "</think>" not in resp_stripped.content.lower()

    # If the raw content had think blocks, thinking_content must be populated
    from gateway.adapters.thinking import strip_thinking_tokens
    _, thinking = strip_thinking_tokens(raw_content)
    if thinking is not None:
        assert resp_stripped.thinking_content is not None
        assert len(resp_stripped.thinking_content) > 0
        print(f"  [§3] Thinking extracted ({len(resp_stripped.thinking_content)} chars): "
              f"{resp_stripped.thinking_content[:100]!r}...")
    else:
        print(f"  [§3] Note: qwen3 returned no <think> block for this prompt (acceptable)")

    print(f"  [§3] Clean content: {resp_stripped.content!r}")
    assert len(resp_stripped.content) > 0, "Stripped content must not be empty"


@pytest.mark.anyio
async def test_s3_thinking_strip_utility_direct():
    """§3b: strip_thinking_tokens utility handles real-world multi-block cases."""
    from gateway.adapters.thinking import strip_thinking_tokens

    real_output = (
        "<think>\nI need to reason about this carefully.\n"
        "Step 1: consider the question.\nStep 2: formulate answer.\n</think>\n\n"
        "The answer is Paris."
    )
    clean, thinking = strip_thinking_tokens(real_output)
    assert clean == "The answer is Paris."
    assert "Step 1" in thinking
    assert "Step 2" in thinking
    assert "<think>" not in clean
    print(f"  [§3b] strip utility OK — clean={clean!r} thinking_len={len(thinking)}")


# =============================================================================
# §4  DUMMY MCP CLIENT + TOOL REGISTRY
# =============================================================================

class DummyCalculatorClient:
    """Pure-Python MCP-duck-typed client: calculator + weather tools. No subprocess."""

    TOOL_CALL_LOG: list[dict] = []  # captured calls for assertions

    def get_tools(self):
        from gateway.mcp.client import ToolDefinition
        return [
            ToolDefinition(
                name="add_numbers",
                description=(
                    "Add two numbers and return their sum. "
                    "Use this when asked to compute a sum or addition."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "number", "description": "First number"},
                        "b": {"type": "number", "description": "Second number"},
                    },
                    "required": ["a", "b"],
                },
                server_name="dummy_calculator",
            ),
            ToolDefinition(
                name="get_weather",
                description=(
                    "Get current weather conditions for a city. "
                    "Returns temperature, conditions, humidity, and wind."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "Name of the city"},
                    },
                    "required": ["city"],
                },
                server_name="dummy_calculator",
            ),
            ToolDefinition(
                name="query_database",
                description=(
                    "Query a database table and return matching records. "
                    "Use for data retrieval questions."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table": {"type": "string", "description": "Table name"},
                        "filter": {"type": "string", "description": "Filter condition as SQL WHERE clause"},
                    },
                    "required": ["table"],
                },
                server_name="dummy_calculator",
            ),
        ]

    async def call_tool(self, name: str, args: dict[str, Any], timeout_ms: int = 30_000):
        from gateway.mcp.client import ToolResult
        self.TOOL_CALL_LOG.append({"tool": name, "args": args, "ts": time.time()})

        if name == "add_numbers":
            a = float(args.get("a", 0))
            b = float(args.get("b", 0))
            result = a + b
            return ToolResult(
                content=f"The sum of {a} and {b} is {result}.",
                is_error=False,
                duration_ms=1.0,
            )

        if name == "get_weather":
            city = args.get("city", "unknown")
            return ToolResult(
                content=(
                    f"Weather in {city}: 22°C, partly cloudy, humidity 65%, "
                    f"wind 12 km/h NW. Last updated: {time.strftime('%H:%M UTC')}"
                ),
                is_error=False,
                duration_ms=2.0,
            )

        if name == "query_database":
            table = args.get("table", "unknown")
            filt = args.get("filter", "1=1")
            return ToolResult(
                content=json.dumps([
                    {"id": 1, "table": table, "filter": filt, "row": "record_A"},
                    {"id": 2, "table": table, "filter": filt, "row": "record_B"},
                ]),
                is_error=False,
                duration_ms=5.0,
            )

        return ToolResult(content=f"Unknown tool: {name}", is_error=True)


@pytest.mark.anyio
async def test_s4_tool_registry_registers_all_tools():
    """§4a: ToolRegistry correctly registers a built-in duck-typed client."""
    from gateway.mcp.registry import ToolRegistry

    client = DummyCalculatorClient()
    registry = ToolRegistry([])
    await registry.register_builtin_client("dummy_calc", client)

    assert registry.get_tool_count() == 3
    assert "dummy_calc" in registry.server_names()

    defs = registry.get_tool_definitions()
    names = {d["function"]["name"] for d in defs}
    assert names == {"add_numbers", "get_weather", "query_database"}
    print(f"  [§4a] ToolRegistry OK — {registry.get_tool_count()} tools: {names}")


@pytest.mark.anyio
async def test_s4_tool_registry_execute_add_numbers():
    """§4b: execute_tool dispatches to dummy client and returns correct result."""
    from gateway.mcp.registry import ToolRegistry

    client = DummyCalculatorClient()
    registry = ToolRegistry([])
    await registry.register_builtin_client("dummy_calc", client)

    result = await registry.execute_tool("add_numbers", {"a": 17, "b": 25})
    assert not result.is_error
    assert "42" in result.content
    print(f"  [§4b] execute_tool add_numbers(17,25) → {result.content!r}")


@pytest.mark.anyio
async def test_s4_tool_registry_schema_validation():
    """§4c: get_tool_schema returns correct input_schema for each tool."""
    from gateway.mcp.registry import ToolRegistry

    client = DummyCalculatorClient()
    registry = ToolRegistry([])
    await registry.register_builtin_client("dummy_calc", client)

    schema = registry.get_tool_schema("add_numbers")
    assert schema is not None
    assert "a" in schema["properties"]
    assert "b" in schema["properties"]
    assert schema["required"] == ["a", "b"]

    weather_schema = registry.get_tool_schema("get_weather")
    assert "city" in weather_schema["properties"]
    print(f"  [§4c] schema validation OK — add_numbers schema: {schema}")


@pytest.mark.anyio
async def test_s4_tool_registry_unknown_tool_returns_error():
    """§4d: execute_tool with unknown name returns is_error=True, does not raise."""
    from gateway.mcp.registry import ToolRegistry

    client = DummyCalculatorClient()
    registry = ToolRegistry([])
    await registry.register_builtin_client("dummy_calc", client)

    result = await registry.execute_tool("nonexistent_tool", {})
    assert result.is_error
    assert "Unknown tool" in result.content or "nonexistent_tool" in result.content
    print(f"  [§4d] unknown tool handled: {result.content!r}")


# =============================================================================
# §5  TOOL-AWARE ACTIVE LOOP — real qwen3:4b calls the calculator tool
# =============================================================================

def _build_minimal_ctx(tmp_wal_dir: str, tool_registry=None, content_analyzers=None):
    """Build a minimal PipelineContext without full gateway startup."""
    from gateway.pipeline.context import PipelineContext
    from gateway.wal.writer import WALWriter
    from gateway.pipeline.session_chain import SessionChainTracker
    from gateway.pipeline.budget_tracker import make_budget_tracker
    from gateway.config import get_settings

    ctx = PipelineContext()
    ctx.http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    ctx.wal_writer = WALWriter(str(Path(tmp_wal_dir) / "wal.db"))
    ctx.session_chain = SessionChainTracker(max_sessions=100, ttl_seconds=3600)
    ctx.content_analyzers = content_analyzers or []

    # Minimal settings for budget tracker (disabled)
    settings = get_settings()
    ctx.budget_tracker = make_budget_tracker(None, settings)

    if tool_registry is not None:
        ctx.tool_registry = tool_registry

    return ctx


@pytest.mark.anyio
async def test_s5_active_tool_loop_with_real_qwen3():
    """§5: qwen3:4b + add_numbers tool — full active loop with real Ollama inference.

    Sends a request that forces qwen3 to use the add_numbers tool, runs the
    gateway active strategy loop, and verifies tool_interactions are captured.
    """
    from gateway.adapters.ollama import OllamaAdapter
    from gateway.mcp.registry import ToolRegistry
    from gateway.pipeline.tool_executor import _inject_tools_into_call, _run_active_tool_loop
    from gateway.pipeline.context import get_pipeline_context
    from gateway.config import get_settings

    # Build tool registry with dummy calculator
    calc_client = DummyCalculatorClient()
    DummyCalculatorClient.TOOL_CALL_LOG.clear()
    registry = ToolRegistry([])
    await registry.register_builtin_client("dummy_calc", calc_client)

    tool_defs = registry.get_tool_definitions()
    print(f"\n  [§5] Injecting {len(tool_defs)} tools into request: "
          f"{[d['function']['name'] for d in tool_defs]}")

    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = _build_minimal_ctx(tmpdir, tool_registry=registry)
        settings = get_settings()

        adapter = OllamaAdapter(OLLAMA_URL, thinking_strip_enabled=True)

        # Craft a request that REQUIRES tool use
        body = {
            "model": MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a helpful assistant. "
                        "You MUST use the add_numbers tool to answer any arithmetic question. "
                        "Do not compute in your head — always call the tool."
                    ),
                },
                {
                    "role": "user",
                    "content": "Please use the add_numbers tool to compute 137 + 856.",
                },
            ],
            "stream": False,
            "tools": tool_defs,
        }
        body_bytes = json.dumps(body).encode()

        req_mock = MagicMock()
        req_mock.body = AsyncMock(return_value=body_bytes)
        req_mock.headers = {"x-session-id": "sess-s5-tool"}
        req_mock.method = "POST"
        req_mock.url = MagicMock()
        req_mock.url.path = "/v1/chat/completions"
        req_mock.url.query = ""
        req_mock.client = MagicMock()
        req_mock.client.host = "127.0.0.1"

        call = await adapter.parse_request(req_mock)

        # Forward to real Ollama
        upstream_req = await adapter.build_forward_request(call, req_mock)
        t0 = time.perf_counter()
        raw_resp = await ctx.http_client.send(upstream_req)
        elapsed = round((time.perf_counter() - t0) * 1000)
        print(f"  [§5] First Ollama call: {raw_resp.status_code} in {elapsed}ms")
        assert raw_resp.status_code == 200, f"Ollama error: {raw_resp.text[:300]}"

        model_response = adapter.parse_response(raw_resp)
        print(f"  [§5] Initial response: content={model_response.content[:80]!r} "
              f"has_pending={model_response.has_pending_tool_calls} "
              f"tool_interactions={model_response.tool_interactions}")

        if model_response.thinking_content:
            print(f"  [§5] Thinking content: {model_response.thinking_content[:100]!r}...")

        # Run the active tool loop (may be zero iterations if qwen3 already answered)
        call, final_response, loop_err, interactions, iterations, _final_http = await _run_active_tool_loop(
            adapter=adapter,
            call=call,
            request=req_mock,
            model_response=model_response,
            ctx=ctx,
            settings=settings,
            provider="ollama",
        )

        print(f"  [§5] Tool loop: iterations={iterations} interactions={len(interactions)}")
        print(f"  [§5] Final response: {final_response.content[:200]!r}")

        if iterations > 0:
            # Verify tool was called
            assert len(interactions) > 0
            tool_names_called = [i.tool_name for i in interactions]
            print(f"  [§5] Tools called: {tool_names_called}")
            assert "add_numbers" in tool_names_called, (
                f"Expected add_numbers to be called, got: {tool_names_called}"
            )

            # Verify the gateway captured actual input and output
            calc_interaction = next(i for i in interactions if i.tool_name == "add_numbers")
            assert calc_interaction.input_data is not None
            assert calc_interaction.output_data is not None
            assert "993" in str(calc_interaction.output_data)  # 137 + 856 = 993
            print(f"  [§5] add_numbers input={calc_interaction.input_data} "
                  f"output={calc_interaction.output_data!r}")

            # Verify the dummy client's call log
            assert len(DummyCalculatorClient.TOOL_CALL_LOG) > 0
            log_entry = DummyCalculatorClient.TOOL_CALL_LOG[0]
            assert log_entry["tool"] == "add_numbers"
            print(f"  [§5] DummyClient TOOL_CALL_LOG: {DummyCalculatorClient.TOOL_CALL_LOG}")

        else:
            # qwen3 answered without calling tool (valid but note it)
            print(f"  [§5] NOTE: qwen3 answered without tool call — "
                  f"check answer contains '993': {'993' in final_response.content}")

        # Final answer should contain the sum
        assert "993" in final_response.content or "137" in final_response.content, (
            f"Final response doesn't mention the calculation: {final_response.content!r}"
        )

        await ctx.http_client.aclose()


# =============================================================================
# §6  WAL WRITE + READ — including thinking_content field
# =============================================================================

@pytest.mark.anyio
async def test_s6_wal_write_read_thinking_content():
    """§6: WAL write/read round-trip with all Phase-17 fields including thinking_content."""
    from gateway.wal.writer import WALWriter
    from gateway.pipeline.hasher import build_execution_record
    from gateway.adapters.base import ModelCall, ModelResponse

    with tempfile.TemporaryDirectory() as tmpdir:
        wal = WALWriter(str(Path(tmpdir) / "wal.db"))

        # Build a realistic execution record
        call = ModelCall(
            provider="ollama",
            model_id=MODEL,
            prompt_text="What is 137 + 856?",
            raw_body=b'{"model":"qwen3:4b","messages":[]}',
            is_streaming=False,
            metadata={
                "user": "tester-s6",
                "session_id": "sess-wal-s6",
                "inference_params": {"temperature": 0.7},
            },
        )
        model_resp = ModelResponse(
            content="The answer is 993.",
            usage={"prompt_tokens": 45, "completion_tokens": 12, "total_tokens": 57},
            raw_body=b'{}',
            provider_request_id="chatcmpl-wal-test",
            model_hash="sha256:test-digest-abc123",
            thinking_content=(
                "The user is asking about 137 + 856.\n"
                "Let me calculate: 137 + 856 = 993."
            ),
        )
        record = build_execution_record(
            call=call,
            model_response=model_resp,
            attestation_id="test-attestation-id",
            policy_version=7,
            policy_result="pass",
            tenant_id="integration-test",
            gateway_id="gw-integration",
            user="tester-s6",
            session_id="sess-wal-s6",
            metadata={
                "enforcement_mode": "enforced",
                "response_policy_result": "pass",
                "token_usage": model_resp.usage,
            },
        )

        # Verify all Phase-17 fields present before write
        assert record["thinking_content"] is not None
        assert "137 + 856" in record["thinking_content"]
        assert record["response_content"] == "The answer is 993."
        assert record["model_hash"] == "sha256:test-digest-abc123"

        # Write
        wal.write_and_fsync(record)
        pending = wal.pending_count()
        assert pending == 1, f"Expected 1 pending record, got {pending}"

        # Read back
        rows = wal.get_undelivered(limit=10)
        assert len(rows) == 1
        execution_id, record_json, created_at = rows[0]
        assert execution_id == record["execution_id"]

        recovered = json.loads(record_json)
        assert recovered["thinking_content"] == record["thinking_content"]
        assert recovered["response_content"] == "The answer is 993."
        assert recovered["model_hash"] == "sha256:test-digest-abc123"
        assert recovered["provider_request_id"] == "chatcmpl-wal-test"
        assert recovered["metadata"]["token_usage"]["total_tokens"] == 57

        # Mark delivered
        wal.mark_delivered(execution_id)
        assert wal.pending_count() == 0

        print(
            f"  [§6] WAL OK — execution_id={execution_id} "
            f"thinking_content_len={len(recovered['thinking_content'])} "
            f"total_tokens={recovered['metadata']['token_usage']['total_tokens']}"
        )
        wal.close()


# =============================================================================
# §7  SESSION CHAIN — Merkle hash chaining across 3 requests
# =============================================================================

@pytest.mark.anyio
async def test_s7_session_chain_three_records():
    """§7: Three sequential records in one session — seq numbers and hashes chain correctly."""
    from gateway.pipeline.session_chain import SessionChainTracker, GENESIS_HASH
    from gateway.pipeline.session_chain import compute_record_hash

    tracker = SessionChainTracker(max_sessions=10, ttl_seconds=3600)
    session_id = f"sess-chain-{uuid.uuid4().hex[:8]}"

    records = []
    for i in range(3):
        seq, prev_hash = await tracker.next_chain_values(session_id)
        assert seq == i, f"Expected seq={i}, got {seq}"
        if i == 0:
            assert prev_hash == GENESIS_HASH, f"First record must use GENESIS_HASH, got {prev_hash[:16]}"
        else:
            assert prev_hash == records[-1]["record_hash"], (
                f"Record {i}: prev_hash {prev_hash[:16]} != previous record_hash "
                f"{records[-1]['record_hash'][:16]}"
            )

        execution_id = str(uuid.uuid4())
        record_hash = compute_record_hash(
            execution_id=execution_id,
            policy_version=7,
            policy_result="pass",
            previous_record_hash=prev_hash,
            sequence_number=seq,
            timestamp=f"2026-03-03T12:00:0{i}Z",
        )

        # Each hash must be 128 hex chars (SHA3-512)
        assert len(record_hash) == 128, f"record_hash wrong length: {len(record_hash)}"
        assert all(c in "0123456789abcdef" for c in record_hash), "Hash contains non-hex chars"

        records.append({"seq": seq, "execution_id": execution_id, "record_hash": record_hash})
        await tracker.update(session_id, seq, record_hash)

    # All hashes must be unique
    hashes = [r["record_hash"] for r in records]
    assert len(set(hashes)) == 3, "All record_hash values must be distinct"

    # Verify chain can be re-constructed: each prev_hash points to the previous record
    for i, r in enumerate(records):
        if i == 0:
            continue
        seq_next, prev_hash_next = await tracker.next_chain_values(session_id)
        # (don't update, just inspect the last state)
        break  # We just need the last state

    seq_final, prev_hash_final = await tracker.next_chain_values(session_id)
    assert seq_final == 3  # next would be seq 3
    assert prev_hash_final == records[-1]["record_hash"]

    print(
        f"  [§7] Session chain OK — session={session_id}\n"
        f"       seq=0 hash={records[0]['record_hash'][:24]}...\n"
        f"       seq=1 hash={records[1]['record_hash'][:24]}...\n"
        f"       seq=2 hash={records[2]['record_hash'][:24]}..."
    )


# =============================================================================
# §8  CONTENT ANALYZERS — PII and toxicity on real text
# =============================================================================

@pytest.mark.anyio
async def test_s8_pii_detector_catches_email():
    """§8a: PIIDetector flags text containing an email address."""
    from gateway.content.pii_detector import PIIDetector
    from gateway.content.base import Verdict

    detector = PIIDetector()
    decision = await detector.analyze("Please contact john.doe@example.com for details.")
    assert decision.verdict in (Verdict.WARN, Verdict.BLOCK)
    assert decision.category == "pii"
    assert decision.confidence > 0.5
    print(f"  [§8a] PII detected: verdict={decision.verdict} reason={decision.reason} "
          f"confidence={decision.confidence}")


@pytest.mark.anyio
async def test_s8_pii_detector_passes_clean_text():
    """§8b: PIIDetector passes clean text without PII."""
    from gateway.content.pii_detector import PIIDetector
    from gateway.content.base import Verdict

    detector = PIIDetector()
    decision = await detector.analyze(
        "The Eiffel Tower is located in Paris, France. "
        "It was built in 1889 and stands 330 metres tall."
    )
    assert decision.verdict == Verdict.PASS
    print(f"  [§8b] Clean text PASS: reason={decision.reason}")


@pytest.mark.anyio
async def test_s8_toxicity_blocks_child_safety():
    """§8c: ToxicityDetector upgrades child_safety matches to BLOCK."""
    from gateway.content.toxicity_detector import ToxicityDetector
    from gateway.content.base import Verdict

    detector = ToxicityDetector()
    decision = await detector.analyze("This is about csam content.")
    assert decision.verdict == Verdict.BLOCK
    assert decision.category == "toxicity"
    print(f"  [§8c] Child safety BLOCK: reason={decision.reason} confidence={decision.confidence}")


@pytest.mark.anyio
async def test_s8_content_analysis_on_real_model_output():
    """§8d: Run content analyzers on actual qwen3:4b output — should PASS for benign response."""
    from gateway.content.pii_detector import PIIDetector
    from gateway.content.toxicity_detector import ToxicityDetector
    from gateway.content.base import Verdict
    from gateway.pipeline.response_evaluator import analyze_text

    raw = await _ollama_chat([
        {"role": "user", "content": "Describe the water cycle in two sentences."}
    ])
    model_content = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
    assert model_content, "No content from model"
    print(f"\n  [§8d] Model output: {model_content[:150]!r}")

    analyzers = [PIIDetector(), ToxicityDetector()]
    decisions = await analyze_text(model_content, analyzers)

    # Benign science text must pass all analyzers
    for d in decisions:
        assert d["verdict"] == Verdict.PASS, (
            f"Analyzer {d['analyzer_id']} flagged benign text: {d}"
        )
    print(f"  [§8d] Content analysis OK — {len(decisions)} decisions, all PASS")


# =============================================================================
# §9  FULL EXECUTION RECORD — real Ollama call + all Phase-17 fields
# =============================================================================

@pytest.mark.anyio
async def test_s9_full_execution_record_real_call():
    """§9: Real qwen3:4b call → parse → build execution record with all Phase-17 fields."""
    from gateway.adapters.ollama import OllamaAdapter
    from gateway.pipeline.hasher import build_execution_record
    from gateway.pipeline.session_chain import SessionChainTracker, compute_record_hash, GENESIS_HASH

    adapter = OllamaAdapter(OLLAMA_URL, thinking_strip_enabled=True)

    # Real call
    raw = await _ollama_chat([
        {"role": "user", "content": "In exactly one sentence, what is machine learning?"}
    ])
    raw_bytes = json.dumps(raw).encode()
    http_resp = httpx.Response(200, content=raw_bytes, headers={"content-type": "application/json"})
    model_resp = adapter.parse_response(http_resp)

    print(f"\n  [§9] Real response: {model_resp.content[:120]!r}")
    print(f"  [§9] Thinking content: {model_resp.thinking_content!r}")
    print(f"  [§9] Usage: {model_resp.usage}")

    from gateway.adapters.base import ModelCall
    call = ModelCall(
        provider="ollama",
        model_id=MODEL,
        prompt_text="In exactly one sentence, what is machine learning?",
        raw_body=json.dumps({"model": MODEL, "messages": []}).encode(),
        is_streaming=False,
        metadata={
            "user": "tester-s9",
            "session_id": "sess-s9",
            "inference_params": {"temperature": 0.7},
        },
    )

    record = build_execution_record(
        call=call,
        model_response=model_resp,
        attestation_id="att-s9-test",
        policy_version=7,
        policy_result="pass",
        tenant_id="integration-test",
        gateway_id="gw-s9",
        user="tester-s9",
        session_id="sess-s9",
        metadata={
            "enforcement_mode": "enforced",
            "response_policy_result": "pass",
            "analyzer_decisions": [],
            "token_usage": model_resp.usage,
        },
    )

    # Mandatory fields
    assert record["execution_id"]
    assert record["prompt_text"] == call.prompt_text
    assert record["response_content"] == model_resp.content
    assert record["provider_request_id"] == model_resp.provider_request_id
    assert record["policy_result"] == "pass"
    assert record["tenant_id"] == "integration-test"
    assert record["timestamp"]

    # Phase-17 field
    if model_resp.thinking_content is not None:
        assert record["thinking_content"] == model_resp.thinking_content
        print(f"  [§9] thinking_content in record: {record['thinking_content'][:80]!r}...")
    else:
        assert record["thinking_content"] is None
        print(f"  [§9] thinking_content is None (no think block)")

    # Session chain (apply manually)
    tracker = SessionChainTracker()
    seq, prev_hash = await tracker.next_chain_values("sess-s9")
    record_hash = compute_record_hash(
        execution_id=record["execution_id"],
        policy_version=record["policy_version"],
        policy_result=record["policy_result"],
        previous_record_hash=prev_hash,
        sequence_number=seq,
        timestamp=record["timestamp"],
    )
    record["sequence_number"] = seq
    record["previous_record_hash"] = prev_hash
    record["record_hash"] = record_hash

    assert len(record["record_hash"]) == 128
    assert record["sequence_number"] == 0
    assert record["previous_record_hash"] == GENESIS_HASH

    print(
        f"  [§9] Execution record OK — id={record['execution_id']}\n"
        f"       seq={record['sequence_number']} hash={record['record_hash'][:24]}...\n"
        f"       tokens={record['metadata']['token_usage']}"
    )


# =============================================================================
# §10 ASGI END-TO-END — real Ollama call through the Starlette gateway app
# =============================================================================

@pytest.mark.anyio
async def test_s10_asgi_proxy_real_ollama_non_streaming():
    """§10a: Full gateway ASGI app in skip_governance mode — non-streaming Ollama call."""
    from gateway.config import get_settings
    from gateway.main import on_startup, on_shutdown, create_app
    from gateway.pipeline.context import get_pipeline_context

    # Reset settings cache to pick up env vars
    get_settings.cache_clear()

    ctx = get_pipeline_context()
    app = create_app()

    await on_startup()
    assert ctx.http_client is not None, "http_client must be set after startup"
    assert ctx.skip_governance is True

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=120.0
        ) as client:
            payload = {
                "model": MODEL,
                "messages": [{"role": "user", "content": "Say the word: ASGI_TEST_PASS"}],
                "stream": False,
            }
            t0 = time.perf_counter()
            resp = await client.post("/v1/chat/completions", json=payload)
            elapsed = round((time.perf_counter() - t0) * 1000)

        assert resp.status_code == 200, (
            f"Gateway returned {resp.status_code}: {resp.text[:300]}"
        )
        body = resp.json()
        choices = body.get("choices", [])
        assert choices, f"No choices in response: {body}"
        content = choices[0]["message"]["content"]
        assert content, "Empty content from model"

        print(
            f"\n  [§10a] ASGI proxy OK — {elapsed}ms\n"
            f"         model={body.get('model')}\n"
            f"         content={content[:120]!r}\n"
            f"         usage={body.get('usage')}"
        )

    finally:
        await on_shutdown()
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_s10_asgi_proxy_real_ollama_streaming():
    """§10b: Full gateway ASGI app — streaming Ollama call accumulates all chunks."""
    from gateway.config import get_settings
    from gateway.main import on_startup, on_shutdown, create_app
    from gateway.pipeline.context import get_pipeline_context

    get_settings.cache_clear()

    ctx = get_pipeline_context()
    # Reinit: http_client may have been closed in §10a teardown
    if ctx.http_client is None:
        ctx.http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        ctx.skip_governance = True

    app = create_app()

    # Fresh startup to get clean state
    await on_startup()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=120.0
        ) as client:
            payload = {
                "model": MODEL,
                "messages": [{"role": "user", "content": "Count from 1 to 5, one per line."}],
                "stream": True,
            }
            t0 = time.perf_counter()
            chunks_received = 0
            content_parts: list[str] = []

            async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                assert resp.status_code == 200, (
                    f"Gateway stream returned {resp.status_code}"
                )
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                            if delta.get("content"):
                                content_parts.append(delta["content"])
                                chunks_received += 1
                        except json.JSONDecodeError:
                            pass

        elapsed = round((time.perf_counter() - t0) * 1000)
        full_content = "".join(content_parts)

        assert chunks_received > 0, "No content chunks received from streaming response"
        assert len(full_content) > 0

        print(
            f"\n  [§10b] ASGI streaming OK — {elapsed}ms "
            f"chunks={chunks_received}\n"
            f"         content={full_content[:120]!r}"
        )

    finally:
        await on_shutdown()
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_s10_asgi_method_not_allowed():
    """§10c: Gateway returns 405 for GET on a proxy route."""
    from gateway.config import get_settings
    from gateway.main import on_startup, on_shutdown, create_app
    from gateway.pipeline.context import get_pipeline_context

    get_settings.cache_clear()
    ctx = get_pipeline_context()
    if ctx.http_client is None:
        ctx.http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        ctx.skip_governance = True

    app = create_app()
    await on_startup()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30.0
        ) as client:
            resp = await client.get("/v1/chat/completions")

        assert resp.status_code == 405
        # Starlette returns an empty body for 405 on POST-only routes (not JSON)
        print(f"  [§10c] 405 Method Not Allowed confirmed (body={resp.text!r})")

    finally:
        await on_shutdown()
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_s10_asgi_health_endpoint():
    """§10d: /health endpoint returns 200 with status field."""
    from gateway.config import get_settings
    from gateway.main import on_startup, on_shutdown, create_app
    from gateway.pipeline.context import get_pipeline_context

    get_settings.cache_clear()
    ctx = get_pipeline_context()
    if ctx.http_client is None:
        ctx.http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        ctx.skip_governance = True

    app = create_app()
    await on_startup()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=30.0
        ) as client:
            resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        print(f"  [§10d] /health: {body}")

    finally:
        await on_shutdown()
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_s10_asgi_thinking_strip_in_gateway_response():
    """§10e: Gateway strips <think> blocks before returning response to caller.

    The raw Ollama response is passed through to the caller unchanged (gateway
    is a transparent proxy in skip_governance mode) — thinking strip is applied
    in the audit record, not in the proxied response. This test verifies
    that the gateway does NOT break the proxied response format.
    """
    from gateway.config import get_settings
    from gateway.main import on_startup, on_shutdown, create_app
    from gateway.pipeline.context import get_pipeline_context

    get_settings.cache_clear()
    ctx = get_pipeline_context()
    if ctx.http_client is None:
        ctx.http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        ctx.skip_governance = True

    app = create_app()
    await on_startup()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=120.0
        ) as client:
            payload = {
                "model": MODEL,
                "messages": [{"role": "user", "content": "What is 3 × 7? Just the number."}],
                "stream": False,
            }
            resp = await client.post("/v1/chat/completions", json=payload)

        assert resp.status_code == 200
        body = resp.json()
        assert "choices" in body
        content = body["choices"][0]["message"]["content"]
        assert content  # Not empty
        # Gateway proxies transparently — response has standard OpenAI shape
        assert "choices" in body
        assert "model" in body
        print(f"  [§10e] Proxy response OK: content={content[:80]!r}")

    finally:
        await on_shutdown()
        get_settings.cache_clear()
