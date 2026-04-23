"""Tool executor: unified tool awareness, injection, execution, and audit.

Extracted from orchestrator.py to provide a clean, single-responsibility module
for all tool-related logic. The orchestrator calls two entry points:

  prepare_tools()  — decide strategy, inject tool definitions into the call
  execute_tools()  — run active tool loop or collect passive interactions

Plus two audit helpers used by the record-write path:

  build_tool_audit_metadata()  — build tool_* keys for execution record metadata
  write_tool_events()          — write per-tool event records to storage
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from gateway.adapters.base import ModelCall, ModelResponse, ProviderAdapter, ToolInteraction
from gateway.mcp.client import ToolResult as MCPToolResult
from gateway.metrics.prometheus import tool_calls_total, tool_loop_iterations
from gateway.pipeline.forwarder import forward, stream_with_tee
from gateway.pipeline.response_evaluator import analyze_text
from gateway.util.time import iso8601_utc as _iso8601
import gateway.util.json_utils as json

logger = logging.getLogger(__name__)


# ── Swallowed-exception instrumentation (for /v1/connections) ───────────────

_tool_exception_log: deque = deque(maxlen=50)  # (ts, tool, error)


def record_tool_exception(*, tool: str, error: str) -> None:
    cleaned = (error or "").strip() or "Exception"
    _tool_exception_log.append((time.time(), tool, cleaned))


def tool_exceptions_snapshot() -> dict:
    now = time.time()
    recent = [e for e in _tool_exception_log if now - e[0] <= 60.0]
    last = recent[-1] if recent else None
    return {
        "exceptions_60s": len(recent),
        "last_exception": (
            {"ts": _iso8601(last[0]), "tool": last[1], "error": last[2]}
            if last else None
        ),
    }


# ── Result types ────────────────────────────────────────────────────────────

@dataclasses.dataclass
class ToolPrepResult:
    """Outcome of prepare_tools(): modified call + resolved strategy."""
    call: ModelCall
    strategy: str  # "active", "passive", or "disabled"


@dataclasses.dataclass
class ToolExecResult:
    """Outcome of execute_tools(): final state after tool strategy runs."""
    call: ModelCall
    model_response: ModelResponse
    interactions: list[ToolInteraction]
    iterations: int
    error: Response | None = None
    http_response: Response | None = None  # final HTTP response (replaces original for active loop)
    # Streaming support: when the final tool-loop answer is streamed,
    # streaming_response holds the StreamingResponse to return to the client,
    # and stream_buffer + stream_background hold the tee buffer / background task
    # for the orchestrator to wire up after-stream record writing.
    streaming_response: Response | None = None
    stream_buffer: list[bytes] | None = None
    # Phase 24.4: when set, the streaming_response was synthesized from an
    # already-parsed non-streaming forward (no second upstream round trip).
    # The audit background task should use `model_response` directly instead
    # of trying to parse `stream_buffer`.
    synthetic_stream: bool = False


# ── Tool-unsupported detection ──────────────────────────────────────────────

_TOOL_UNSUPPORTED_PHRASES = (
    "does not support tools",
    "tool use is not supported",
    "tools are not supported",
    "tool_use is not supported",
    "does not support function",
    "function calling is not supported",
    "does not support tool_use",
)


def is_tool_unsupported_error(status_code: int, body: bytes | memoryview | None) -> bool:
    """Check if a provider error indicates the model doesn't support tools."""
    if status_code not in (400, 422) or body is None:
        return False
    try:
        text = bytes(body).decode("utf-8", errors="replace").lower()
        return any(phrase in text for phrase in _TOOL_UNSUPPORTED_PHRASES)
    except Exception:
        return False


# ── Call mutation helpers ───────────────────────────────────────────────────

def strip_tools_from_call(call: ModelCall) -> ModelCall:
    """Remove tool definitions from request body — used when model rejects tools."""
    try:
        body = json.loads(call.raw_body)
        body.pop("tools", None)
        body.pop("tool_choice", None)
        new_body = json.dumps_bytes(body)
        return dataclasses.replace(call, raw_body=new_body)
    except Exception as exc:
        record_tool_exception(tool="unknown", error=str(exc) or type(exc).__name__)
        logger.warning(
            "strip_tools_from_call: failed for model=%s — sending original",
            call.model_id, exc_info=True,
        )
        return call


