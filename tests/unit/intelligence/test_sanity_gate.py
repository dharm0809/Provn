"""Phase 25 Task 25 wiring: SanityRunner ↔ shadow_gate.process_candidate.

Verifies that the sanity gate sits BEFORE auto-promote and never
silently approves:

  * sanity passes  → promotion proceeds.
  * sanity fails   → promotion is BLOCKED, candidate stays in
                     candidates/, `model_promotion_blocked` is emitted,
                     and the shadow_complete event carries `passed=False`.
  * sanity raises  → treated as a failure (block, not approve).
  * model unwired  → sanity skipped with a loud log; promotion proceeds
                     so existing safety / schema_mapper deployments
                     aren't permanently stranded by the strategy table.

The intent-adapter happy path is exercised against a stub session in
the dedicated test file rather than spinning up real ONNX bytes — the
adapter's contract is one-line + two-line decode, and the wiring
boundary is what matters here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gateway.intelligence.events import EventType, LifecycleEvent
from gateway.intelligence.registry import ModelRegistry
from gateway.intelligence.sanity_runner import SanityResult, SanityRunner
from gateway.intelligence.shadow_gate import GateResult, process_candidate
from gateway.intelligence.shadow_metrics import ShadowMetrics


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass
class _Settings:
    shadow_sample_target: int = 100
    shadow_min_accuracy_delta: float = 0.02
    shadow_max_disagreement: float = 0.40
    shadow_max_error_rate: float = 0.05
    auto_promote_models_list: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.auto_promote_models_list is None:
            self.auto_promote_models_list = []


class _Writer:
    def __init__(self) -> None:
        self.events: list[LifecycleEvent] = []

    async def write_event(self, event: LifecycleEvent) -> None:
        self.events.append(event)


def _metrics(
    *,
    model_name: str = "intent",
    candidate_version: str = "v2",
    sample_count: int = 1000,
    labeled_count: int = 500,
    candidate_accuracy: float = 0.92,
    production_accuracy: float = 0.85,
    disagreement_rate: float = 0.1,
    candidate_error_rate: float = 0.01,
    mcnemar_p_value: float = 0.001,
) -> ShadowMetrics:
    return ShadowMetrics(
        model_name=model_name,
        candidate_version=candidate_version,
        sample_count=sample_count,
        labeled_count=labeled_count,
        candidate_accuracy=candidate_accuracy,
        production_accuracy=production_accuracy,
        disagreement_rate=disagreement_rate,
        candidate_error_rate=candidate_error_rate,
        mcnemar_p_value=mcnemar_p_value,
    )


def _make_registry(tmp_path: Path) -> ModelRegistry:
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"v2")
    r.enable_shadow("intent", "v2")
    return r


# ── helpers: sanity check stubs ───────────────────────────────────────────


def _sanity_pass(_model, _version, _registry, _runner):
    """Pretend sanity ran and returned a clean pass."""
    return SanityResult(
        passed=True,
        overall_accuracy=1.0,
        per_class_accuracy={"normal": 1.0},
        per_class_counts={"normal": 4},
        per_class_wrong={"normal": 0},
        failing_classes=[],
        total_examples=4,
    ), None


def _sanity_fail(_model, _version, _registry, _runner):
    """Sanity ran and the candidate fell below the per-class floor."""
    return SanityResult(
        passed=False,
        overall_accuracy=0.5,
        per_class_accuracy={"normal": 0.5, "web_search": 1.0},
        per_class_counts={"normal": 4, "web_search": 4},
        per_class_wrong={"normal": 2, "web_search": 0},
        failing_classes=["normal"],
        total_examples=8,
    ), "sanity: per-class accuracy below floor for normal"


def _sanity_raises(_model, _version, _registry, _runner):
    """Sanity itself crashed — adapter raised, registry threw, etc.

    Return-shape matches the contract: result is None (no fixture run
    completed), reason is non-None (treat as failure).
    """
    return None, "sanity adapter raised: RuntimeError('boom')"


# ── tests ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_sanity_pass_allows_promotion(tmp_path):
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=["intent"])

    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=writer,
        sanity_check=_sanity_pass,
    )

    assert result.promoted is True
    assert result.promotion_event is not None
    assert result.promotion_blocked_event is None
    # Production was swapped — registry layer responsibility.
    assert (tmp_path / "production" / "intent.onnx").read_bytes() == b"v2"
    # No `model_promotion_blocked` event emitted on success.
    assert not any(
        e.event_type == EventType.MODEL_PROMOTION_BLOCKED for e in writer.events
    )


@pytest.mark.anyio
async def test_sanity_fail_blocks_promotion(tmp_path):
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=["intent"])

    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=writer,
        sanity_check=_sanity_fail,
    )

    # Promotion blocked — production untouched, candidate still in
    # candidates/.
    assert result.promoted is False
    assert (tmp_path / "production" / "intent.onnx").exists() is False
    assert (tmp_path / "candidates" / "intent-v2.onnx").exists()
    # Block event emitted with the per-class detail attached.
    assert result.promotion_blocked_event is not None
    assert result.promotion_blocked_event.event_type == EventType.MODEL_PROMOTION_BLOCKED
    payload = result.promotion_blocked_event.payload
    assert payload["model_name"] == "intent"
    assert payload["candidate_version"] == "v2"
    assert payload["failing_classes"] == ["normal"]
    assert payload["per_class_accuracy"] == {"normal": 0.5, "web_search": 1.0}
    # Sanity-block reason joined the `reasons` list so the dashboard can render it.
    assert any("sanity" in r for r in result.reasons)
    # shadow_complete event was emitted with passed=False (gate alone
    # passed, but the COMBINED verdict is False because sanity blocked).
    assert result.shadow_complete_event is not None
    assert result.shadow_complete_event.payload["passed"] is False
    # Both the block event AND shadow_complete should have been written.
    types = [e.event_type for e in writer.events]
    assert EventType.MODEL_PROMOTION_BLOCKED in types
    assert EventType.SHADOW_VALIDATION_COMPLETE in types
    assert EventType.MODEL_PROMOTED not in types


@pytest.mark.anyio
async def test_sanity_runner_raises_blocks_promotion(tmp_path):
    """Adapter / runner crash must NOT silently approve."""
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=["intent"])

    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=writer,
        sanity_check=_sanity_raises,
    )

    assert result.promoted is False
    assert result.promotion_blocked_event is not None
    payload = result.promotion_blocked_event.payload
    # When the runner itself raised we don't have per-class data —
    # the event still carries the detail string so the dashboard can
    # explain the block.
    assert "sanity adapter raised" in payload["detail"]
    # No per-class data, but failing_classes is always present (empty).
    assert payload["failing_classes"] == []
    assert (tmp_path / "production" / "intent.onnx").exists() is False


@pytest.mark.anyio
async def test_sanity_skipped_when_model_unwired(tmp_path):
    """`safety` has no wired adapter — sanity skips, promotion proceeds.

    Drives the real `_run_sanity_check` (no `sanity_check` override) so
    we're testing the actual `is_wired` carve-out, not a stub.
    """
    reg = ModelRegistry(base_path=str(tmp_path))
    reg.ensure_structure()
    (tmp_path / "candidates" / "safety-v9.onnx").write_bytes(b"v9")
    reg.enable_shadow("safety", "v9")
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=["safety"])

    result = await process_candidate(
        metrics=_metrics(model_name="safety", candidate_version="v9"),
        settings=settings, registry=reg, walacor_writer=writer,
    )

    assert result.promoted is True
    assert result.promotion_blocked_event is None
    assert result.sanity_result is None  # skipped, not run


@pytest.mark.anyio
async def test_sanity_not_run_when_gate_fails(tmp_path):
    """Cheap-gate-first ordering: don't waste an ORT load on a metric-fail."""
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(
        shadow_sample_target=100,
        auto_promote_models_list=["intent"],
    )

    called = {"n": 0}

    def _tracking_sanity(*args, **kwargs):
        called["n"] += 1
        return None, None

    result = await process_candidate(
        # sample_count=10 fails the sample gate; sanity must not run.
        metrics=_metrics(sample_count=10),
        settings=settings, registry=reg, walacor_writer=writer,
        sanity_check=_tracking_sanity,
    )

    assert result.promoted is False
    assert called["n"] == 0
    assert result.shadow_complete_event.payload["passed"] is False


