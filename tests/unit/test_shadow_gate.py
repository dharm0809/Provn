"""Phase 25 Task 24: promotion gate tests.

`evaluate_gate` is pure logic over `ShadowMetrics` — drive with
fabricated metrics instances. `process_candidate` owns side effects;
we verify promote/shadow-complete branches with a real `ModelRegistry`
and a lifecycle-event recorder stub.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gateway.intelligence.events import EventType, LifecycleEvent
from gateway.intelligence.registry import ModelRegistry
from gateway.intelligence.shadow_gate import (
    GateResult,
    evaluate_gate,
    process_candidate,
)
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
    """Captures lifecycle events so tests can assert emission."""
    def __init__(self) -> None:
        self.events: list[LifecycleEvent] = []

    async def write_event(self, event):
        self.events.append(event)


def _metrics(
    *,
    sample_count: int = 1000,
    labeled_count: int = 500,
    candidate_accuracy: float = 0.92,
    production_accuracy: float = 0.85,
    disagreement_rate: float = 0.1,
    candidate_error_rate: float = 0.01,
    mcnemar_p_value: float = 0.001,
    model_name: str = "intent",
    candidate_version: str = "v2",
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


# ── evaluate_gate (pure) ────────────────────────────────────────────────────

def test_all_gates_pass():
    r = evaluate_gate(_metrics(), _Settings())
    assert r.passed is True
    assert r.reasons == ["all gates passed"]


def test_insufficient_samples_fails():
    r = evaluate_gate(_metrics(sample_count=50), _Settings(shadow_sample_target=100))
    assert r.passed is False
    assert any("insufficient samples" in msg for msg in r.reasons)


def test_accuracy_delta_below_threshold_fails():
    r = evaluate_gate(
        _metrics(candidate_accuracy=0.86, production_accuracy=0.85),  # delta=0.01
        _Settings(shadow_min_accuracy_delta=0.02),
    )
    assert r.passed is False
    assert any("accuracy delta" in msg for msg in r.reasons)


def test_disagreement_rate_above_threshold_fails():
    r = evaluate_gate(
        _metrics(disagreement_rate=0.5),
        _Settings(shadow_max_disagreement=0.4),
    )
    assert r.passed is False
    assert any("disagreement rate" in msg for msg in r.reasons)


def test_error_rate_above_threshold_fails():
    r = evaluate_gate(
        _metrics(candidate_error_rate=0.1),
        _Settings(shadow_max_error_rate=0.05),
    )
    assert r.passed is False
    assert any("error rate" in msg for msg in r.reasons)


def test_non_significant_mcnemar_fails():
    r = evaluate_gate(_metrics(mcnemar_p_value=0.2), _Settings())
    assert r.passed is False
    assert any("not statistically significant" in msg for msg in r.reasons)


def test_all_failures_collected_not_just_first():
    # Violate four thresholds simultaneously — reasons list must
    # contain all four, not just the first-encountered one.
    r = evaluate_gate(
        _metrics(
            sample_count=10,
            candidate_accuracy=0.5, production_accuracy=0.9,  # delta < 0
            disagreement_rate=0.8,
            candidate_error_rate=0.5,
        ),
        _Settings(),
    )
    assert r.passed is False
    assert len(r.reasons) >= 4


def test_zero_labeled_suppresses_accuracy_check():
    # With no labeled rows the accuracy-delta gate is a no-op — the
    # McNemar gate is what blocks promotion in this regime because
    # `compute_metrics` hands back p=1.0 when labeled_count==0.
    r = evaluate_gate(
        _metrics(
            sample_count=200, labeled_count=0,
            candidate_accuracy=0.0, production_accuracy=0.0,
            mcnemar_p_value=1.0,  # mirrors real `compute_metrics` output
        ),
        _Settings(shadow_sample_target=100),
    )
    # Fails, but NOT because of accuracy-delta (the gate skipped it).
    assert r.passed is False
    assert not any("accuracy delta" in msg for msg in r.reasons)
    assert any("McNemar" in msg for msg in r.reasons)


def test_metrics_dict_includes_every_field():
    r = evaluate_gate(_metrics(), _Settings())
    for k in (
        "model_name", "candidate_version", "sample_count", "labeled_count",
        "candidate_accuracy", "production_accuracy", "disagreement_rate",
        "candidate_error_rate", "mcnemar_p_value",
    ):
        assert k in r.metrics


# ── process_candidate side effects ──────────────────────────────────────────

def _make_registry(tmp_path: Path, *, with_candidate: bool = True) -> ModelRegistry:
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    if with_candidate:
        (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"v2")
        r.enable_shadow("intent", "v2")
    return r


@pytest.mark.anyio
async def test_process_auto_promotes_when_gate_passes_and_model_allowlisted(tmp_path):
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=["intent"])

    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=writer,
        dataset_hash="d1", approver="auto",
    )

    assert result.promoted is True
    assert result.promotion_event is not None
    assert result.promotion_event.event_type == EventType.MODEL_PROMOTED
    # Only the promotion event — no shadow_complete emitted because we
    # did promote.
    types = [e.event_type for e in writer.events]
    assert EventType.MODEL_PROMOTED in types
    assert EventType.SHADOW_VALIDATION_COMPLETE not in types
    # Production file is now the candidate bytes.
    assert (tmp_path / "production" / "intent.onnx").read_bytes() == b"v2"
    # Shadow marker cleared.
    assert reg.active_candidate("intent") is None


@pytest.mark.anyio
async def test_process_skips_auto_promote_when_model_not_in_allowlist(tmp_path):
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=[])  # nothing auto-promotes

    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=writer,
    )

    assert result.promoted is False
    # Shadow marker still active — candidate awaits manual approval.
    assert reg.active_candidate("intent") is not None
    assert result.shadow_complete_event.event_type == EventType.SHADOW_VALIDATION_COMPLETE
    # And the `passed` flag carried in the event is True (gate did pass).
    assert result.shadow_complete_event.payload["passed"] is True


@pytest.mark.anyio
async def test_process_emits_shadow_complete_with_passed_false_on_gate_fail(tmp_path):
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(
        shadow_sample_target=100,
        auto_promote_models_list=["intent"],  # allowlisted but gate blocks
    )

    result = await process_candidate(
        metrics=_metrics(sample_count=10),  # fails sample gate
        settings=settings, registry=reg, walacor_writer=writer,
    )

    assert result.promoted is False
    assert result.passed is False
    assert result.shadow_complete_event.payload["passed"] is False
    # No promotion event.
    assert not any(e.event_type == EventType.MODEL_PROMOTED for e in writer.events)


@pytest.mark.anyio
async def test_process_works_without_walacor_writer(tmp_path):
    # No writer — candidate still promotes (auto allowlisted) but no
    # events are captured anywhere. Must not raise.
    reg = _make_registry(tmp_path)
    settings = _Settings(auto_promote_models_list=["intent"])
    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=None,
    )
    assert result.promoted is True
    assert (tmp_path / "production" / "intent.onnx").exists()


@pytest.mark.anyio
async def test_process_falls_back_to_shadow_complete_on_promote_failure(tmp_path):
    # Candidate file missing → registry.promote raises FileNotFoundError.
    # The gate should swallow it, treat it as "not promoted", and emit
    # shadow_complete so the dashboard can see the outcome.
    reg = _make_registry(tmp_path, with_candidate=False)
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=["intent"])

    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=writer,
    )

    assert result.promoted is False
    assert result.shadow_complete_event is not None


@pytest.mark.anyio
async def test_process_carries_approver_into_promotion_event(tmp_path):
    reg = _make_registry(tmp_path)
    writer = _Writer()
    settings = _Settings(auto_promote_models_list=["intent"])

    result = await process_candidate(
        metrics=_metrics(),
        settings=settings, registry=reg, walacor_writer=writer,
        approver="dr.stakeholder@walacor.com",
    )

    assert result.promotion_event.payload["approver"] == "dr.stakeholder@walacor.com"
