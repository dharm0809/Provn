"""Eager session eviction on promotion.

ShadowRunner subscribes to `ModelRegistry.subscribe_promotion` and drops
every cached `InferenceSession` for the promoted model the instant the
filesystem swap completes — no waiting for an out-of-band cleanup call
and no leaking until process restart.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.intelligence.registry import ModelRegistry
from gateway.intelligence.shadow import ShadowRunner


class _StubDB:
    path = ":memory:"


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    reg = ModelRegistry(str(tmp_path))
    reg.ensure_structure()
    return reg


def _write_candidate(registry: ModelRegistry, model: str, version: str) -> Path:
    p = registry.base / "candidates" / f"{model}-{version}.onnx"
    p.write_bytes(b"\x00")
    return p


@pytest.mark.anyio
async def test_promotion_evicts_all_sessions_for_promoted_model(
    registry: ModelRegistry, anyio_backend: str
):
    runner = ShadowRunner(_StubDB(), registry=registry)

    # Two candidate sessions cached for `intent` (e.g. v1 lost, v2 won).
    runner._sessions[("intent", "v1")] = object()
    runner._sessions[("intent", "v2")] = object()
    # An unrelated model's session must survive.
    runner._sessions[("safety", "v9")] = object()

    _write_candidate(registry, "intent", "v2")

    await registry.promote("intent", "v2")

    # Both intent sessions gone — including the winner. The promoted ONNX
    # is now served via the production-session reload path, not this cache.
    assert ("intent", "v1") not in runner._sessions
    assert ("intent", "v2") not in runner._sessions
    # Unrelated model untouched.
    assert ("safety", "v9") in runner._sessions


@pytest.mark.anyio
async def test_subscribe_promotion_callback_is_invoked(
    registry: ModelRegistry, anyio_backend: str
):
    events: list[tuple[str, str]] = []
    registry.subscribe_promotion(lambda m, v: events.append((m, v)))

    _write_candidate(registry, "intent", "v7")
    await registry.promote("intent", "v7")

    assert events == [("intent", "v7")]


@pytest.mark.anyio
async def test_misbehaving_listener_does_not_break_promotion(
    registry: ModelRegistry, anyio_backend: str
):
    def boom(model: str, version: str) -> None:
        raise RuntimeError("subscriber blew up")

    registry.subscribe_promotion(boom)
    _write_candidate(registry, "intent", "v3")

    # Promotion still succeeds: file moved, generation bumped.
    gen_before = registry.get_generation("intent")
    await registry.promote("intent", "v3")
    assert registry.get_generation("intent") == gen_before + 1
    assert registry.production_path("intent").exists()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