@pytest.mark.anyio
async def test_sanity_not_run_when_model_not_allowlisted(tmp_path):
    """Manual-review path doesn't need the offline check either."""
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=[])  # nothing auto

    called = {"n": 0}

    def _tracking_sanity(*args, **kwargs):
        called["n"] += 1
        return None, None

    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=writer,
        sanity_check=_tracking_sanity,
    )

    assert result.promoted is False
    assert called["n"] == 0
    # Manual-review path still emits shadow_complete with passed=True
    # (gate did pass — sanity just didn't run).
    assert result.shadow_complete_event.payload["passed"] is True


# ── _run_sanity_check (the production helper, not a stub) ─────────────────


def test_run_sanity_check_blocks_when_candidate_file_missing(tmp_path):
    from gateway.intelligence.shadow_gate import _run_sanity_check

    reg = ModelRegistry(base_path=str(tmp_path))
    reg.ensure_structure()
    # Don't write a candidate file — the registry returns a path that
    # doesn't exist; the adapter should raise FileNotFoundError, the
    # helper should translate that into a sanity FAILURE.
    runner = SanityRunner(fixtures_dir=tmp_path)
    result, reason = _run_sanity_check("intent", "v2", reg, runner)

    assert result is None
    assert reason is not None
    assert "candidate file missing" in reason


def test_run_sanity_check_skips_unwired_model(tmp_path):
    from gateway.intelligence.shadow_gate import _run_sanity_check

    reg = ModelRegistry(base_path=str(tmp_path))
    reg.ensure_structure()
    runner = SanityRunner(fixtures_dir=tmp_path)
    # `safety` is unwired — should return (None, None), the
    # "skip-but-allow-promotion" signal.
    result, reason = _run_sanity_check("safety", "v9", reg, runner)
    assert result is None
    assert reason is None
