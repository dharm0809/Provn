# Multi-Strategy Tool-Aware Gateway — Implementation Plan

> **Status:** Historical planning artifact. The session chain shipped as an ID-pointer chain (record_id + previous_record_id), not a SHA3 Merkle chain. Treat any Merkle-chain phrasing here as original intent.

**Goal:** Capture the full model interaction — including tool calls, web searches, code execution, and MCP calls — in the audit trail, with zero client code changes beyond `base_url`.

**Principle:** One gateway binary, two strategies selected by provider type. Cloud providers expose tool calls in their response payloads (parse them). Local/private models have no built-in tools (the gateway runs the tool loop itself).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      Walacor Gateway                         │
│                                                              │
│  Request comes in (same as today)                            │
│       │                                                      │
│  Steps 1-2.6 unchanged (attestation, policy, budget, WAL)    │
│       │                                                      │
│  Step 3: Forward to provider                                 │
│       │                                                      │
│  Step 3.5 (NEW): Tool Strategy Router                        │
│       │                                                      │
│       ├── Cloud provider (OpenAI, Anthropic)?                │
│       │     PassiveStrategy:                                 │
│       │       Parse tool_calls/tool_use from response        │
│       │       Extract queries, sources, code, outputs        │
│       │       Attach to audit record                         │
│       │                                                      │
│       └── Local provider (Ollama, private, generic)?         │
│             ActiveStrategy:                                  │
│               While response contains tool_calls:            │
│                 Validate tool against policy                  │
│                 Execute via MCP server                        │
│                 Run content analyzer on tool output           │
│                 Log tool input/output                         │
│                 Send results back to LLM                      │
│               Attach full tool chain to audit record          │
│       │                                                      │
│  Steps 4-8 unchanged (content gate, hash, chain, write)      │
└──────────────────────────────────────────────────────────────┘
```

---

## What exists today (no changes needed)

| Component | File | Status |
|---|---|---|
| Adapter pattern (5 providers) | `src/gateway/adapters/` | Reuse as-is |
| 8-step pipeline orchestrator | `src/gateway/pipeline/orchestrator.py` | Extend at step 3.5 |
| Pipeline context singleton | `src/gateway/pipeline/context.py` | Add new fields |
| Forwarder (single-shot + streaming) | `src/gateway/pipeline/forwarder.py` | Reuse for re-forwards |
| SHA3-512 hasher + record builder | `src/gateway/pipeline/hasher.py` | Extend metadata |
| Session chain (G5 Merkle) | `src/gateway/pipeline/session_chain.py` | No change — one chain link per complete interaction |
| Content analyzers (G4 plugin) | `src/gateway/content/base.py` | Reuse for tool output analysis |
| WAL writer + Walacor client | `src/gateway/wal/`, `src/gateway/walacor/` | No change |
| Config (pydantic-settings) | `src/gateway/config.py` | Add new fields |
| ExecutionRecord model | `walacor-core` package | Extend metadata dict |

---

## What needs to be built

### Phase 14A: Passive Strategy (cloud providers) — parse tool calls from response

**Zero client impact. Zero provider restriction. Just smarter response parsing.**

Cloud providers (OpenAI, Anthropic) already return tool call details in the response payload. The gateway currently ignores them.

#### 1. Extend `ModelResponse` dataclass

**File:** `src/gateway/adapters/base.py`

```python
@dataclass(frozen=True)
class ToolInteraction:
    """One tool call + result as reported by the provider."""
    tool_id: str                    # provider-assigned ID (e.g. "call_abc123")
    tool_type: str                  # "function" | "web_search" | "code_interpreter" | "file_search"
    tool_name: str | None           # function name, null for built-in tools
    input_data: dict | str | None   # arguments / search query / code
    output_data: dict | str | None  # result (null for passive — provider doesn't always return it)
    sources: list[dict] | None      # URLs for web_search
    metadata: dict | None           # extra (e.g. code_interpreter stdout)

@dataclass(frozen=True)
class ModelResponse:
    content: str
    usage: dict[str, Any] | None
    raw_body: bytes
    provider_request_id: str | None = None
    model_hash: str | None = None
    tool_interactions: list[ToolInteraction] | None = None  # NEW
    has_pending_tool_calls: bool = False                     # NEW — for active strategy
