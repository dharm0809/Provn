"""Unit tests for WebSearchTool + ToolRegistry built-in registration."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gateway.mcp.client import ToolDefinition, ToolResult
from gateway.mcp.registry import ToolRegistry
from gateway.tools.web_search import WebSearchTool, _TOOL_NAME


# ---------------------------------------------------------------------------
# Fixture: asyncio backend
# ---------------------------------------------------------------------------

@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_http_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp
        )
    return resp


def _make_tool(provider: str = "duckduckgo", api_key: str = "", max_results: int = 5) -> WebSearchTool:
    http_client = MagicMock(spec=httpx.AsyncClient)
    return WebSearchTool(
        provider=provider,
        api_key=api_key,
        max_results=max_results,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# get_tools
# ---------------------------------------------------------------------------

def test_get_tools_returns_definition():
    tool = _make_tool()
    definitions = tool.get_tools()
    assert len(definitions) == 1
    defn = definitions[0]
    assert isinstance(defn, ToolDefinition)
    assert defn.name == _TOOL_NAME
    assert defn.server_name == "builtin_web_search"
    assert "query" in defn.input_schema["properties"]


# ---------------------------------------------------------------------------
# call_tool — unknown name / missing query
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_tool_unknown_tool_name():
    tool = _make_tool()
    result = await tool.call_tool("not_a_tool", {})
    assert result.is_error is True
    assert "Unknown tool" in result.content


@pytest.mark.anyio
async def test_call_tool_missing_query():
    tool = _make_tool()
    result = await tool.call_tool(_TOOL_NAME, {})
    assert result.is_error is True
    assert "Missing required argument" in result.content
    # No HTTP call was made
    tool._http.get.assert_not_called()


# ---------------------------------------------------------------------------
# call_tool — DuckDuckGo
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_tool_duckduckgo_success():
    """Mock the DDGS library so the test never hits a live API."""
    tool = _make_tool(provider="duckduckgo")
    fake_ddgs_results = [
        {"title": "Python", "href": "https://python.org", "body": "Python is a programming language."},
        {"title": "CPython", "href": "https://cpython.org", "body": "CPython is the reference implementation."},
    ]
    mock_ddgs_instance = MagicMock()
    mock_ddgs_instance.text.return_value = fake_ddgs_results
    mock_ddgs_cls = MagicMock(return_value=mock_ddgs_instance)

    with patch.dict("sys.modules", {"ddgs": MagicMock(DDGS=mock_ddgs_cls)}):
        # Force re-import path to pick up the patched module
        with patch("gateway.tools.web_search.DDGS", mock_ddgs_cls, create=True):
            # Call the internal method directly to avoid import caching issues
            results = await tool._search_duckduckgo("python programming", 5, 30.0)

    assert len(results) >= 1
    assert results[0]["title"] == "Python"
    assert results[0]["url"] == "https://python.org"

    # Also verify full call_tool path via Instant Answers fallback (no ddgs library)
    tool2 = _make_tool(provider="duckduckgo")
    ddg_payload = {
        "AbstractText": "Python is a programming language.",
        "Heading": "Python",
        "AbstractURL": "https://python.org",
        "RelatedTopics": [
            {"Text": "CPython is the reference implementation.", "FirstURL": "https://cpython.org"},
        ],
    }
    tool2._http.get = AsyncMock(return_value=_make_http_response(ddg_payload))

    with patch.dict("sys.modules", {"ddgs": None, "duckduckgo_search": None}):
        result = await tool2.call_tool(_TOOL_NAME, {"query": "python programming"})

    assert result.is_error is False
    data = json.loads(result.content)
    assert data["query"] == "python programming"
    assert len(data["results"]) >= 1
    assert data["results"][0]["title"] == "Python"
    assert result.sources is not None
    assert any(s["url"] == "https://python.org" for s in result.sources)


# ---------------------------------------------------------------------------
# call_tool — Brave
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_tool_brave_success():
    tool = _make_tool(provider="brave", api_key="test-key")
    brave_payload = {
        "web": {
            "results": [
                {"title": "Brave Result 1", "url": "https://example.com/1", "description": "Snippet 1"},
                {"title": "Brave Result 2", "url": "https://example.com/2", "description": "Snippet 2"},
            ]
        }
    }
    tool._http.get = AsyncMock(return_value=_make_http_response(brave_payload))

    result = await tool.call_tool(_TOOL_NAME, {"query": "brave test", "max_results": 2})

    assert result.is_error is False
    data = json.loads(result.content)
    assert len(data["results"]) == 2
    assert data["results"][0]["url"] == "https://example.com/1"
    assert result.sources is not None
    assert len(result.sources) == 2
    assert result.sources[0] == {"title": "Brave Result 1", "url": "https://example.com/1"}

    # Verify Brave API headers were sent
    call_kwargs = tool._http.get.call_args
    assert "X-Subscription-Token" in call_kwargs.kwargs["headers"]
    assert call_kwargs.kwargs["headers"]["X-Subscription-Token"] == "test-key"


# ---------------------------------------------------------------------------
# call_tool — SerpAPI
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_tool_serpapi_success():
    tool = _make_tool(provider="serpapi", api_key="serp-key")
    serp_payload = {
        "organic_results": [
            {"title": "SerpAPI Result", "link": "https://serp.example.com", "snippet": "Serp snippet"},
        ]
    }
    tool._http.get = AsyncMock(return_value=_make_http_response(serp_payload))

    result = await tool.call_tool(_TOOL_NAME, {"query": "serpapi test"})

    assert result.is_error is False
    data = json.loads(result.content)
    assert data["results"][0]["url"] == "https://serp.example.com"
    assert result.sources is not None
    assert result.sources[0]["url"] == "https://serp.example.com"


# ---------------------------------------------------------------------------
# call_tool — error paths
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_call_tool_timeout():
    tool = _make_tool()
    tool._http.get = AsyncMock(side_effect=asyncio.TimeoutError())

    result = await tool.call_tool(_TOOL_NAME, {"query": "timeout test"}, timeout_ms=100)

    assert result.is_error is True
    assert "timed out" in result.content


@pytest.mark.anyio
async def test_call_tool_http_error(caplog):
    import logging
    tool = _make_tool(provider="brave", api_key="bad-key")
    bad_resp = _make_http_response({}, status_code=401)
    tool._http.get = AsyncMock(return_value=bad_resp)

    with caplog.at_level(logging.WARNING, logger="gateway.tools.web_search"):
        result = await tool.call_tool(_TOOL_NAME, {"query": "will fail"})

    assert result.is_error is True
    assert "Web search failed" in result.content


# ---------------------------------------------------------------------------
# ToolResult.sources field
# ---------------------------------------------------------------------------

def test_sources_field_on_tool_result():
    sources = [{"title": "Example", "url": "https://example.com"}]
    result = ToolResult(content="data", is_error=False, sources=sources)
    assert result.sources == sources


def test_sources_field_defaults_to_none():
    result = ToolResult(content="data", is_error=False)
    assert result.sources is None


# ---------------------------------------------------------------------------
# ToolRegistry: register_builtin_client
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_register_builtin_client_adds_to_tool_map():
    registry = ToolRegistry([])
    tool = _make_tool()

    await registry.register_builtin_client("builtin_web_search", tool)

    assert registry.get_tool_count() == 1
    assert "builtin_web_search" in registry.server_names()


@pytest.mark.anyio
async def test_register_builtin_client_conflict_raises():
    registry = ToolRegistry([])
    tool1 = _make_tool()
    tool2 = _make_tool()

    await registry.register_builtin_client("builtin_web_search", tool1)

    with pytest.raises(RuntimeError, match="conflicts with tool from"):
        await registry.register_builtin_client("builtin_web_search_2", tool2)


@pytest.mark.anyio
async def test_registry_execute_tool_dispatches_to_builtin():
    registry = ToolRegistry([])
    tool = _make_tool(provider="duckduckgo")
    ddg_payload = {
        "AbstractText": "Python is great.",
        "Heading": "Python",
        "AbstractURL": "https://python.org",
        "RelatedTopics": [],
    }
    tool._http.get = AsyncMock(return_value=_make_http_response(ddg_payload))

    await registry.register_builtin_client("builtin_web_search", tool)

    result = await registry.execute_tool(_TOOL_NAME, {"query": "python"})

    assert result.is_error is False
    data = json.loads(result.content)
    assert data["query"] == "python"


@pytest.mark.anyio
async def test_get_tool_definitions_includes_builtin():
    registry = ToolRegistry([])
    tool = _make_tool()

    await registry.register_builtin_client("builtin_web_search", tool)

    definitions = registry.get_tool_definitions()
    assert len(definitions) == 1
    assert definitions[0]["function"]["name"] == _TOOL_NAME
