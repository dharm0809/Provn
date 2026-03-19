"""Tool registry: manages all MCP server connections and provides a unified tool catalog.

At startup the registry connects to every configured MCP server, lists its tools,
and builds a unified name → client map.  The orchestrator calls execute_tool() during
the active-strategy tool loop and get_tool_definitions() to inject tool specs into
local-model requests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from gateway.mcp.client import MCPClient, MCPServerConfig, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Unified facade over one or more MCP server connections."""

    def __init__(self, servers: list[MCPServerConfig], extra_allowed_commands: set[str] | None = None) -> None:
        self._servers = servers
        self._extra_allowed_commands = extra_allowed_commands
        self._clients: dict[str, MCPClient] = {}   # server_name → client
        self._tool_map: dict[str, MCPClient] = {}  # tool_name   → owning client

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Connect to all configured MCP servers. Raises RuntimeError if tool name conflicts exist."""
        conflicts: list[str] = []
        for config in self._servers:
            try:
                client = MCPClient(config, extra_allowed_commands=self._extra_allowed_commands)
                await client.connect()
                self._clients[config.name] = client
                for tool in client.get_tools():
                    if tool.name in self._tool_map:
                        existing = self._tool_map[tool.name]._config.name
                        conflicts.append(
                            f"'{tool.name}' defined by both '{existing}' and '{config.name}'"
                        )
                    else:
                        self._tool_map[tool.name] = client  # only register if no conflict
            except Exception as exc:
                logger.error("Failed to connect to MCP server '%s': %s", config.name, exc)
        if conflicts:
            raise RuntimeError(
                "MCP tool name conflicts — resolve before starting: " + "; ".join(conflicts)
            )

    async def shutdown(self) -> None:
        """Gracefully disconnect from all MCP servers."""
        for name, client in self._clients.items():
            try:
                await client.close()
                logger.debug("MCP server '%s' disconnected", name)
            except Exception as exc:
                logger.warning("Error closing MCP server '%s': %s", name, exc)
        self._clients.clear()
        self._tool_map.clear()

    async def register_builtin_client(self, name: str, client: Any) -> None:
        """Register a built-in tool client (Python callable, no MCP transport needed).

        The client must implement:
          get_tools() -> list[ToolDefinition]
          async call_tool(name, arguments, timeout_ms) -> ToolResult
        """
        self._clients[name] = client
        for tool in client.get_tools():
            if tool.name in self._tool_map:
                existing = self._tool_map[tool.name]
                existing_name = getattr(getattr(existing, "_config", None), "name", name)
                raise RuntimeError(
                    f"Built-in tool '{tool.name}' conflicts with tool from '{existing_name}'"
                )
            self._tool_map[tool.name] = client
        logger.info(
            "Built-in tool client '%s' registered: tools=%s",
            name, [t.name for t in client.get_tools()],
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return all available tools in OpenAI function-calling format."""
        definitions: list[dict[str, Any]] = []
        for client in self._clients.values():
            for tool in client.get_tools():
                definitions.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                })
        return definitions

    def get_tool_count(self) -> int:
        return len(self._tool_map)

    def server_names(self) -> list[str]:
        return list(self._clients.keys())

    def get_tool_schema(self, tool_name: str) -> dict | None:
        """Return the input_schema for a named tool, or None if unknown."""
        client = self._tool_map.get(tool_name)
        if client is None:
            return None
        for tool in client.get_tools():
            if tool.name == tool_name:
                return tool.input_schema
        return None

    # ── Execution ─────────────────────────────────────────────────────────────

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        """Dispatch a tool call to the owning MCP server."""
        client = self._tool_map.get(tool_name)
        if client is None:
            logger.warning("Attempted to call unknown tool '%s'", tool_name)
            return ToolResult(
                content=f"Unknown tool: '{tool_name}'. Available: {list(self._tool_map)}",
                is_error=True,
            )
        return await client.call_tool(tool_name, arguments, timeout_ms=timeout_ms)


# ── Config parsing ────────────────────────────────────────────────────────────

def parse_mcp_server_configs(json_str: str) -> list[MCPServerConfig]:
    """Parse WALACOR_MCP_SERVERS_JSON — accepts a JSON string or a file path."""
    if not json_str:
        return []

    raw = json_str.strip()

    # If it doesn't start with [ or { treat as a file path
    if not raw.startswith(("[", "{")):
        path = Path(raw)
        if not path.exists():
            logger.warning("MCP servers JSON file not found: '%s'", raw)
            return []
        raw = path.read_text()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse WALACOR_MCP_SERVERS_JSON: %s", exc)
        return []

    if isinstance(data, dict):
        data = [data]

    configs: list[MCPServerConfig] = []
    for item in data:
        try:
            configs.append(MCPServerConfig(
                name=item["name"],
                transport=item.get("transport", "http"),
                url=item.get("url"),
                command=item.get("command"),
                args=item.get("args") or [],
                env=item.get("env"),
            ))
        except KeyError as exc:
            logger.error("MCP server config missing required field %s: %s", exc, item)
    return configs