```

#### 2. Update OpenAI adapter response parsing

**File:** `src/gateway/adapters/openai.py`

Currently `parse_response` reads `choices[0].message.content` and ignores `choices[0].message.tool_calls`. Change:

- Check `choices[0].message.tool_calls` — if present, extract each tool call into `ToolInteraction`
- Check `output[]` items for Responses API format (`web_search_call`, `code_interpreter_call`) — extract queries, sources, code, outputs
- Set `has_pending_tool_calls = True` if `finish_reason == "tool_calls"` and content is empty (model wants client to execute tools)
- Same changes for `parse_streamed_response` — detect `finish_reason == "tool_calls"` in final SSE chunk

**OpenAI Chat Completions format (tool_calls in response):**
```json
{
  "choices": [{
    "message": {
      "content": null,
      "tool_calls": [
        {"id": "call_abc", "type": "function", "function": {"name": "search", "arguments": "{...}"}}
      ]
    },
    "finish_reason": "tool_calls"
  }]
}
```

**OpenAI Responses API format (web_search_call in output):**
```json
{
  "output": [
    {"type": "web_search_call", "id": "ws_abc", "action": {"type": "search", "queries": [...], "sources": [...]}},
    {"type": "message", "content": [{"type": "text", "text": "Based on..."}]}
  ]
}
```

Both formats → same `ToolInteraction` list.

#### 3. Update Anthropic adapter response parsing

**File:** `src/gateway/adapters/anthropic.py`

Currently iterates `content[]` blocks and only processes `type == "text"`. Change:

- Also capture `type == "tool_use"` blocks → extract `id`, `name`, `input` into `ToolInteraction`
- Also capture `type == "server_tool_use"` blocks (for Anthropic's server-side tools)
- Set `has_pending_tool_calls = True` if `stop_reason == "tool_use"`

**Anthropic format:**
```json
{
  "content": [
    {"type": "text", "text": "Let me search..."},
    {"type": "tool_use", "id": "tu_abc", "name": "web_search", "input": {"query": "..."}}
  ],
  "stop_reason": "tool_use"
}
```

#### 4. Attach tool interactions to audit record

**File:** `src/gateway/pipeline/orchestrator.py`

After step 3 (forward), if `model_response.tool_interactions` is not empty:

```python
audit_metadata["tool_interactions"] = [
    {
        "tool_id": t.tool_id,
        "tool_type": t.tool_type,
        "tool_name": t.tool_name,
        "input_hash": compute_sha3_512_string(json.dumps(t.input_data)) if t.input_data else None,
        "output_hash": compute_sha3_512_string(json.dumps(t.output_data)) if t.output_data else None,
        "sources": t.sources,
        "source": "provider",   # passive — parsed from provider response
    }
    for t in model_response.tool_interactions
]
audit_metadata["tool_interaction_count"] = len(model_response.tool_interactions)
audit_metadata["tool_strategy"] = "passive"
```

This goes into the existing `metadata` dict of `ExecutionRecord`. No schema change needed in `walacor-core`.

#### 5. New Prometheus metrics

**File:** `src/gateway/metrics/prometheus.py`

```
walacor_gateway_tool_calls_total        Counter  {provider, tool_type, source}
walacor_gateway_tool_loop_iterations    Histogram {provider}
```

---

### Phase 14B: Active Strategy (local/private models) — gateway runs the tool loop

**For Ollama, LM Studio, vLLM, private models that have no built-in tools.**

#### 6. MCP client component

**New file:** `src/gateway/mcp/client.py`

A lightweight MCP client that can connect to MCP servers via:
- **HTTP/SSE** (remote MCP servers)
- **stdio** (local MCP servers spawned as subprocesses)

```python
class MCPClient:
    """Connects to one MCP server, executes tool calls."""

    async def connect(self, server_config: MCPServerConfig) -> None
    async def list_tools(self) -> list[ToolDefinition]
    async def call_tool(self, tool_name: str, arguments: dict) -> ToolResult
    async def close(self) -> None

class MCPServerConfig:
    name: str               # "web-search", "postgres", etc.
    transport: str           # "http" | "stdio"
    url: str | None          # for HTTP transport
    command: str | None      # for stdio transport (e.g. "npx @modelcontextprotocol/server-postgres")
    args: list[str] | None   # for stdio transport
    env: dict | None         # environment variables for stdio
