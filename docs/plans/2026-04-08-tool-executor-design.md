# Tool Executor Extraction — Design

**Date:** 2026-04-08  
**Goal:** Extract tool system from orchestrator.py into a dedicated module, simplify config, add streaming final answer.

## Problem

Tool logic is spread across 14 functions (~600 lines) in orchestrator.py (2640 lines), with decisions scattered across 5 different call sites. Three overlapping web search config flags, two parallel capability caches, and the active tool strategy kills streaming entirely.

## Design Decisions (from brainstorming)

1. **Single web search flag**: `WALACOR_WEB_SEARCH_ENABLED` — drop `openai_web_search_enabled` and `gateway_web_search_enabled`
2. **Remove `tool_strategy` config**: Strategy is automatic per-request (active when tools registered + model supports them, passive for provider-reported tool calls)
3. **Kill legacy `_model_capabilities`**: Use only `ctx.capability_registry`
4. **Keep per-key tool filtering**: Multi-tenant tool control stays
5. **Stream final answer**: Tool loop stays non-streaming internally, but the last LLM call (after tools complete) streams to the client

## Architecture

### New module: `src/gateway/pipeline/tool_executor.py`

**Public API (2 functions):**

```python
async def prepare_tools(
    call: ModelCall,
    adapter: ProviderAdapter,
    request: Request,
    ctx, settings,
) -> ToolPrepResult:
    """Decide tool strategy, inject tool definitions into call if needed.
    Returns ToolPrepResult with modified call and strategy."""

async def execute_tools(
    strategy: str,
    call: ModelCall,
    model_response: ModelResponse,
    http_response: Response,
    adapter: ProviderAdapter,
    request: Request,
    ctx, settings,
    provider: str,
) -> ToolResult:
    """Run tool strategy (active loop or passive collection).
    Handles: tool-unsupported retry, capability caching, streaming final answer.
    Returns ToolResult with final call, response, interactions, and optional streaming response."""

# Audit helpers (called from _build_and_write_record and _after_stream_record):
def build_tool_audit_metadata(interactions, strategy, iterations) -> dict
async def write_tool_events(interactions, execution_id, call, strategy, ctx, settings)
```

### What moves to tool_executor.py

| Function | Lines | Notes |
|----------|-------|-------|
| `_select_tool_strategy` | 513-527 | Simplified: no `auto` mode |
| `_strip_tools_from_call` | 530-540 | |
| `_TOOL_UNSUPPORTED_PHRASES` | 543-551 | |
| `_is_tool_unsupported_error` | 554-562 | |
| `_filter_tools_for_key` | 565-590 | Kept for multi-tenant |
| `_inject_tools_into_call` | 593-612 | |
| `_serialize_tool_interaction` | 615-627 | |
| `_build_tool_audit_metadata` | 630-644 | → `build_tool_audit_metadata` (public) |
| `_build_tool_event_record` | 647-687 | |
| `_write_tool_events` | 690-728 | → `write_tool_events` (public) |
| `_emit_tool_metrics` | 730-735 | |
| `_execute_one_tool` | 738-847 | |
| `_run_active_tool_loop` | 850-905 | Modified: last call streams |
| `_route_tool_strategy` | 1578-1612 | Absorbed into `execute_tools` |

### What stays in orchestrator.py

- `_PreCheckResult`, `_AuditParams`, `_ToolStrategyResult` dataclasses (renamed/simplified)
- `handle_request` flow — calls `prepare_tools()` and `execute_tools()`
- `_build_and_write_record` — calls `build_tool_audit_metadata()` and `write_tool_events()`

### Streaming final answer

In `_run_active_tool_loop`, when the loop finishes and the original request was streaming:

```python
# Last iteration: stream the final answer
if original_streaming and not current_model.has_pending_tool_calls:
    # Convert back to streaming for the final call
    final_call = _restore_streaming(current_call)
    # Return streaming response + model_response=None (parsed in background)
```

The orchestrator receives a streaming `Response` from `execute_tools()` and returns it directly (with the after-stream background task attached, same as the normal streaming path).

### Config changes

**Remove:**
- `openai_web_search_enabled`
- `gateway_web_search_enabled`  
- `tool_strategy`

**Keep:**
- `tool_aware_enabled` (master switch)
- `web_search_enabled` (single flag)
- `web_search_provider`, `web_search_api_key`, `web_search_max_results`
- `tool_max_iterations`, `tool_loop_total_timeout_ms`, `tool_execution_timeout_ms`
- `tool_max_output_bytes`, `tool_content_analysis_enabled`
- `mcp_servers_json`, `mcp_allowed_commands`

### Legacy capability cache removal

Delete from orchestrator.py:
- `_model_capabilities: LRUCache`
- `_model_supports_tools()`
- `_record_model_capability()`

Update health.py and control/api.py to use `ctx.capability_registry` instead.

### Test impact

3 test files need import path updates:
- `tests/unit/test_tool_access_control.py` — `_filter_tools_for_key`
- `tests/integration/test_live_llama.py` — `_inject_tools_into_call`, `_run_active_tool_loop`
- `tests/unit/test_redis_trackers.py` — `_write_tool_events` mock path