def _inject_tools_into_call(call: ModelCall, tool_definitions: list[dict]) -> ModelCall:
    """Transparently add tool definitions to request body (active strategy)."""
    if not tool_definitions:
        return call
    try:
        body = json.loads(call.raw_body)
        if not body.get("tools"):
            body["tools"] = tool_definitions
            new_body = json.dumps_bytes(body)
            return ModelCall(
                provider=call.provider, model_id=call.model_id,
                prompt_text=call.prompt_text, raw_body=new_body,
                is_streaming=call.is_streaming, metadata=call.metadata,
            )
    except Exception as exc:
        record_tool_exception(tool="unknown", error=str(exc) or type(exc).__name__)
        logger.warning(
            "Failed to inject tool definitions: model=%s", call.model_id,
            exc_info=True,
        )
    return call


def _force_non_streaming(call: ModelCall) -> ModelCall:
    """Override stream=false for the active tool loop (need full response to parse tool_calls)."""
    try:
        body = json.loads(call.raw_body)
        body["stream"] = False
        return dataclasses.replace(call, is_streaming=False, raw_body=json.dumps_bytes(body))
    except Exception as exc:
        record_tool_exception(tool="unknown", error=str(exc) or type(exc).__name__)
        logger.warning("Failed to override stream=false for tool loop", exc_info=True)
        return call


def _restore_streaming(call: ModelCall) -> ModelCall:
    """Restore stream=true for the final answer after tools complete."""
    try:
        body = json.loads(call.raw_body)
        body["stream"] = True
        return dataclasses.replace(call, is_streaming=True, raw_body=json.dumps_bytes(body))
    except Exception as exc:
        record_tool_exception(tool="unknown", error=str(exc) or type(exc).__name__)
        logger.warning("Failed to restore streaming for final answer", exc_info=True)
        return call


# ── Per-key tool filtering ──────────────────────────────────────────────────

def filter_tools_for_key(
    tool_definitions: list[dict],
    api_key: str | None,
    ctx,
) -> list[dict]:
    """Filter tool definitions based on per-key allow-list from the control plane."""
    if not api_key or ctx.control_store is None:
        return tool_definitions
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    allowed = ctx.control_store.get_allowed_tools(key_hash)
    if allowed is None:
        return tool_definitions  # no restrictions
    if not allowed:
        return []  # explicitly blocked
    return [
        t for t in tool_definitions
        if (t.get("function", {}).get("name") in allowed or t.get("name") in allowed)
    ]


# ── Strategy selection ──────────────────────────────────────────────────────

def _select_strategy(call: ModelCall, ctx, settings) -> str:
    """Determine tool strategy for this request. Fully automatic — no config knob.

    Returns:
      "active"   — tools will be injected and the gateway runs the tool loop
      "passive"  — tool_aware enabled but no active tools for this request
      "disabled" — tool awareness off

    Rules (Phase 24.5):
      1. tool_aware_enabled=false                         → "disabled"
      2. provider is anthropic                            → "passive"
         (Anthropic runs web_search + other server tools entirely server-side
         during the same streaming forward. We auto-inject the native server
         tool in AnthropicAdapter.parse_request and let the stream flow
         through with zero extra round trips. Our adapter's parse_response /
         parse_streamed_response still capture the tool_use + result blocks
         for audit.)
      3. `_gateway_web_search` flag was explicitly set    → "active"
      4. External MCP servers configured                  → "active" on every request
      5. Built-in web search enabled AND the registry
         has any tool                                     → "active" on every request
         (the model decides when to call it; applies to OpenAI / Ollama /
          HuggingFace / generic adapters that don't have native server tools)
      6. otherwise                                        → "passive"

    Models that don't support tools (detected via the capability_registry) are
    handled upstream in prepare_tools(): "active" is downgraded to "none" before
    any injection happens.
    """
    if not settings.tool_aware_enabled:
        return "disabled"

    if call.provider == "anthropic":
        return "passive"

    if call.metadata.get("_gateway_web_search"):
        return "active"

    if settings.mcp_servers_json and ctx.tool_registry and ctx.tool_registry.get_tool_count() > 0:
        return "active"

    if (
        settings.web_search_enabled
        and ctx.tool_registry
        and ctx.tool_registry.get_tool_count() > 0
    ):
        return "active"

    return "passive"