```

Dependencies: Use the official `mcp` Python SDK (`pip install mcp`) which handles protocol details. The gateway wraps it with timeout enforcement and audit hooks.

#### 7. Tool registry

**New file:** `src/gateway/mcp/registry.py`

```python
class ToolRegistry:
    """Maps tool names to MCP server configs. Provides tool definitions for LLM."""

    def __init__(self, servers: list[MCPServerConfig])
    async def startup(self) -> None         # connect to all servers, list tools
    def get_tool_definitions(self) -> list[dict]  # OpenAI function-calling format
    def resolve_tool(self, tool_name: str) -> MCPClient | None
    async def execute_tool(self, tool_name: str, arguments: dict) -> ToolResult
    async def shutdown(self) -> None
```

At startup, connects to all configured MCP servers, calls `list_tools()` on each, and builds a unified tool catalog. This catalog is injected into LLM requests for local models.

#### 8. Tool loop in orchestrator

**File:** `src/gateway/pipeline/orchestrator.py`

Between step 3 (forward) and step 4 (post-inference policy), add step 3.5:

```python
# Step 3.5: Tool call loop (active strategy only)
tool_interactions = []
iteration = 0
max_iterations = settings.tool_max_iterations  # default 10, configurable

while (
    model_response.has_pending_tool_calls
    and ctx.tool_registry
    and iteration < max_iterations
):
    iteration += 1

    # Extract tool calls from response
    pending_calls = model_response.tool_interactions
    tool_results = []

    for tc in pending_calls:
        # Optional: per-tool policy check
        # Optional: content analysis on tool input (injection detection)

        result = await ctx.tool_registry.execute_tool(tc.tool_name, tc.input_data)

        # Optional: content analysis on tool output (PII, exfiltration)

        tool_interactions.append(ToolInteraction(
            tool_id=tc.tool_id,
            tool_type=tc.tool_type,
            tool_name=tc.tool_name,
            input_data=tc.input_data,
            output_data=result.content,
            sources=None,
            metadata={"iteration": iteration, "duration_ms": result.duration_ms},
        ))
        tool_results.append({"tool_call_id": tc.tool_id, "content": result.content})

    # Build new request with tool results appended to messages
    call = adapter.build_tool_result_call(call, pending_calls, tool_results)

    # Re-forward to LLM
    response, model_response = await forward(adapter, call, request)

# Attach all tool interactions to audit metadata (same format as passive)
if tool_interactions:
    audit_metadata["tool_interactions"] = [serialize(t) for t in tool_interactions]
    audit_metadata["tool_interaction_count"] = len(tool_interactions)
    audit_metadata["tool_loop_iterations"] = iteration
    audit_metadata["tool_strategy"] = "active"
```

#### 9. Adapter extension for tool result injection

**File:** `src/gateway/adapters/base.py`

Add optional method to `ProviderAdapter`:

```python
class ProviderAdapter(ABC):
    # ... existing methods ...

    def build_tool_result_call(
        self,
        original_call: ModelCall,
        tool_calls: list[ToolInteraction],
        tool_results: list[dict],
    ) -> ModelCall:
        """Build a new ModelCall with tool results appended to messages.
        Default: raise NotImplementedError (passive-only adapters).
        """
        raise NotImplementedError("This adapter does not support active tool execution")
```

Implement in `OllamaAdapter` and `OpenAIAdapter` (for local OpenAI-compat servers):

```python
def build_tool_result_call(self, original_call, tool_calls, tool_results):
    body = json.loads(original_call.raw_body)
    # Append assistant message with tool_calls
    body["messages"].append({
        "role": "assistant",
        "tool_calls": [
            {"id": tc.tool_id, "type": "function", "function": {"name": tc.tool_name, "arguments": json.dumps(tc.input_data)}}
            for tc in tool_calls
        ]
    })
    # Append tool result messages
    for result in tool_results:
        body["messages"].append({
            "role": "tool",
            "tool_call_id": result["tool_call_id"],
            "content": str(result["content"]),
        })
    new_body = json.dumps(body).encode()
    return dataclasses.replace(
        original_call,
        raw_body=new_body,
        prompt_text=original_call.prompt_text,  # keep original for hashing
    )
