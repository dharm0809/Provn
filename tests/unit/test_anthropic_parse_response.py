"""parse_response must log + surface parse failures, not silently return empty."""
from __future__ import annotations
import logging
import httpx
import pytest
from gateway.adapters.anthropic import AnthropicAdapter


def test_parse_response_raises_on_non_json(caplog: pytest.LogCaptureFixture) -> None:
    adapter = AnthropicAdapter(base_url="https://api.anthropic.com", api_key="test-key")
    caplog.set_level(logging.WARNING, logger="gateway.adapters.anthropic")
    resp = httpx.Response(
        status_code=502,
        content=b"<html><body>Bad Gateway</body></html>",
        headers={"content-type": "text/html"},
    )
    with pytest.raises(ValueError, match="Anthropic response body is not valid JSON"):
        adapter.parse_response(resp)
    assert any("parse_response" in r.getMessage() for r in caplog.records)
