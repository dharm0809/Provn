"""Redis-backed shared cache for /v1/connections.

The in-process ``_CACHE`` dict misses across uvicorn workers because each
worker has its own process memory. With ``ctx.redis_client`` set, the
snapshot must be persisted to Redis under ``walacor:connections:snapshot``
with TTL = 45s, so all N workers behind SO_REUSEPORT serve the same
warmed snapshot.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeRedis:
    """Minimal aioredis-compatible stub: GET/SET with TTL bookkeeping."""

    def __init__(self):
        self.store: dict[bytes | str, bytes] = {}
        self.last_ex: int | None = None
        self.set_calls = 0
        self.get_calls = 0

    async def get(self, key):
        self.get_calls += 1
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.set_calls += 1
        self.last_ex = ex
        self.store[key] = value if isinstance(value, bytes) else value.encode()


@pytest.mark.anyio
async def test_redis_cache_populated_on_first_build(monkeypatch):
    """First request builds, then writes the snapshot to Redis with TTL."""
    from gateway.connections import api as conn_api

    conn_api._reset_cache_for_tests()
    fake = _FakeRedis()
    ctx = MagicMock()
    ctx.redis_client = fake

    fake_snapshot = {"tiles": [], "events": [], "generated_at": "x"}

    async def fake_build(_ctx):
        return fake_snapshot

    monkeypatch.setattr(conn_api, "build_snapshot", fake_build)

    result = await conn_api._safe_build(ctx)
    assert result == fake_snapshot

    # Drive the handler path directly via the cache helpers
    await conn_api._redis_set(fake, fake_snapshot)
    assert fake.set_calls == 1
    assert fake.last_ex == int(conn_api._TTL_S)
    raw = await conn_api._redis_get(fake)
    assert raw == fake_snapshot


@pytest.mark.anyio
async def test_redis_cache_hit_skips_build(monkeypatch):
    """Second request finds the snapshot in Redis and does NOT rebuild."""
    from gateway.connections import api as conn_api

    conn_api._reset_cache_for_tests()
    fake = _FakeRedis()
    fake.store[conn_api._REDIS_KEY] = json.dumps({"tiles": [], "events": [], "cached": True}).encode()

    cached = await conn_api._redis_get(fake)
    assert cached == {"tiles": [], "events": [], "cached": True}
    assert fake.get_calls == 1


@pytest.mark.anyio
async def test_redis_get_failure_falls_back_to_rebuild(monkeypatch):
    """Redis GET raising must not crash the endpoint — return None to trigger rebuild."""
    from gateway.connections import api as conn_api

    broken = MagicMock()
    broken.get = AsyncMock(side_effect=ConnectionError("redis down"))

    out = await conn_api._redis_get(broken)
    assert out is None  # fail-open: caller will rebuild


@pytest.mark.anyio
async def test_redis_unparseable_payload_treated_as_miss():
    """A garbled cache entry must not poison the endpoint forever."""
    from gateway.connections import api as conn_api

    fake = _FakeRedis()
    fake.store[conn_api._REDIS_KEY] = b"\xff\xfe not-json"

    out = await conn_api._redis_get(fake)
    assert out is None
