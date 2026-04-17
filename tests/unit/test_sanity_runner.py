"""Phase 25 Task 25: SanityRunner tests.

Exercises fixture loading, the per-class accuracy gate, and the
missing-fixture conservative-reject path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.intelligence.sanity_runner import SanityRunner


def _write_fixture(tmp_path: Path, model: str, examples: list[dict]) -> Path:
    path = tmp_path / f"{model}_sanity.json"
    path.write_text(json.dumps({"model_name": model, "examples": examples}))
    return path


# ── load ───────────────────────────────────────────────────────────────────

def test_load_returns_examples_from_fixture(tmp_path):
    _write_fixture(tmp_path, "intent", [
        {"input": "a", "label": "normal"},
        {"input": "b", "label": "web_search"},
    ])
    runner = SanityRunner(fixtures_dir=tmp_path)
    rows = runner.load("intent")
    assert len(rows) == 2
    assert rows[0]["input"] == "a"


def test_load_missing_fixture_returns_empty(tmp_path):
    runner = SanityRunner(fixtures_dir=tmp_path)
    assert runner.load("intent") == []


def test_load_skips_malformed_examples(tmp_path):
    _write_fixture(tmp_path, "intent", [
        {"input": "ok", "label": "normal"},
        {"input_only": "no_label"},  # missing label → dropped
        "not a dict",
        {"label_only": "web_search"},  # missing input → dropped
    ])
    runner = SanityRunner(fixtures_dir=tmp_path)
    assert len(runner.load("intent")) == 1


def test_load_tolerates_unreadable_file(tmp_path):
    # Invalid JSON — the runner must return [] instead of raising so
    # the gate correctly rejects promotion for a broken fixture.
    (tmp_path / "intent_sanity.json").write_text("{not-json}")
    runner = SanityRunner(fixtures_dir=tmp_path)
    assert runner.load("intent") == []


# ── run ────────────────────────────────────────────────────────────────────

def test_run_accepts_when_every_class_meets_floor(tmp_path):
    _write_fixture(tmp_path, "intent", [
        {"input": "a", "label": "normal"},
        {"input": "b", "label": "normal"},
        {"input": "c", "label": "web_search"},
        {"input": "d", "label": "web_search"},
    ])
    runner = SanityRunner(fixtures_dir=tmp_path)
    # Perfect candidate — predicts the label verbatim.
    result = runner.run("intent", lambda ex: ex, min_per_class_accuracy=0.7)
    # Wait — the fixture has `input` strings, NOT labels. Use a real inferrer.

    # Redo with a correct inferrer.
    label_by_input = {"a": "normal", "b": "normal", "c": "web_search", "d": "web_search"}
    result = runner.run("intent", lambda ex: label_by_input[ex], min_per_class_accuracy=0.7)

    assert result.passed is True
    assert result.overall_accuracy == 1.0
    assert result.failing_classes == []
    assert result.per_class_accuracy == {"normal": 1.0, "web_search": 1.0}
    assert result.total_examples == 4


def test_run_rejects_when_any_class_below_floor(tmp_path):
    _write_fixture(tmp_path, "intent", [
        # 5 web_search examples — inferrer gets 4/5 right (80%) → above floor.
        {"input": "a", "label": "web_search"},
        {"input": "b", "label": "web_search"},
        {"input": "c", "label": "web_search"},
        {"input": "d", "label": "web_search"},
        {"input": "e", "label": "web_search"},
        # 5 normal examples — inferrer gets 2/5 right (40%) → below floor.
        {"input": "f", "label": "normal"},
        {"input": "g", "label": "normal"},
        {"input": "h", "label": "normal"},
        {"input": "i", "label": "normal"},
        {"input": "j", "label": "normal"},
    ])
    correct_for = {"a", "b", "c", "d", "f", "g"}  # web_search 4/5; normal 2/5
    def _infer(ex):
        return "web_search" if ex in correct_for and ex in {"a", "b", "c", "d"} else "normal" if ex in {"f", "g"} else "web_search"

    runner = SanityRunner(fixtures_dir=tmp_path)
    result = runner.run("intent", _infer, min_per_class_accuracy=0.7)

    assert result.passed is False
    assert "normal" in result.failing_classes
    assert "web_search" not in result.failing_classes


def test_run_captures_inference_errors_without_raising(tmp_path):
    _write_fixture(tmp_path, "intent", [
        {"input": "good", "label": "normal"},
        {"input": "boom", "label": "normal"},
    ])
    def _infer(ex):
        if ex == "boom":
            raise RuntimeError("candidate crashed")
        return "normal"

    runner = SanityRunner(fixtures_dir=tmp_path)
    result = runner.run("intent", _infer, min_per_class_accuracy=0.7)
    assert result.error_count == 1
    # 1 correct / 2 → 50% for `normal`, below 70% floor.
    assert result.passed is False
    assert "normal" in result.failing_classes


def test_run_missing_fixture_is_conservative_reject(tmp_path):
    # Empty fixtures dir — runner returns `passed=False` so the gate
    # refuses to promote a candidate whose sanity set isn't authored.
    runner = SanityRunner(fixtures_dir=tmp_path)
    result = runner.run("intent", lambda ex: "x")
    assert result.passed is False
    assert result.failing_classes == ["<no fixture>"]
    assert result.total_examples == 0


def test_run_min_accuracy_threshold_is_respected(tmp_path):
    # 3 of 4 correct on one class (75%). Floor 0.7 → pass. Floor 0.8 → fail.
    _write_fixture(tmp_path, "intent", [
        {"input": str(i), "label": "normal"} for i in range(4)
    ])
    def _infer(ex):
        return "normal" if ex in {"0", "1", "2"} else "web_search"

    runner = SanityRunner(fixtures_dir=tmp_path)
    pass_result = runner.run("intent", _infer, min_per_class_accuracy=0.7)
    fail_result = runner.run("intent", _infer, min_per_class_accuracy=0.8)
    assert pass_result.passed is True
    assert fail_result.passed is False


# ── packaged fixtures ───────────────────────────────────────────────────────

def test_packaged_intent_fixture_parses():
    runner = SanityRunner()  # uses the default packaged dir
    rows = runner.load("intent")
    assert len(rows) >= 12  # at least seed size
    labels = {r["label"] for r in rows}
    # Every canonical intent label has at least one example.
    assert labels >= {
        "normal", "web_search", "rag", "mcp_tools", "reasoning", "system_task",
    }


def test_packaged_safety_fixture_parses():
    runner = SanityRunner()
    rows = runner.load("safety")
    labels = {r["label"] for r in rows}
    assert labels >= {
        "safe", "violence", "sexual", "criminal",
        "self_harm", "hate_speech", "dangerous", "child_safety",
    }


def test_packaged_schema_fixture_parses():
    runner = SanityRunner()
    rows = runner.load("schema_mapper")
    assert rows  # non-empty
    # schema inputs are feature dicts, not strings.
    assert all(isinstance(r["input"], dict) for r in rows)
