"""Phase 25 Task 18: IntentTrainer tests.

Exercises the real sklearn pipeline fit + validation; skl2onnx is not a
test-env dependency, so `_to_onnx` is stubbed to produce deterministic
bytes and the file-emission path is asserted end-to-end.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.intelligence.distillation.trainers.base import TrainingError
from gateway.intelligence.distillation.trainers.intent_trainer import IntentTrainer


class _StubTrainer(IntentTrainer):
    """Bypass skl2onnx — write deterministic bytes so we can assert the path."""

    def _to_onnx(self, pipeline, X_sample):
        return b"FAKE_ONNX_BYTES"


def test_train_writes_candidate_and_calibration(tmp_path):
    trainer = _StubTrainer()
    X = ["please search for the news", "tell me about quantum", "look up current weather"]
    y = ["web_search", "normal", "web_search"]

    path = trainer.train(X, y, version="v1", candidates_dir=tmp_path)

    assert path == tmp_path / "intent-v1.onnx"
    assert path.read_bytes() == b"FAKE_ONNX_BYTES"

    calib = json.loads((tmp_path / "intent-v1-calibration.json").read_text())
    assert calib["model_name"] == "intent"
    assert calib["version"] == "v1"
    assert calib["total_samples"] == 3
    assert calib["class_counts"] == {"normal": 1, "web_search": 2}
    # Priors sum to 1 (within fp tolerance).
    assert abs(sum(calib["class_priors"].values()) - 1.0) < 1e-6


def test_train_creates_candidates_dir(tmp_path):
    trainer = _StubTrainer()
    nested = tmp_path / "nested" / "candidates"
    assert not nested.exists()
    trainer.train(["a", "b"], ["x", "y"], "v2", nested)
    assert nested.is_dir()
    assert (nested / "intent-v2.onnx").exists()


def test_train_raises_on_empty_dataset(tmp_path):
    trainer = _StubTrainer()
    with pytest.raises(TrainingError, match="empty"):
        trainer.train([], [], "v1", tmp_path)


def test_train_raises_on_xy_length_mismatch(tmp_path):
    trainer = _StubTrainer()
    with pytest.raises(TrainingError, match="length mismatch"):
        trainer.train(["a", "b"], ["x"], "v1", tmp_path)


def test_train_raises_on_single_class(tmp_path):
    trainer = _StubTrainer()
    with pytest.raises(TrainingError, match="at least 2 classes"):
        trainer.train(["a", "b", "c"], ["x", "x", "x"], "v1", tmp_path)


def test_train_rejects_path_traversal_version(tmp_path):
    trainer = _StubTrainer()
    with pytest.raises(TrainingError, match="invalid version"):
        trainer.train(["a", "b"], ["x", "y"], "../evil", tmp_path)
    with pytest.raises(TrainingError, match="invalid version"):
        trainer.train(["a", "b"], ["x", "y"], "v1/escape", tmp_path)


def test_train_fits_real_sklearn_pipeline(tmp_path):
    # Smoke test the real sklearn fit — confirms our topology + params
    # actually work on a tiny balanced dataset. ONNX is still stubbed.
    trainer = _StubTrainer()
    X = [
        "search for the latest iPhone news",
        "find online reviews of this car",
        "look up today's weather",
        "tell me a joke about cats",
        "what's the meaning of life",
        "how do birds fly",
    ]
    y = ["web_search", "web_search", "web_search", "normal", "normal", "normal"]
    trainer.train(X, y, version="v1", candidates_dir=tmp_path)

    # Bytes written — good enough. We don't run inference here; that's
    # the shadow validator's job (Phase F).
    assert (tmp_path / "intent-v1.onnx").read_bytes() == b"FAKE_ONNX_BYTES"


def test_model_name_constant():
    assert IntentTrainer.model_name == "intent"
