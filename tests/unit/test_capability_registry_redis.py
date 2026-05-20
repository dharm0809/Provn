"""Unit tests for the Redis-backed capability registry (mocked Redis).

Pins the multi-worker shared-state behavior introduced to fix per-worker
capability cache desync: a tool-unsupported probe on worker A must be
visible to worker B on the next request via Redis HSET / HGETALL.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# Pin anyio tests to asyncio backend (AsyncMock is asyncio-specific).
@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


from gateway.adaptive.capability_registry import (
    CapabilityRegistry,
    RedisCapabilityRegistry,
    make_capability_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_redis():
    """Async-Redis mock with pipeline + hgetall + scan + delete."""
    client = MagicMock()
    pipe = AsyncMock()
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    pipe.execute = AsyncMock(return_value=[True, 1, True])
    # hset/hincrby/expire on the pipe are sync builders (return self)
    pipe.hset = MagicMock(return_value=pipe)
    pipe.hincrby = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    client.pipeline = MagicMock(return_value=pipe)
    client.hgetall = AsyncMock(return_value={})
    client.delete = AsyncMock(return_value=1)
    client.scan = AsyncMock(return_value=(0, []))
    return client, pipe


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_supports_tools_returns_none_when_absent():
    client, _ = _mock_redis()
    reg = RedisCapabilityRegistry(client, ttl_seconds=3600)
    assert reg.supports_tools("llama3.1:8b") is None


@pytest.mark.anyio
async def test_record_then_supports_tools_reads_from_mirror():
    client, _ = _mock_redis()
    reg = RedisCapabilityRegistry(client, ttl_seconds=3600)
    reg.record("llama3.1:8b", supports_tools=True, provider="ollama")
    await reg.drain()
    assert reg.supports_tools("llama3.1:8b") is True


# ---------------------------------------------------------------------------
# Write path — Redis fan-out
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_record_writes_hset_and_sets_ttl():
    client, pipe = _mock_redis()
    reg = RedisCapabilityRegistry(client, ttl_seconds=86400)

    reg.record("qwen3:32b", supports_tools=False, provider="ollama")
    await reg.drain()

    # HSET called with the model's hash key
    assert pipe.hset.called
    hset_args, hset_kwargs = pipe.hset.call_args
    assert hset_args[0] == "gateway:capability:qwen3:32b"
    mapping = hset_kwargs.get("mapping") or (hset_args[1] if len(hset_args) > 1 else {})
    assert mapping["supports_tools"] == "0"
    assert mapping["provider"] == "ollama"

    # HINCRBY probe_count + EXPIRE 24h
    pipe.hincrby.assert_called_with("gateway:capability:qwen3:32b", "probe_count", 1)
    pipe.expire.assert_called_with("gateway:capability:qwen3:32b", 86400)


@pytest.mark.anyio
async def test_record_latency_appends_capped_at_50():
    client, pipe = _mock_redis()
    reg = RedisCapabilityRegistry(client, ttl_seconds=3600)
    reg.record("m", supports_tools=True)
    await reg.drain()

    for i in range(80):
        reg.record_latency("m", float(i))
    await reg.drain()

    cap = reg._cache["m"]
    assert len(cap.observed_latencies) == 50
    # last value should be the most recent sample (79.0)
    assert cap.observed_latencies[-1] == 79.0


@pytest.mark.anyio
async def test_redis_write_failure_is_swallowed_local_mirror_intact():
    client, pipe = _mock_redis()
    pipe.execute = AsyncMock(side_effect=RuntimeError("redis down"))
    reg = RedisCapabilityRegistry(client, ttl_seconds=3600)

    # Should not raise even though Redis blows up.
    reg.record("m", supports_tools=True)
    await reg.drain()

    # Local mirror still reflects the record (fail-open semantics).
    assert reg.supports_tools("m") is True


# ---------------------------------------------------------------------------
# get_timeout — matches in-memory formula
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_timeout_default_when_no_samples():
    client, _ = _mock_redis()
    reg = RedisCapabilityRegistry(client, ttl_seconds=3600)
    assert reg.get_timeout("unknown", default=99.0) == 99.0


@pytest.mark.anyio
async def test_get_timeout_uses_p95_after_three_samples():
    client, _ = _mock_redis()
    reg = RedisCapabilityRegistry(client, ttl_seconds=3600)
    reg.record("m", supports_tools=True)
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        reg.record_latency("m", v)
    await reg.drain()
    # P95 of 5 samples = index max(0, int(5*.95)-1)=3 → 4.0; *2.5 = 10.0
    assert reg.get_timeout("m", default=120.0) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Hydrate / SCAN — cross-worker visibility
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_hydrate_loads_remote_record_into_local_mirror():
    import time as _t
    client, _ = _mock_redis()
    now_str = str(_t.time()).encode()
    client.hgetall = AsyncMock(return_value={
        b"supports_tools": b"1",
        b"provider": b"ollama",
        b"probed_at": now_str,
        b"probe_count": b"3",
        b"observed_latencies": b"[1.0, 2.0]",
        b"model_type": b"chat",
    })
    reg = RedisCapabilityRegistry(client, ttl_seconds=3600)

    loaded = await reg.hydrate("llama3.1:8b")
    assert loaded is True
    # Now visible synchronously to the request hot path.
    assert reg.supports_tools("llama3.1:8b") is True


@pytest.mark.anyio
async def test_scan_all_capabilities_uses_scan_not_keys():
    client, _ = _mock_redis()
    import time as _t
    client.scan = AsyncMock(return_value=(0, [b"gateway:capability:m1"]))
    client.hgetall = AsyncMock(return_value={
        b"supports_tools": b"1",
        b"probed_at": str(_t.time()).encode(),
        b"probe_count": b"1",
        b"observed_latencies": b"[]",
        b"model_type": b"chat",
        b"provider": b"ollama",
    })
    reg = RedisCapabilityRegistry(client, ttl_seconds=10_000_000)

    out = await reg.scan_all_capabilities(scan_budget_ms=500)
    client.scan.assert_called()  # SCAN, not KEYS (KEYS isn't even mocked)
    assert "m1" in out


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------

def test_factory_returns_in_memory_when_no_redis_client():
    settings = SimpleNamespace(capability_probe_ttl_seconds=3600)
    reg = make_capability_registry(None, settings)
    assert isinstance(reg, CapabilityRegistry)


def test_factory_returns_redis_variant_when_redis_client_provided():
    client, _ = _mock_redis()
    settings = SimpleNamespace(capability_probe_ttl_seconds=3600)
    reg = make_capability_registry(client, settings)
    assert isinstance(reg, RedisCapabilityRegistry)
    assert reg._ttl == 3600


@pytest.mark.anyio
async def test_mark_for_reprobe_deletes_redis_key():
    client, _ = _mock_redis()
    reg = RedisCapabilityRegistry(client, ttl_seconds=3600)
    reg.record("m", supports_tools=True)
    await reg.drain()

    reg.mark_for_reprobe("m")
    await reg.drain()
    client.delete.assert_called_with("gateway:capability:m")
