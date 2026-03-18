"""Built-in web search for local/private model active strategy.

Providers:
  duckduckgo  — DDG Instant Answers API, no key (basic results)
  brave       — Brave Search API, requires WALACOR_WEB_SEARCH_API_KEY
  serpapi     — SerpAPI, requires WALACOR_WEB_SEARCH_API_KEY
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from gateway.mcp.client import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_TOOL_NAME = "web_search"
_TOOL_DESCRIPTION = (
    "Search the web for current information. Returns titles, URLs, and snippets. "
    "Use for facts, news, or data not in training data."
)
_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The search query"},
        "max_results": {"type": "integer", "description": "Max results (default: 5)", "default": 5},
    },
    "required": ["query"],
}


class WebSearchTool:
    """MCPClient-compatible built-in web search tool."""

    def __init__(
        self,
        provider: str,
        api_key: str,
        max_results: int,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._default_max = max_results
        self._http = http_client

    def get_tools(self) -> list[ToolDefinition]:
        return [ToolDefinition(
            name=_TOOL_NAME,
            description=_TOOL_DESCRIPTION,
            input_schema=_TOOL_INPUT_SCHEMA,
            server_name="builtin_web_search",
        )]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout_ms: int = 30_000,
    ) -> ToolResult:
        if name != _TOOL_NAME:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)
        query = arguments.get("query", "")
        n = int(arguments.get("max_results", self._default_max))
        if not query:
            return ToolResult(content="Missing required argument: query", is_error=True)

        t0 = time.perf_counter()
        try:
            timeout = timeout_ms / 1000.0
            if self._provider == "brave":
                results = await self._search_brave(query, n, timeout)
            elif self._provider == "serpapi":
                results = await self._search_serpapi(query, n, timeout)
            else:
                results = await self._search_duckduckgo(query, n, timeout)
        except asyncio.TimeoutError:
            return ToolResult(content=f"Web search timed out after {timeout_ms}ms", is_error=True)
        except Exception as exc:
            logger.warning("Web search error provider=%s: %s", self._provider, exc)
            return ToolResult(content=f"Web search failed: {exc}", is_error=True)

        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        sources = [
            {"title": r.get("title", ""), "url": r.get("url", "")}
            for r in results
            if r.get("url")
        ]
        try:
            content = json.dumps({"query": query, "results": results}, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.warning("Web search result serialization failed: %s", exc)
            content = json.dumps({"query": query, "error": str(exc)})
        return ToolResult(
            content=content,
            is_error=False,
            duration_ms=duration_ms,
            sources=sources,
        )

    async def _search_brave(self, query: str, n: int, timeout: float) -> list[dict]:
        resp = await self._http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": n},
            headers={"Accept": "application/json", "X-Subscription-Token": self._api_key},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description", ""),
            }
            for r in data.get("web", {}).get("results", [])[:n]
        ]

    async def _search_serpapi(self, query: str, n: int, timeout: float) -> list[dict]:
        resp = await self._http.get(
            "https://serpapi.com/search",
            params={"q": query, "num": n, "api_key": self._api_key, "engine": "google"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
            }
            for r in data.get("organic_results", [])[:n]
        ]

    async def _search_duckduckgo(self, query: str, n: int, timeout: float) -> list[dict]:
        """DuckDuckGo web search via ddgs library (full results)."""
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                return await self._search_duckduckgo_instant(query, n, timeout)

        def _sync_search() -> list[dict]:
            raw = DDGS().text(query, max_results=n)
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                }
                for r in raw
            ]

        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _sync_search),
            timeout=timeout,
        )

    async def _search_duckduckgo_instant(self, query: str, n: int, timeout: float) -> list[dict]:
        """Fallback: DDG Instant Answers API (limited, encyclopedia-style answers only)."""
        resp = await self._http.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results: list[dict] = []
        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading", query),
                "url": data.get("AbstractURL", ""),
                "snippet": data["AbstractText"],
            })
        for topic in data.get("RelatedTopics", [])[:n - len(results)]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic.get("Text", "")[:80],
                    "url": topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", ""),
                })
        return results[:n]
