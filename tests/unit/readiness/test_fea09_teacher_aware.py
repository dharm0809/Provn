"""FEA-09 (intelligence signal coverage) is teacher-aware.

A model whose harvester has no active teacher pipeline (e.g. SafetyHarvester
on a deployment without LlamaGuard) is **inference-only** on this gateway —
no teacher source is wired, so no `divergence_signal` rows will ever land
in the verdict log no matter how many predictions accumulate. The pre-fix
check called this "low coverage" and red-lit the model, which kept the
readiness rollup DEGRADED on perfectly healthy default-config deployments.

The fix: FEA-09 consults each registered harvester's `is_teacher_active()`
and skips models whose teacher pipeline isn't running. Those models are
reported in evidence as `teacher_inactive` so an operator can SEE that
the model is inference-only — but they don't contribute to the unhealthy
total.
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timezone

import pytest


def _run(coro):
    return asyncio.run(coro)


def _settings(**kw):
    defaults = dict(intelligence_enabled=True)
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


class _StubDB:
    """Returns whatever AccuracySnapshot you parameterize per-model."""

    def __init__(self, snapshots: dict[str, "_Snap"]):
        self._snapshots = snapshots

    def accuracy_in_window(self, model, *, start, end):
        # Defaults to "zero coverage on lots of rows" when not specified.
        return self._snapshots.get(model, _Snap(total_rows=10_000, coverage=0.0))


class _Snap:
    def __init__(self, total_rows: int, coverage: float):
        self.total_rows = total_rows
        self.coverage = coverage


class _StubHarvester:
    def __init__(self, target_model: str, teacher_active: bool):
        self.target_model = target_model
        self._teacher_active = teacher_active

    def is_teacher_active(self) -> bool:
        return self._teacher_active

    async def process(self, signal):  # pragma: no cover - never invoked here
        pass


class _StubRunner:
    def __init__(self, harvesters):
        self._harvesters = harvesters

    @property
    def harvesters(self):
        return list(self._harvesters)


def _ctx(**kw):
    defaults = dict(intelligence_db=None, harvester_runner=None)
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def test_fea09_green_when_only_teacher_inactive_models_have_low_coverage(monkeypatch):
    """Safety model has 0% coverage but no teacher → out of scope → green."""
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings", lambda: _settings(),
    )
    monkeypatch.setattr(
        "gateway.intelligence.registry.ALLOWED_MODEL_NAMES",
        {"intent", "safety", "schema_mapper"},
    )

    db = _StubDB({
        "intent":        _Snap(total_rows=10_000, coverage=0.5),  # healthy
        "safety":        _Snap(total_rows=2_000,  coverage=0.0),  # 0 — but no teacher
        "schema_mapper": _Snap(total_rows=15_000, coverage=0.5),  # healthy
    })
    runner = _StubRunner([
        _StubHarvester("intent", teacher_active=True),
        _StubHarvester("safety", teacher_active=False),   # ← the key bit
        _StubHarvester("schema_mapper", teacher_active=True),
    ])
    ctx = _ctx(intelligence_db=db, harvester_runner=runner)

    from gateway.readiness.checks.features import _Fea09SignalCoverage
    result = _run(_Fea09SignalCoverage().run(ctx))

    assert result.status == "green"
    assert "safety" in (result.evidence or {}).get("teacher_inactive", [])
    assert "inference-only" in result.detail


def test_fea09_red_when_teacher_active_model_has_low_coverage(monkeypatch):
    """Intent has teacher but coverage stuck at 0 → real problem → red."""
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings", lambda: _settings(),
    )
    monkeypatch.setattr(
        "gateway.intelligence.registry.ALLOWED_MODEL_NAMES",
        {"intent", "safety"},
    )

    db = _StubDB({
        "intent": _Snap(total_rows=10_000, coverage=0.0),
        "safety": _Snap(total_rows=2_000,  coverage=0.0),
    })
    runner = _StubRunner([
        _StubHarvester("intent", teacher_active=True),
        _StubHarvester("safety", teacher_active=False),
    ])
    ctx = _ctx(intelligence_db=db, harvester_runner=runner)

    from gateway.readiness.checks.features import _Fea09SignalCoverage
    result = _run(_Fea09SignalCoverage().run(ctx))

    assert result.status == "red"
    unhealthy = [m["model"] for m in result.evidence["unhealthy"]]
    assert "intent" in unhealthy
    assert "safety" not in unhealthy  # excluded by teacher-inactive gate


def test_fea09_falls_back_to_all_models_when_runner_unavailable(monkeypatch):
    """No runner on ctx → score every ALLOWED_MODEL_NAMES.

    This is the fail-open path: if the runner shape changed or hasn't
    been set yet, FEA-09 must not silently start ignoring all models.
    """
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings", lambda: _settings(),
    )
    monkeypatch.setattr(
        "gateway.intelligence.registry.ALLOWED_MODEL_NAMES",
        {"intent", "safety"},
    )

    db = _StubDB({
        "intent": _Snap(total_rows=10_000, coverage=0.0),
        "safety": _Snap(total_rows=2_000,  coverage=0.0),
    })
    ctx = _ctx(intelligence_db=db, harvester_runner=None)

    from gateway.readiness.checks.features import _Fea09SignalCoverage
    result = _run(_Fea09SignalCoverage().run(ctx))
    assert result.status == "red"
    assert {m["model"] for m in result.evidence["unhealthy"]} == {"intent", "safety"}