```

#### 10. Tool definitions injection

When the active strategy is enabled and MCP servers are configured, the gateway injects tool definitions into LLM requests for local models:

**File:** `src/gateway/adapters/ollama.py` (and generic.py)

In `build_forward_request`, if tool registry has tools:
```python
body = json.loads(call.raw_body)
if not body.get("tools") and ctx.tool_registry:
    body["tools"] = ctx.tool_registry.get_tool_definitions()
```

This means the client doesn't need to know about tools at all. The gateway adds them transparently.

---

### Phase 14C: Configuration and startup

#### 11. New config fields

**File:** `src/gateway/config.py`

```python
# Tool-aware gateway (Phase 14)
tool_aware_enabled: bool = Field(default=False, description="Enable tool-call awareness and auditing")
tool_strategy: str = Field(default="auto", description="'auto' (detect from provider), 'passive', 'active', 'disabled'")
tool_max_iterations: int = Field(default=10, description="Max tool-call loop iterations (active strategy)")
tool_execution_timeout_ms: int = Field(default=30000, description="Per-tool execution timeout in ms")
tool_content_analysis_enabled: bool = Field(default=True, description="Run content analyzers on tool inputs/outputs")

# MCP server configuration (active strategy)
mcp_servers_json: str = Field(default="", description="JSON array of MCP server configs, or path to JSON file")
```

#### 12. Configuration format for MCP servers

```bash
# Environment variable
WALACOR_MCP_SERVERS_JSON='[
  {"name": "web-search", "transport": "http", "url": "http://localhost:3001/mcp"},
  {"name": "postgres", "transport": "stdio", "command": "npx", "args": ["@modelcontextprotocol/server-postgres", "postgresql://..."]},
  {"name": "filesystem", "transport": "stdio", "command": "npx", "args": ["@modelcontextprotocol/server-filesystem", "/data"]}
]'

# Or point to a file
WALACOR_MCP_SERVERS_JSON=/etc/walacor/mcp-servers.json
```

#### 13. Startup initialization

**File:** `src/gateway/main.py`

```python
async def _init_tool_registry(settings, ctx) -> None:
    """Phase 14: MCP tool registry and clients."""
    if not settings.tool_aware_enabled:
        return

    from gateway.mcp.registry import ToolRegistry
    from gateway.mcp.client import MCPServerConfig

    servers = _parse_mcp_servers(settings.mcp_servers_json)
    if servers:
        ctx.tool_registry = ToolRegistry(servers)
        await ctx.tool_registry.startup()
        logger.info("Tool registry ready: %d servers, %d tools",
                     len(servers), len(ctx.tool_registry.get_tool_definitions()))
```

---

### Phase 14D: Strategy selection logic

#### 14. Auto-detection

**File:** `src/gateway/pipeline/orchestrator.py`

```python
def _select_tool_strategy(adapter: ProviderAdapter, settings) -> str:
    """Determine tool strategy based on provider and config."""
    if not settings.tool_aware_enabled:
        return "disabled"
    if settings.tool_strategy != "auto":
        return settings.tool_strategy

    # Auto-detect based on provider
    provider = adapter.get_provider_name()
    if provider in ("openai", "anthropic"):
        return "passive"        # cloud providers return tool calls in response
    if provider in ("ollama", "generic", "huggingface"):
        return "active"         # local models need gateway to run tool loop
    return "passive"            # safe default
