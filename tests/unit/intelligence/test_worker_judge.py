"""Tests for the distillation worker's pluggable judge URL.

Verifies the worker:
- defaults to gateway self-loopback (OpenAI shape) when constructed
  with no args — the new shape replacing the historical Ollama-only
  hardcode at `localhost:11434`
- still supports the legacy `ollama_url=` kwarg so deployments wired
  to a local Ollama don't regress
- dispatches the right HTTP shape per URL: `/chat/completions` for
  OpenAI-compatible endpoints (URL ending in /v1), `/api/chat` for
  Ollama
- never raises out of `_call_ollama` (a broken judge can't take down
  the worker loop)
"""
from __future__ import annotations

import pytest

from gateway.intelligence.worker import IntelligenceWorker


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeHttpResp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    """Stub for httpx.AsyncClient — records the last request and replies
    with a configured payload."""

    def __init__(self, reply: _FakeHttpResp) -> None:
        self._reply = reply
        self.last_url: str | None = None
        self.last_headers: dict | None = None
        self.last_json: dict | None = None

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *_a) -> None:  # noqa: ANN001
        return None

    async def post(self, url: str, *, json: dict | None = None,
                   headers: dict | None = None) -> _FakeHttpResp:
        self.last_url = url
        self.last_headers = headers or {}
        self.last_json = json or {}
        return self._reply


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, fake: _FakeHttpClient) -> None:
    """Replace httpx.AsyncClient so any `with httpx.AsyncClient(...)` block
    inside the worker yields our fake."""
    import httpx
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: fake,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Constructor + default
# ─────────────────────────────────────────────────────────────────────────────

def test_default_constructor_uses_gateway_loopback() -> None:
    w = IntelligenceWorker()
    assert w._judge_url == "http://localhost:8000/v1"
    assert w._judge_mode == "openai"
    assert w._judge_model == "claude-haiku-4-5"


def test_legacy_ollama_url_kwarg_still_works() -> None:
    """Deployments that still pass `ollama_url=...` (or tests that
    haven't migrated) must continue to function in Ollama shape."""
    w = IntelligenceWorker(ollama_url="http://oll:11434")
    assert w._judge_url == "http://oll:11434"
    assert w._judge_mode == "ollama"


def test_explicit_judge_url_overrides_default() -> None:
    w = IntelligenceWorker(judge_url="http://other:8000/v1")
    assert w._judge_url == "http://other:8000/v1"
    assert w._judge_mode == "openai"


def test_non_v1_url_routes_to_ollama_mode() -> None:
    w = IntelligenceWorker(judge_url="http://anywhere:11434")
    assert w._judge_mode == "ollama"


# ─────────────────────────────────────────────────────────────────────────────
# Request shape dispatch
# ─────────────────────────────────────────────────────────────────────────────

async def test_openai_shape_hits_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeHttpClient(_FakeHttpResp(200, {
        "choices": [{"message": {"content": '{"category":"normal","confidence":0.95}'}}],
    }))
    _patch_httpx(monkeypatch, fake)
    w = IntelligenceWorker(
        judge_url="http://localhost:8000/v1",
        judge_model="claude-haiku-4-5",
        judge_api_key="wgk-test-key",
    )
    out = await w._call_ollama("classify this", 32)
    assert out == '{"category":"normal","confidence":0.95}'
    assert fake.last_url == "http://localhost:8000/v1/chat/completions"
    assert fake.last_headers["Authorization"] == "Bearer wgk-test-key"
    assert fake.last_json["model"] == "claude-haiku-4-5"
    assert fake.last_json["temperature"] == 0
    # Worker should request structured JSON output.
    assert fake.last_json["response_format"] == {"type": "json_object"}


async def test_ollama_shape_hits_api_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeHttpClient(_FakeHttpResp(200, {
        "message": {"content": '{"category":"reasoning"}'},
    }))
    _patch_httpx(monkeypatch, fake)
    w = IntelligenceWorker(ollama_url="http://oll:11434")
    out = await w._call_ollama("classify this", 32)
    assert out == '{"category":"reasoning"}'
    assert fake.last_url == "http://oll:11434/api/chat"
    # Ollama shape: no Authorization header, has `format: json` option.
    assert "Authorization" not in (fake.last_headers or {})
    assert fake.last_json["format"] == "json"


async def test_non_200_response_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeHttpClient(_FakeHttpResp(401, {"error": "unauthorized"}))
    _patch_httpx(monkeypatch, fake)
    w = IntelligenceWorker(judge_api_key="bad")
    out = await w._call_ollama("anything", 32)
    assert out is None


async def test_exception_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken judge endpoint must NOT raise out of `_call_ollama`.

    The worker loop swallows None results; raising would crash the
    background task and the distillation buffer would stop filling.
    """
    import httpx

    class _Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def post(self, *a, **kw): raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _Boom())
    w = IntelligenceWorker()
    out = await w._call_ollama("anything", 32)
    assert out is None