# ── prepare_tools (entry point 1) ──────────────────────────────────────────

async def prepare_tools(
    call: ModelCall,
    request: Request,
    ctx,
    settings,
) -> ToolPrepResult:
    """Decide tool strategy and inject tool definitions into the call if active.

    Called during pre-checks. Returns the (possibly modified) call and strategy.
    """
    strategy = _select_strategy(call, ctx, settings)

    if strategy != "active" or not ctx.tool_registry:
        return ToolPrepResult(call=call, strategy=strategy)

    # Check model capability — skip injection for models known to not support tools
    if ctx.capability_registry:
        supports = ctx.capability_registry.supports_tools(call.model_id)
        if supports is False:
            logger.debug("Skipping tool injection for %s — known no tool support", call.model_id)
            return ToolPrepResult(call=call, strategy="none")

    # Get and filter tool definitions
    tool_defs = ctx.tool_registry.get_tool_definitions()
    from gateway.auth.api_key import get_api_key_from_request
    api_key = get_api_key_from_request(request)
    tool_defs = filter_tools_for_key(tool_defs, api_key, ctx)

    if not tool_defs:
        return ToolPrepResult(call=call, strategy="passive")

    # Inject tools into the call
    call = _inject_tools_into_call(call, tool_defs)

    return ToolPrepResult(call=call, strategy="active")


# ── Single tool execution ───────────────────────────────────────────────────

async def _execute_one_tool(
    tc: ToolInteraction, ctx, settings, provider: str, iteration: int,
) -> tuple[ToolInteraction, dict]:
    """Execute one tool call. Returns (enriched_interaction, result_dict)."""
    # Validate arguments against tool schema
    if tc.tool_name and ctx.tool_registry:
        schema = ctx.tool_registry.get_tool_schema(tc.tool_name)
        if schema:
            required = schema.get("required", [])
            args = tc.input_data if isinstance(tc.input_data, dict) else {}
            missing = [f for f in required if f not in args]
            if missing:
                logger.warning("Tool arg validation failed: tool=%s missing=%s", tc.tool_name, missing)
                enriched = ToolInteraction(
                    tool_id=tc.tool_id, tool_type=tc.tool_type, tool_name=tc.tool_name,
                    input_data=tc.input_data, output_data=None, sources=None,
                    metadata={"iteration": iteration, "duration_ms": 0.0, "is_error": True,
                              "validation_error": f"missing required args: {missing}"},
                )
                return enriched, {"tool_call_id": tc.tool_id,
                                  "content": f"Tool call rejected: missing required arguments {missing}"}

    t_start = time.perf_counter()
    result = await ctx.tool_registry.execute_tool(
        tc.tool_name or "",
        tc.input_data if isinstance(tc.input_data, dict) else {},
        timeout_ms=settings.tool_execution_timeout_ms,
    )
    duration_ms = round((time.perf_counter() - t_start) * 1000.0, 2)

    # Truncate oversized output
    if result.content and len(result.content) > settings.tool_max_output_bytes:
        logger.warning(
            "Tool %s output truncated: %d > %d bytes",
            tc.tool_name, len(result.content), settings.tool_max_output_bytes,
        )
        result = MCPToolResult(
            content=result.content[:settings.tool_max_output_bytes] + "\n[TRUNCATED]",
            is_error=result.is_error,
            duration_ms=getattr(result, "duration_ms", None),
            sources=getattr(result, "sources", None),
        )

    # Heuristic prompt injection detection
    _injection_detected = False
    if not result.is_error and result.content:
        _injection_patterns = [
            "ignore previous instructions", "ignore all previous",
            "disregard your instructions", "you are now",
            "new instructions:", "system prompt:", "override:", "<system>",
        ]
        content_lower = (result.content if isinstance(result.content, str) else str(result.content)).lower()
        for pattern in _injection_patterns:
            if pattern in content_lower:
                logger.warning("Potential prompt injection in tool output: tool=%s pattern='%s'", tc.tool_name, pattern)
                _injection_detected = True
                break

    try:
        tool_calls_total.labels(provider=provider, tool_type=tc.tool_type, source="gateway").inc()
    except Exception:
        logger.debug("Metric increment failed (tool_calls_total)", exc_info=True)

    # Content analysis BEFORE feeding back to LLM
    output_content = result.content
    is_error = result.is_error
    _is_web_search = tc.tool_name in ("web_search",) or tc.tool_type == "web_search"
    if (settings.tool_content_analysis_enabled
            and ctx.content_analyzers and output_content and not is_error):
        analysis = await analyze_text(output_content, ctx.content_analyzers)
        blocking = [d for d in analysis if d.get("verdict") == "block"]
        if blocking:
            top = blocking[0]
            if _is_web_search and top.get("category") == "pii":
                logger.info("Tool PII downgraded to warn (web search): tool=%s", tc.tool_name)
            else:
                logger.warning("Tool output blocked: tool=%s category=%s", tc.tool_name, top["category"])
                output_content = f"[Tool output blocked by content policy: {top['category']}]"
                is_error = True

    _meta: dict = {"iteration": iteration, "duration_ms": duration_ms, "is_error": is_error}
    if _injection_detected:
        _meta["injection_warning"] = True
    enriched = ToolInteraction(
        tool_id=tc.tool_id, tool_type=tc.tool_type, tool_name=tc.tool_name,
        input_data=tc.input_data, output_data=output_content, sources=result.sources,
        metadata=_meta,
    )
    return enriched, {"tool_call_id": tc.tool_id, "content": output_content}


