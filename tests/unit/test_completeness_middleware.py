"""Unit tests for the completeness middleware finally-block invariant.

Per CLAUDE.md: every request must produce an attempt record via
`completeness_middleware`'s `finally` block — including when the
handler raises mid-flight or a streaming response disconnects mid-body.

These tests stand up a minimal Starlette app with the real middleware
mounted, swap in a recording StorageRouter, and exercise four cases:

  1. Successful request → one attempt row written with the right fields
  2. Handler raises mid-request → attempt row STILL written
  3. Streaming response abrupt-disconnect → attempt row written
  4. Skip-list paths (e.g. /lineage/) → NO attempt row

We use a real httpx.AsyncClient + ASGITransport (no TestClient sync
shim). The pipeline context's storage is replaced with an in-memory
fake so we can introspect every write call.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from gateway.middleware.completeness import (
    _pending_attempt_writes,
    completeness_middleware,
)
from gateway.pipeline.context import get_pipeline_context


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Recording storage stub ──────────────────────────────────────────────


class _RecordingStorage:
    """In-memory StorageRouter stand-in that captures every write call.

    The real router is a fan-out; the middleware only calls
    `write_attempt`, which is fire-and-forget. We mirror that contract
    here and snapshot each call so the tests can assert.
    """

    def __init__(self) -> None:
        self.attempts: list[dict] = []

    async def write_attempt(self, record: dict) -> None:
        # Snapshot the dict to defeat any in-place mutation
        self.attempts.append(dict(record))


# ── Test app fixture ────────────────────────────────────────────────────


async def _ok(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def _boom(request: Request) -> JSONResponse:
    raise RuntimeError("handler exploded mid-request")


async def _stream(request: Request) -> StreamingResponse:
    async def body():
        yield b"chunk-1"
        # Simulate stream interruption: raise after first chunk lands.
        # In production this is what an upstream provider hangup looks
        # like to the SSE generator.
        raise RuntimeError("upstream disconnected mid-stream")

    return StreamingResponse(body(), media_type="text/plain")


async def _lineage(request: Request) -> JSONResponse:
    """Path on the middleware skip-list."""
    return JSONResponse({"sessions": []})


def _build_app() -> Starlette:
    routes = [
        Route("/v1/chat/completions", _ok, methods=["POST"]),
        Route("/v1/boom", _boom, methods=["POST"]),
        Route("/v1/stream", _stream, methods=["GET"]),
        Route("/lineage/sessions", _lineage, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(BaseHTTPMiddleware, dispatch=completeness_middleware)
    return app


@pytest.fixture
def patched_storage(monkeypatch):
    """Swap pipeline_context.storage with a recording stub for the test."""
    ctx = get_pipeline_context()
    storage = _RecordingStorage()
    original = ctx.storage
    ctx.storage = storage  # type: ignore[assignment]
    yield storage
    ctx.storage = original


async def _drain_pending_attempts() -> None:
    """The middleware writes attempts in a background task. Wait for them."""
    # Snapshot the live set; tasks self-discard on completion.
    pending = list(_pending_attempt_writes)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ── 1. Successful request ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_successful_request_writes_attempt_row(patched_storage):
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway"
    ) as client:
        resp = await client.post("/v1/chat/completions", json={})

    assert resp.status_code == 200
    await _drain_pending_attempts()

    assert len(patched_storage.attempts) == 1, (
        f"Expected exactly 1 attempt row, got {patched_storage.attempts}"
    )
    rec = patched_storage.attempts[0]
    assert rec["path"] == "/v1/chat/completions"
    assert rec["status_code"] == 200
    assert rec["request_id"]  # populated
    # disposition default is `error_gateway` — the pipeline (not exercised
    # here) is what overwrites it on a real success path. We only assert
    # the contract: middleware writes SOME disposition.
    assert "disposition" in rec


# ── 2. Handler raises → attempt STILL written ───────────────────────────


@pytest.mark.anyio
async def test_handler_exception_still_writes_attempt(patched_storage):
    """Finally-block invariant: an exception in the handler must NOT
    skip the attempt write. status_code falls back to 500."""
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway"
    ) as client:
        # BaseHTTPMiddleware re-raises; ASGITransport surfaces a 500.
        try:
            resp = await client.post("/v1/boom", json={})
            # Some Starlette versions return a 500, others propagate.
            # We don't actually assert on the response here — the
            # invariant is the attempt row.
            assert resp.status_code in (500, 502)
        except Exception:
            # Even if the transport propagates, the finally block ran.
            pass

    await _drain_pending_attempts()

    matching = [a for a in patched_storage.attempts if a["path"] == "/v1/boom"]
    assert len(matching) == 1, (
        f"Expected attempt row even after handler raised; got attempts: "
        f"{patched_storage.attempts}"
    )
    rec = matching[0]
    # When the handler raises, response is None → status falls back to 500.
    assert rec["status_code"] == 500


# ── 3. Streaming abrupt disconnect → attempt STILL written ──────────────


@pytest.mark.anyio
async def test_streaming_disconnect_writes_attempt(patched_storage):
    """The finally clause must run even when a streaming response is
    interrupted mid-body (upstream provider hangup, network teardown,
    client abort). We simulate this with a generator that raises after
    the first chunk; what matters is the middleware's `finally` block
    still lands an attempt row.
    """
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway", timeout=5.0
    ) as client:
        try:
            async with client.stream("GET", "/v1/stream") as resp:
                async for _chunk in resp.aiter_bytes():
                    pass  # consume; the second chunk raises
        except Exception:
            # ASGITransport surfaces the in-stream exception to the client.
            # The middleware's finally block must still have run.
            pass

    # Background attempt write — wait briefly for the task to finish.
    for _ in range(20):
        await _drain_pending_attempts()
        if any(a["path"] == "/v1/stream" for a in patched_storage.attempts):
            break
        await asyncio.sleep(0.05)

    matching = [a for a in patched_storage.attempts if a["path"] == "/v1/stream"]
    assert len(matching) == 1, (
        f"Streaming interruption must still produce an attempt row; "
        f"got: {patched_storage.attempts}"
    )


# ── 4. Skip-list paths produce NO attempt ──────────────────────────────


@pytest.mark.anyio
async def test_lineage_path_skipped(patched_storage):
    """Paths under `/lineage` bypass the middleware entirely — no
    request_id is generated, no attempt row is written. (Lineage UI
    requests must not pollute gateway_attempts.)"""
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway"
    ) as client:
        resp = await client.get("/lineage/sessions")

    assert resp.status_code == 200
    await _drain_pending_attempts()

    matching = [
        a for a in patched_storage.attempts
        if a["path"].startswith("/lineage")
    ]
    assert matching == [], (
        f"/lineage/* must be on the skip-list; got attempt rows: "
        f"{matching}"
    )
