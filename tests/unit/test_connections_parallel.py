"""Connections snapshot builds tiles concurrently, not serially.

Pre-fix `build_snapshot` awaited every tile builder sequentially. On prod
under contention from the precompute worker + chain worker, this meant
cold-load wall-clock was the SUM of every tile's cost — observed at 8-10s
which timed out OpenWebUI's polling and the dashboard alike.

This test pins the parallelism: with 5 fake-slow tile builders each taking
500ms, the total build must finish in well under 5×500ms = 2.5s. If
build_snapshot ever regresses to a serial loop, this test fails loudly.
"""
from __future__ import annotations

import asyncio
import time
import types
from unittest.mock import patch

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_build_snapshot_runs_tiles_concurrently(monkeypatch):
    """5 slow tile builders × 500ms each → total < 1500ms when parallel."""
    from gateway.connections import builder as conn_builder

    # Patch _safe_build to inject artificial delay per tile.
    async def slow_tile(tile_id, ctx):
        await asyncio.sleep(0.5)
        return {"id": tile_id, "status": "green", "headline": f"{tile_id} mocked"}

    async def empty_events(ctx):
        return []

    # Trim the TILE_ORDER list to 5 tiles so the test is fast.
    fake_order = ("providers", "walacor_delivery", "analyzers", "tool_loop", "auth")

    with patch.object(conn_builder, "TILE_ORDER", fake_order), \
         patch.object(conn_builder, "_safe_build", slow_tile), \
         patch.object(conn_builder, "build_events", empty_events):
        ctx = types.SimpleNamespace()
        start = time.monotonic()
        result = await conn_builder.build_snapshot(ctx)
        elapsed = time.monotonic() - start

    # 5 tiles × 0.5s serial = 2.5s. Parallel = ~0.5s + small overhead.
    # Allow 1.0s headroom for slow CI runners.
    assert elapsed < 1.0, (
        f"build_snapshot took {elapsed:.2f}s for 5 × 500ms tiles — "
        f"expected <1s if parallel. Serial would be ~2.5s."
    )
    assert len(result["tiles"]) == 5
    assert all(t["status"] == "green" for t in result["tiles"])


@pytest.mark.anyio
async def test_build_snapshot_handles_one_tile_raising(monkeypatch):
    """One tile raising must NOT cancel sibling tiles (gather with
    return_exceptions=True). The bad tile becomes an `unknown` entry;
    others render normally."""
    from gateway.connections import builder as conn_builder

    async def maybe_raise(tile_id, ctx):
        if tile_id == "auth":
            raise RuntimeError("simulated auth tile failure")
        return {"id": tile_id, "status": "green", "headline": f"{tile_id} ok"}

    async def empty_events(ctx):
        return []

    fake_order = ("providers", "auth", "analyzers")

    with patch.object(conn_builder, "TILE_ORDER", fake_order), \
         patch.object(conn_builder, "_safe_build", maybe_raise), \
         patch.object(conn_builder, "build_events", empty_events):
        ctx = types.SimpleNamespace()
        result = await conn_builder.build_snapshot(ctx)

    tiles_by_id = {t["id"]: t for t in result["tiles"]}
    assert tiles_by_id["providers"]["status"] == "green"
    assert tiles_by_id["analyzers"]["status"] == "green"
    assert tiles_by_id["auth"]["status"] == "unknown"