# ── Active tool loop ────────────────────────────────────────────────────────

async def _run_active_tool_loop(
    adapter: ProviderAdapter,
    call: ModelCall,
    request: Request,
    model_response: ModelResponse,
    ctx, settings, provider: str,
    original_streaming: bool,
) -> ToolExecResult:
    """Gateway-side tool-call loop for local/private models.

    Runs non-streaming internally. If original_streaming is True, the FINAL
    LLM call (after all tools complete) is forwarded as streaming so the user
    gets progressive output.
    """
    all_interactions: list[ToolInteraction] = []
    iterations = 0
    current_call = call
    current_model = model_response
    final_http_resp: Response | None = None
    loop_deadline = time.perf_counter() + (settings.tool_loop_total_timeout_ms / 1000.0)

    while (
        current_model.has_pending_tool_calls
        and current_model.tool_interactions
        and iterations < settings.tool_max_iterations
        and time.perf_counter() < loop_deadline
    ):
        iterations += 1
        pending = current_model.tool_interactions
        tool_results: list[dict] = []

        for tc in pending:
            enriched, result_dict = await _execute_one_tool(tc, ctx, settings, provider, iterations)
            all_interactions.append(enriched)
            tool_results.append(result_dict)

        try:
            current_call = adapter.build_tool_result_call(current_call, pending, tool_results)
        except NotImplementedError:
            logger.warning("Adapter %s: no build_tool_result_call — stopping loop", adapter.get_provider_name())
            break

        # Peek ahead: if the model isn't likely to call more tools and the
        # original request was streaming, stream the final answer.
        # Heuristic: after max_iterations-1, or when only 1 iteration of tool calls happened
        # and we're about to get the final answer, stream it.
        # For safety, we only stream the very last forward call (when no more pending).
        # We can't know in advance, so we always forward non-streaming and only
        # convert to streaming on the *final* iteration when the model stops calling tools.
        http_resp, current_model = await forward(adapter, current_call, request)
        final_http_resp = http_resp
        if http_resp.status_code >= 500:
            return ToolExecResult(
                call=current_call, model_response=current_model,
                interactions=all_interactions, iterations=iterations,
                error=http_resp,
            )

    if time.perf_counter() >= loop_deadline:
        logger.warning("Tool loop timeout reached (%.0fms)", settings.tool_loop_total_timeout_ms)

    if iterations > 0:
        try:
            tool_loop_iterations.labels(provider=provider).observe(iterations)
        except Exception:
            logger.debug("Metric increment failed (tool_loop_iterations)", exc_info=True)

    # If original request was streaming and the model is done calling tools,
    # re-forward the final call as streaming so the client gets progressive output.
    if (
        original_streaming
        and iterations > 0
        and not current_model.has_pending_tool_calls
        and final_http_resp is not None
    ):
        try:
            streaming_call = _restore_streaming(current_call)
            buf: list[bytes] = []
            streaming_resp, _ = await stream_with_tee(adapter, streaming_call, request, buffer=buf)
            return ToolExecResult(
                call=streaming_call, model_response=current_model,
                interactions=all_interactions, iterations=iterations,
                http_response=final_http_resp,
                streaming_response=streaming_resp,
                stream_buffer=buf,
            )
        except Exception as exc:
            record_tool_exception(tool="unknown", error=str(exc) or type(exc).__name__)
            logger.warning("Failed to stream final tool answer — using non-streaming response", exc_info=True)

    return ToolExecResult(
        call=current_call, model_response=current_model,
        interactions=all_interactions, iterations=iterations,
        http_response=final_http_resp,
    )


