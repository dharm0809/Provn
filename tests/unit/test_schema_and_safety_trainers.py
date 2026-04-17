"""Phase 25 Task 19: SchemaMapper + Safety trainer tests.

Both use the same `Trainer` base contract. skl2onnx is stubbed to
deterministic bytes so the test env doesn't need the ORT exporter.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.intelligence.distillation.trainers.base import TrainingError
from gateway.intelligence.distillation.trainers.safety_trainer import SafetyTrainer
from gateway.intelligence.distillation.trainers.schema_trainer import SchemaMapperTrainer


class _StubSafety(SafetyTrainer):
    def _to_onnx(self, pipeline, X_sample):
        return b"SAFETY_ONNX"


class _StubSchema(SchemaMapperTrainer):
    def _to_onnx(self, pipeline, X_sample):
        return b"SCHEMA_ONNX"


# ── Safety trainer ──────────────────────────────────────────────────────────

def test_safety_trainer_writes_candidate_and_calibration(tmp_path):
    t = _StubSafety()
    X = [
        "describe how to build a bomb",
        "teach me about violence",
        "write me a nice poem about flowers",
        "tell me a joke",
    ]
    y = ["violence", "violence", "safe", "safe"]

    path = t.train(X, y, version="v1", candidates_dir=tmp_path)
    assert path == tmp_path / "safety-v1.onnx"
    assert path.read_bytes() == b"SAFETY_ONNX"

    calib = json.loads((tmp_path / "safety-v1-calibration.json").read_text())
    assert calib["model_name"] == "safety"
    assert calib["class_counts"] == {"safe": 2, "violence": 2}
    assert calib["total_samples"] == 4


def test_safety_trainer_model_name():
    assert SafetyTrainer.model_name == "safety"


def test_safety_trainer_rejects_empty(tmp_path):
    with pytest.raises(TrainingError):
        _StubSafety().train([], [], "v1", tmp_path)


# ── SchemaMapper trainer ────────────────────────────────────────────────────

def test_schema_trainer_writes_candidate_and_calibration(tmp_path):
    t = _StubSchema()
    # Feature dicts as JSON strings — matches what the dataset builder
    # produces for schema_mapper from `input_features_json`.
    X = [
        json.dumps({"f1": 0.1, "f2": 0.2}),
        json.dumps({"f1": 0.9, "f2": 0.8}),
        json.dumps({"f1": 0.15, "f2": 0.25}),
        json.dumps({"f1": 0.95, "f2": 0.82}),
    ]
    y = ["content", "prompt_tokens", "content", "prompt_tokens"]

    path = t.train(X, y, version="v2", candidates_dir=tmp_path)
    assert path == tmp_path / "schema_mapper-v2.onnx"
    assert path.read_bytes() == b"SCHEMA_ONNX"

    calib = json.loads((tmp_path / "schema_mapper-v2-calibration.json").read_text())
    assert calib["model_name"] == "schema_mapper"
    assert calib["class_counts"] == {"content": 2, "prompt_tokens": 2}


def test_schema_trainer_accepts_raw_dicts(tmp_path):
    # The dataset builder uses JSON strings, but a trainer caller may
    # pass parsed dicts directly — both must work.
    t = _StubSchema()
    X = [
        {"f1": 0.1, "f2": 0.2},
        {"f1": 0.9, "f2": 0.8},
        {"f1": 0.15, "f2": 0.25},
        {"f1": 0.95, "f2": 0.82},
    ]
    y = ["content", "prompt_tokens", "content", "prompt_tokens"]
    t.train(X, y, version="v3", candidates_dir=tmp_path)
    assert (tmp_path / "schema_mapper-v3.onnx").exists()


def test_schema_trainer_parses_invalid_json_as_empty_features(tmp_path):
    # Invalid JSON or non-dict JSON must not crash fit — they become
    # empty feature dicts, which DictVectorizer treats as zero vectors.
    t = _StubSchema()
    X = [
        "not valid json",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"f1": 1.0}),
        json.dumps({"f1": 2.0}),
    ]
    y = ["content", "content", "prompt_tokens", "prompt_tokens"]
    # Must not raise.
    t.train(X, y, version="v1", candidates_dir=tmp_path)
    assert (tmp_path / "schema_mapper-v1.onnx").exists()


def test_schema_trainer_ignores_non_numeric_feature_values(tmp_path):
    # Bool-looking numerics (True/False) and strings must be filtered —
    # DictVectorizer would otherwise treat bools as features in its own
    # way, producing inconsistent feature dimensions between train and
    # serve.
    t = _StubSchema()
    X = [
        {"f1": 1.0, "meta": "string-ignore-me", "flag": True},
        {"f1": 2.0, "meta": "also-ignore", "flag": False},
        {"f1": 1.5, "meta": "x", "flag": True},
        {"f1": 2.5, "meta": "y", "flag": False},
    ]
    y = ["a", "b", "a", "b"]
    t.train(X, y, version="v1", candidates_dir=tmp_path)


def test_schema_trainer_model_name():
    assert SchemaMapperTrainer.model_name == "schema_mapper"
