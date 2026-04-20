"""maybe_fire_shadow must retain a strong reference to the created task."""
from __future__ import annotations
import asyncio
import gc
from unittest.mock import MagicMock
import pytest
import gateway.intelligence.shadow as shadow_mod


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_maybe_fire_shadow_retains_task_reference(monkeypatch) -> None:
    runner = MagicMock()
    runner.is_enabled = True
    registry = MagicMock()
    candidate = MagicMock()
    candidate.version = "v1"
    registry.active_candidate.return_value = candidate

    async def slow_shadow(*a, **kw):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(shadow_mod, "fire_shadow_text", lambda *a, **kw: slow_shadow())
    before = len(shadow_mod._IN_FLIGHT_TASKS)
    shadow_mod.maybe_fire_shadow(
        runner=runner,
        registry=registry,
        model_name="intent",
        input_text="hello",
        production_prediction="normal",
        production_confidence=1.0,
        infer_on_session=MagicMock(),
    )
    gc.collect()
    assert len(shadow_mod._IN_FLIGHT_TASKS) == before + 1
    await asyncio.sleep(0.1)
    assert len(shadow_mod._IN_FLIGHT_TASKS) == before