# ── execute_tools (entry point 2) ──────────────────────────────────────────

async def execute_tools(
    strategy: str,
    call: ModelCall,
    model_response: ModelResponse,
    http_response: Response,
    adapter: ProviderAdapter,
    request: Request,
    ctx, settings, provider: str,
    original_streaming: bool = False,
) -> ToolExecResult:
    """Run tool strategy: active loop or passive collection.

    Also handles tool-unsupported retry and capability caching.
    Called from orchestrator after the initial forward().
    """
    # Tool-unsupported retry: model rejected tools → cache + strip + retry
    if is_tool_unsupported_error(http_response.status_code, bytes(http_response.body)):
        if ctx.capability_registry:
            ctx.capability_registry.record(call.model_id, supports_tools=False, provider=provider)
        logger.info("Model %s: tools not supported — retrying without", call.model_id)
        call = strip_tools_from_call(call)
        strategy = "none"
        from gateway.pipeline.forwarder import forward as _fwd
        http_response, model_response = await _fwd(adapter, call, request)
        return ToolExecResult(
            call=call, model_response=model_response,
            interactions=[], iterations=0,
            http_response=http_response,
        )

    # Cache successful tool support
    if strategy == "active" and http_response.status_code < 400:
        if ctx.capability_registry:
            ctx.capability_registry.record(call.model_id, supports_tools=True, provider=provider)

    # Passive: just collect tool interactions from the provider response
    if strategy == "passive" and model_response.tool_interactions:
        interactions = list(model_response.tool_interactions)
        _emit_tool_metrics(interactions, provider, "provider")
        return ToolExecResult(
            call=call, model_response=model_response,
            interactions=interactions, iterations=0,
        )

    # Active: run the gateway-side tool loop
    if strategy == "active" and ctx.tool_registry and model_response.has_pending_tool_calls:
        try:
            return await _run_active_tool_loop(
                adapter, call, request, model_response,
                ctx, settings, provider, original_streaming,
            )
        except Exception as exc:
            record_tool_exception(tool="tool_loop", error=str(exc) or type(exc).__name__)
            logger.error("Active tool loop failed — falling back to original response", exc_info=True)
            return ToolExecResult(
                call=call, model_response=model_response,
                interactions=[], iterations=0,
            )

    # Phase 24.4: Active strategy was requested but the model didn't call any
    # tools. Instead of re-forwarding with stream=true (which doubles latency
    # because the model regenerates the whole answer), synthesize OpenAI SSE
    # chunks from the already-parsed non-streaming response. Zero extra upstream
    # round trips — just fast byte-level fabrication of a stream.
    if (
        strategy == "active"
        and original_streaming
        and not model_response.has_pending_tool_calls
        and http_response.status_code < 400
    ):
        try:
            from gateway.pipeline.forwarder import build_synthesized_streaming_response
            streaming_resp = build_synthesized_streaming_response(
                model_response,
                call.model_id,
                session_id=call.metadata.get("session_id", ""),
            )
            return ToolExecResult(
                call=call, model_response=model_response,
                interactions=[], iterations=0,
                http_response=http_response,
                streaming_response=streaming_resp,
                stream_buffer=[],  # no parsing needed — see synthetic_stream flag
                synthetic_stream=True,
            )
        except Exception as exc:
            record_tool_exception(tool="stream_synthesize", error=str(exc) or type(exc).__name__)
            logger.warning(
                "Failed to synthesize streaming response — returning non-streaming",
                exc_info=True,
            )

    # No tool activity
    return ToolExecResult(
        call=call, model_response=model_response,
        interactions=[], iterations=0,
    )


