"""MCP client: connects to one MCP server (HTTP/SSE or stdio) and executes tool calls.

The `mcp` package (pip install mcp) is an optional dependency required only when
WALACOR_TOOL_STRATEGY=active (or auto with a local provider).  If it is not installed
the gateway raises a clear RuntimeError at startup rather than at request time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Security: MCP stdio subprocess hardening ─────────────────────────────────

_DEFAULT_ALLOWED_COMMANDS = {"python", "python3", "python3.12", "node", "npx", "uvx"}

_SENSITIVE_ENV_PATTERNS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL")


def _validate_stdio_command(config: "MCPServerConfig", extra_allowed: set[str] | None = None) -> None:
    """Validate MCP stdio command against allowlist.

    Raises ValueError if the command base name is not in the allowed set.
    """
    if config.transport != "stdio":
        return
    allowed = _DEFAULT_ALLOWED_COMMANDS | (extra_allowed or set())
    cmd_base = Path(config.command or "").name
    if cmd_base not in allowed:
        logger.error(
            "MCP stdio command rejected: command='%s' base='%s' allowed=%s server='%s'",
            config.command, cmd_base, sorted(allowed), config.name,
        )
        raise ValueError(
            f"MCP stdio command '{config.command}' (base: '{cmd_base}') "
            f"not in allowed commands: {sorted(allowed)}. "
            f"Add to WALACOR_MCP_ALLOWED_COMMANDS to extend."
        )
    logger.warning(
        "MCP stdio command validated: command='%s' base='%s' server='%s'",
        config.command, cmd_base, config.name,
    )


def _sanitize_subprocess_env(config_env: dict[str, str] | None) -> dict[str, str]:
    """Remove sensitive env vars from MCP subprocess environment.

    If config_env is provided, it is used as the base; otherwise os.environ is
    copied.  In both cases, any key whose uppercase form contains a sensitive
    pattern (KEY, SECRET, PASSWORD, TOKEN, CREDENTIAL) is stripped.
    """
    base = dict(config_env) if config_env else dict(os.environ)
    sanitized = {k: v for k, v in base.items()
                 if not any(p in k.upper() for p in _SENSITIVE_ENV_PATTERNS)}
    removed = set(base.keys()) - set(sanitized.keys())
    if removed:
        logger.warning(
            "Stripped %d sensitive env var(s) from MCP subprocess: %s",
            len(removed), sorted(removed),
        )
    return sanitized


@dataclass
class MCPServerConfig:
    """Configuration for one MCP server connection."""

    name: str                               # logical name, e.g. "web-search"
    transport: str                          # "http" | "stdio"
    url: str | None = None                  # for HTTP/SSE transport
    command: str | None = None              # for stdio transport (e.g. "npx")
    args: list[str] = field(default_factory=list)   # argv for stdio subprocess
    env: dict[str, str] | None = None       # extra env vars for stdio subprocess


@dataclass
class ToolDefinition:
    """A tool exposed by an MCP server, in OpenAI function-calling format."""

    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str


@dataclass
class ToolResult:
    """Result of a single tool call."""

    content: str | dict | list
    is_error: bool = False
    duration_ms: float = 0.0
    sources: list[dict] | None = None  # cited URLs (e.g. web_search results)


class MCPClient:
    """Thin async wrapper around one MCP server connection.

    Lifecycle: call connect() once at startup, then call_tool() as needed,
    and close() on shutdown.  The caller (ToolRegistry) is responsible for
    lifecycle management.
    """

    def __init__(self, config: MCPServerConfig, extra_allowed_commands: set[str] | None = None) -> None:
        self._config = config
        self._extra_allowed = extra_allowed_commands
        self._session: Any = None
        self._ctx_read: Any = None   # async context manager for the transport
        self._tools: list[ToolDefinition] = []

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish connection to the MCP server and discover its tools."""
        try:
            from mcp import ClientSession  # type: ignore[import]
            from mcp.client.stdio import StdioServerParameters, stdio_client  # type: ignore[import]
            from mcp.client.sse import sse_client  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "The 'mcp' package is required for the active tool strategy. "
                "Install it with: pip install mcp"
            ) from exc

        if self._config.transport == "stdio":
            # Security: validate command against allowlist BEFORE spawning
            _validate_stdio_command(self._config, extra_allowed=self._extra_allowed)

            # Security: strip sensitive env vars from subprocess
            safe_env = _sanitize_subprocess_env(self._config.env)

            params = StdioServerParameters(
                command=self._config.command,
                args=self._config.args,
                env=safe_env,
            )
            self._ctx_read = stdio_client(params)
        elif self._config.transport == "http":
            if not self._config.url:
                raise ValueError(f"MCP server '{self._config.name}': url is required for HTTP transport")
            # Security: block SSRF — reject URLs resolving to private/internal IPs
            from gateway.security.url_validator import validate_outbound_url
            validate_outbound_url(self._config.url)
            self._ctx_read = sse_client(self._config.url)
        else:
            raise ValueError(f"MCP server '{self._config.name}': unknown transport '{self._config.transport}'")

        read, write = await self._ctx_read.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        await self._refresh_tools()

    async def _refresh_tools(self) -> None:
        result = await self._session.list_tools()
        self._tools = [
            ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema) if tool.inputSchema else {
                    "type": "object", "properties": {},
                },
                server_name=self._config.name,
            )
            for tool in result.tools
        ]
        logger.info(
            "MCP server '%s' ready: %d tools — %s",
            self._config.name,
            len(self._tools),
            [t.name for t in self._tools],
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_tools(self) -> list[ToolDefinition]:
        return list(self._tools)

    # ── Execution ─────────────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        """Execute a tool call with an enforced per-call timeout."""
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments),
                timeout=timeout_ms / 1000.0,
            )
            duration_ms = (time.perf_counter() - t0) * 1000.0

            content = _extract_result_content(result)
            return ToolResult(
                content=content,
                is_error=bool(getattr(result, "isError", False)),
                duration_ms=round(duration_ms, 2),
            )

        except asyncio.TimeoutError:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            logger.warning(
                "Tool call timed out: server=%s tool=%s timeout_ms=%d",
                self._config.name, tool_name, timeout_ms,
            )
            return ToolResult(
                content=f"Tool call timed out after {timeout_ms} ms",
                is_error=True,
                duration_ms=round(duration_ms, 2),
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._ctx_read is not None:
            try:
                await self._ctx_read.__aexit__(None, None, None)
            except Exception:
                pass
            self._ctx_read = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_result_content(result: Any) -> str:
    """Convert an MCP CallToolResult content list to a plain string."""
    blocks = getattr(result, "content", None) or []
    if not blocks:
        return ""
    parts: list[str] = []
    for block in blocks:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts) if parts else str(blocks)