```

The operator can override: `WALACOR_TOOL_STRATEGY=active` forces active mode even for OpenAI (useful if the operator wants the gateway to control all tool execution instead of relying on OpenAI's built-in tools).

---

## Execution order

| Step | What | Effort | Dependencies |
|---|---|---|---|
| **14A-1** | `ToolInteraction` dataclass + `ModelResponse` extension | Small | None |
| **14A-2** | OpenAI adapter: parse `tool_calls` + Responses API `web_search_call` | Medium | 14A-1 |
| **14A-3** | Anthropic adapter: parse `tool_use` blocks | Medium | 14A-1 |
| **14A-4** | Orchestrator: attach tool interactions to audit metadata | Small | 14A-1 |
| **14A-5** | Prometheus metrics for tool calls | Small | 14A-1 |
| **14A-6** | Tests for passive strategy | Medium | 14A-2, 14A-3, 14A-4 |
| **14B-1** | MCP client (`mcp/client.py`) | Medium-Large | None |
| **14B-2** | Tool registry (`mcp/registry.py`) | Medium | 14B-1 |
| **14B-3** | Tool loop in orchestrator (step 3.5) | Medium | 14A-1, 14B-2 |
| **14B-4** | `build_tool_result_call` on adapters | Medium | 14A-1 |
| **14B-5** | Tool definition injection for local models | Small | 14B-2 |
| **14B-6** | Tests for active strategy | Medium | 14B-3, 14B-4 |
| **14C-1** | Config fields + MCP server config parsing | Small | None |
| **14C-2** | Startup initialization + shutdown | Small | 14B-2, 14C-1 |
| **14D-1** | Strategy auto-detection | Small | 14A-4, 14B-3 |

**Suggested order:** 14A (passive) first — it's lower risk, delivers value immediately for cloud provider users, and validates the data model. Then 14B (active) builds on the same `ToolInteraction` type. 14C and 14D can be done in parallel with 14B.

---

## Audit record example (both strategies produce this)

```json
{
  "execution_id": "abc-123",
  "prompt_text": "What were our Q4 sales?",
  "prompt_hash": "sha3-...",
  "response_content": "Based on the database query, Q4 sales were $4.2M...",
  "response_hash": "sha3-...",
  "provider_request_id": "chatcmpl-xyz",
  "model_attestation_id": "att_456",
  "policy_version": 7,
  "policy_result": "pass",
  "session_id": "sess-789",
  "sequence_number": 3,
  "record_hash": "sha3-...",
  "metadata": {
    "tool_strategy": "active",
    "tool_interaction_count": 2,
    "tool_loop_iterations": 1,
    "tool_interactions": [
      {
        "tool_id": "call_001",
        "tool_type": "function",
        "tool_name": "postgres.query",
        "input_hash": "sha3-...",
        "output_hash": "sha3-...",
        "source": "gateway",
        "iteration": 1,
        "duration_ms": 245
      },
      {
        "tool_id": "call_002",
        "tool_type": "function",
        "tool_name": "web_search",
        "input_hash": "sha3-...",
        "output_hash": "sha3-...",
        "sources": [{"url": "https://..."}],
        "source": "gateway",
        "iteration": 1,
        "duration_ms": 1200
      }
    ],
    "enforcement_mode": "enforced",
    "token_usage": {"prompt_tokens": 850, "completion_tokens": 120, "total_tokens": 970}
  }
}
```

---

## Client experience (all providers)

```python
# OpenAI user — unchanged. Tool calls parsed from response automatically.
client = OpenAI(api_key="sk-...", base_url="http://gateway:8000/v1")

# Ollama user — unchanged. Gateway adds tools + runs tool loop.
client = OpenAI(api_key="sk-...", base_url="http://gateway:8000/v1")

# Private model user — unchanged. Same as Ollama.
client = OpenAI(api_key="sk-...", base_url="http://gateway:8000/v1")
```

**One `base_url` change. Full audit of all tool interactions. No tool restrictions for cloud users. MCP tool access for local model users.**

---

## New dependencies

| Package | Purpose | Required for |
|---|---|---|
| `mcp` | Official MCP Python SDK | Active strategy only |

Passive strategy requires **zero new dependencies**.

---

## What does NOT change

- All five guarantees (G1-G5) remain intact
- Completeness invariant (`GEN_ATTEMPT = GEN + GEN_DENY + GEN_ERROR`) — unchanged
- Session chain — one chain link per complete interaction (tool loop is internal)
- Client-facing API — same routes, same request/response format
- SQLite WAL and Walacor backend — same write path
- Content analyzers — reused on tool outputs (active strategy)
- Policy enforcement — unchanged (extended with per-tool policy in future)

---

## Future extensions (not in this plan)

- **Per-tool attestation** — attest MCP servers like models (G1 extension)
- **Tool-specific policy rules** — block certain tools for certain tenants/users
- **Tool call content analysis** — dedicated analyzer for injection/exfiltration in tool I/O
- **Streaming + tool calls** — detect `finish_reason=tool_calls` mid-stream, pause stream, execute tools, resume
- **Agentic session chains** — multi-agent tracking across orchestrators
- **Tool call budget** — count tool executions toward token/cost budget