# ── Audit helpers ───────────────────────────────────────────────────────────

def _serialize_tool_interaction(t: ToolInteraction, source: str) -> dict[str, Any]:
    """Serialize one ToolInteraction to audit metadata dict."""
    d: dict[str, Any] = {"tool_id": t.tool_id, "tool_type": t.tool_type, "tool_name": t.tool_name, "source": source}
    if t.sources:
        d["sources"] = t.sources
    if t.metadata:
        d.update(t.metadata)
    return d


def build_tool_audit_metadata(
    interactions: list[ToolInteraction], strategy: str, iterations: int,
) -> dict[str, Any]:
    """Build tool_* keys to merge into execution record metadata."""
    if not interactions:
        return {}
    source = "provider" if strategy == "passive" else "gateway"
    result: dict[str, Any] = {
        "tool_strategy": strategy,
        "tool_interaction_count": len(interactions),
        "tool_interactions": [_serialize_tool_interaction(t, source) for t in interactions],
    }
    if iterations > 0:
        result["tool_loop_iterations"] = iterations
    return result


def _build_tool_event_record(
    t: ToolInteraction,
    execution_id: str,
    session_id: str | None,
    prompt_id: str,
    source: str,
    tenant_id: str,
    gateway_id: str,
) -> dict[str, Any]:
    """Build a tool event record for storage (Walacor ETId 9000023 / WAL)."""
    record: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "execution_id": execution_id,
        "session_id": session_id,
        "prompt_id": prompt_id,
        "tenant_id": tenant_id,
        "gateway_id": gateway_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "tool_call",
        "tool_id": t.tool_id,
        "tool_type": t.tool_type,
        "tool_name": t.tool_name or t.tool_type,
        "source": source,
    }
    if t.input_data is not None:
        record["input_data"] = t.input_data
    if t.output_data is not None:
        record["output_data"] = t.output_data
    if t.sources:
        record["sources"] = t.sources
    if t.metadata:
        record["iteration"] = t.metadata.get("iteration")
        record["duration_ms"] = t.metadata.get("duration_ms")
        record["is_error"] = t.metadata.get("is_error")
    return record


async def write_tool_events(
    interactions: list[ToolInteraction],
    execution_id: str,
    call: ModelCall,
    strategy: str,
    ctx: Any,
    settings: Any,
) -> None:
    """Write each tool interaction as a first-class audit event record (dual-write)."""
    if not interactions:
        return
    session_id = call.metadata.get("session_id")
    prompt_id = call.metadata.get("prompt_id", "")
    source = "provider" if strategy == "passive" else "gateway"
    for t in interactions:
        record = _build_tool_event_record(
            t, execution_id, session_id, prompt_id, source,
            settings.gateway_tenant_id, settings.gateway_id,
        )
        # Content analysis on tool output
        if ctx.content_analyzers and t.output_data is not None:
            output_text = (t.output_data if isinstance(t.output_data, str)
                           else json.dumps(t.output_data, default=str))
            analysis = await analyze_text(output_text, ctx.content_analyzers)
            if analysis:
                record["content_analysis"] = analysis
        # Schema validation before write
        try:
            _si = getattr(ctx, "schema_intelligence", None)
            if _si:
                record, _ = _si.validate_tool_event(record)
            else:
                from gateway.classifier.unified import validate_tool_event as _sv_te
                record, _ = _sv_te(record)
        except Exception as _val_err:
            record_tool_exception(
                tool=getattr(t, "tool_name", "unknown") or "unknown",
                error=str(_val_err) or type(_val_err).__name__,
            )
            logger.error("Tool event schema validation failed: %s", _val_err)
        if ctx.storage:
            await ctx.storage.write_tool_event(record)


def emit_tool_metrics(interactions: list[ToolInteraction], provider: str, source: str) -> None:
    """Increment Prometheus tool_calls_total for each interaction."""
    for t in interactions:
        try:
            tool_calls_total.labels(provider=provider, tool_type=t.tool_type, source=source).inc()
        except Exception:
            logger.debug("Metric increment failed (tool_calls_total)", exc_info=True)


# Keep private name as alias for internal use within this module
_emit_tool_metrics = emit_tool_metrics
