"""Verify orchestrator._emit_harvester_signals dispatches one signal per model.

These tests don't run the full pipeline — they directly exercise the
dispatch helper with a synthetic record dict. The intent is to lock in
the contract so a future refactor that quietly drops one of the three
submit branches gets caught.

Findings from the harvester-wiring trace done as part of Task 2.3:
  - intent submits when meta._intent is non-empty
  - schema_mapper submits when meta.canonical is a dict
  - safety submits when meta.analyzer_decisions has truzenai.safety.v1

If this test starts failing, do NOT widen the conditions to make it
pass — re-trace the source pipeline and confirm the call site is
still firing in production.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_runner():
    runner = MagicMock()
    runner.submit = MagicMock()
    return runner


@pytest.fixture
def fake_ctx(fake_runner, monkeypatch):
    ctx = SimpleNamespace(harvester_runner=fake_runner)
    from gateway.pipeline import orchestrator as orch
    monkeypatch.setattr(orch, "get_pipeline_context", lambda: ctx, raising=True)
    return ctx


def test_emit_signals_intent_only(fake_runner, fake_ctx):
    from gateway.pipeline.orchestrator import _emit_harvester_signals

    record = {
        "metadata": {"_intent": "rag"},
        "prompt_text": "what's in the doc?",
        "response_content": "answer",
    }
    _emit_harvester_signals(record, session_id="s1")
    submitted = [c.args[0] for c in fake_runner.submit.call_args_list]
    models = sorted(s.model_name for s in submitted)
    assert models == ["intent"]
    assert submitted[0].prediction == "rag"


def test_emit_signals_schema_mapper(fake_runner, fake_ctx):
    from gateway.pipeline.orchestrator import _emit_harvester_signals

    record = {
        "metadata": {"canonical": {"content_length": 42}},
        "prompt_text": "p",
        "response_content": "r",
    }
    _emit_harvester_signals(record, session_id="s1")
    submitted = [c.args[0] for c in fake_runner.submit.call_args_list]
    sm = [s for s in submitted if s.model_name == "schema_mapper"]
    assert len(sm) == 1
    assert sm[0].prediction == "complete"


def test_emit_signals_safety(fake_runner, fake_ctx):
    from gateway.pipeline.orchestrator import _emit_harvester_signals

    record = {
        "metadata": {
            "analyzer_decisions": [
                {"analyzer_id": "presidio.pii.v1", "category": "pii"},
                {"analyzer_id": "truzenai.safety.v1", "category": "violence"},
            ],
        },
        "prompt_text": "p", "response_content": "r",
    }
    _emit_harvester_signals(record, session_id="s1")
    submitted = [c.args[0] for c in fake_runner.submit.call_args_list]
    sf = [s for s in submitted if s.model_name == "safety"]
    assert len(sf) == 1
    assert sf[0].prediction == "violence"


def test_emit_signals_all_three_when_record_complete(fake_runner, fake_ctx):
    """The integration-shaped record fires one signal per model."""
    from gateway.pipeline.orchestrator import _emit_harvester_signals

    record = {
        "metadata": {
            "_intent": "web_search",
            "canonical": {"content_length": 10},
            "analyzer_decisions": [
                {"analyzer_id": "truzenai.safety.v1", "category": "safe"},
            ],
        },
        "prompt_text": "p", "response_content": "r",
    }
    _emit_harvester_signals(record, session_id="s1")
    submitted = [c.args[0] for c in fake_runner.submit.call_args_list]
    assert sorted(s.model_name for s in submitted) == ["intent", "safety", "schema_mapper"]


def test_emit_signals_silent_when_no_runner(monkeypatch):
    from gateway.pipeline import orchestrator as orch
    from gateway.pipeline.orchestrator import _emit_harvester_signals

    ctx = SimpleNamespace(harvester_runner=None)
    monkeypatch.setattr(orch, "get_pipeline_context", lambda: ctx, raising=True)
    # Should not raise.
    _emit_harvester_signals({"metadata": {"_intent": "x"}}, session_id="s1")


def test_drift_monitor_updates_coverage_gauge(tmp_path):
    """check_once writes coverage to walacor_gateway_intelligence_signal_coverage_ratio."""
    from datetime import datetime, timedelta, timezone

    from gateway.intelligence.db import IntelligenceDB
    from gateway.intelligence.drift_monitor import DriftMonitor
    from gateway.metrics.prometheus import intelligence_signal_coverage_ratio

    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    now = datetime.now(timezone.utc)
    # 100 rows total, 60 with signal → coverage 0.60
    with db._connect() as conn:
        for i in range(100):
            sig = "A" if i < 60 else None
            conn.execute(
                "INSERT INTO onnx_verdicts "
                "(model_name, input_hash, input_features_json, prediction, "
                " confidence, request_id, timestamp, divergence_signal, divergence_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("intent", "h", "{}", "A", 0.9, None, now.isoformat(),
                 sig, "test"),
            )

    monitor = DriftMonitor(db, models=["intent"], min_samples=1, min_coverage=0.0,
                           clock=lambda: now + timedelta(seconds=1))
    import asyncio
    asyncio.run(monitor.check_once())

    coverage = intelligence_signal_coverage_ratio.labels(model="intent")._value.get()
    assert 0.55 < coverage < 0.65
