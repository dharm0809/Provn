"""Each harvester reports honestly whether its teacher pipeline is active.

Read by FEA-09 to distinguish "model is inference-only on this deployment"
from "teacher is broken." See `tests/unit/readiness/test_fea09_teacher_aware.py`
for the readiness-check half of this story.
"""
from __future__ import annotations

import types

import pytest


def _stub_db():
    return types.SimpleNamespace(path="/tmp/unused-db")


# ── IntentHarvester ─────────────────────────────────────────────────────


def test_intent_teacher_inactive_when_url_empty():
    from gateway.intelligence.harvesters.intent import IntentHarvester

    h = IntentHarvester(_stub_db(), teacher_url="", teacher_sample_rate=0.5)
    assert h.is_teacher_active() is False


def test_intent_teacher_inactive_when_no_http_client():
    from gateway.intelligence.harvesters.intent import IntentHarvester

    h = IntentHarvester(
        _stub_db(),
        teacher_url="https://judge.example.com/v1/chat/completions",
        teacher_sample_rate=0.5,
        http_client=None,
    )
    assert h.is_teacher_active() is False


def test_intent_teacher_inactive_when_sample_rate_zero():
    from gateway.intelligence.harvesters.intent import IntentHarvester

    class _FakeHttp:
        pass

    h = IntentHarvester(
        _stub_db(),
        teacher_url="https://judge.example.com/v1/chat/completions",
        teacher_sample_rate=0.0,
        http_client=_FakeHttp(),
    )
    assert h.is_teacher_active() is False


def test_intent_teacher_active_when_all_three_set():
    from gateway.intelligence.harvesters.intent import IntentHarvester

    class _FakeHttp:
        pass

    h = IntentHarvester(
        _stub_db(),
        teacher_url="http://localhost:8000/v1/chat/completions",
        teacher_sample_rate=0.01,
        http_client=_FakeHttp(),
    )
    assert h.is_teacher_active() is True


# ── SafetyHarvester ─────────────────────────────────────────────────────


def test_safety_teacher_inactive_when_llama_guard_not_loaded():
    from gateway.intelligence.harvesters.safety import SafetyHarvester

    h = SafetyHarvester(_stub_db(), llama_guard_loaded=False)
    assert h.is_teacher_active() is False


def test_safety_teacher_active_when_llama_guard_loaded():
    from gateway.intelligence.harvesters.safety import SafetyHarvester

    h = SafetyHarvester(_stub_db(), llama_guard_loaded=True)
    assert h.is_teacher_active() is True


# ── SchemaMapperHarvester (the default-True case) ───────────────────────


def test_schema_mapper_teacher_active_by_default():
    """SchemaMapper has a deterministic heuristic teacher built into the
    mapper itself — always active."""
    from gateway.intelligence.harvesters.schema_mapper import SchemaMapperHarvester

    h = SchemaMapperHarvester(_stub_db())
    assert h.is_teacher_active() is True
