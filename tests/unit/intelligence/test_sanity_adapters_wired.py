"""SanityAdapter wiring tests for safety + schema_mapper.

After the production-shape refactor:

  * the trainers emit a candidate ONNX shape-compatible with the
    packaged production model (`(None, 200)` for safety, `(None, 139)`
    for schema_mapper, int64 label output for both),
  * featurization at sanity time uses the SAME packaged production
    sidecars / functions production runs at serve time — NOT a
    trainer-emitted vocab/idf/dictvec,
  * trainer side-cars are now `labels.json` + `featurizer_ref.json`
    only; their absence still surfaces as a sanity FAILURE
    (block promotion).

The fixtures here use the real trainer to build a candidate so the
test exercises the full trainer→adapter contract. Skipping skl2onnx
isn't tenable any more — without a real conversion the candidate
file isn't a valid ONNX and ORT can't load it.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

# Skip the whole module when sklearn / skl2onnx / onnxruntime aren't
# available (matches the trainer's own ImportError-as-TrainingError
# guard).
pytest.importorskip("sklearn")
pytest.importorskip("skl2onnx")
pytest.importorskip("onnxruntime")

from gateway.intelligence import sanity_adapters
from gateway.intelligence.distillation.trainers.safety_trainer import SafetyTrainer
from gateway.intelligence.distillation.trainers.schema_trainer import SchemaMapperTrainer
from gateway.intelligence.sanity_runner import SanityRunner


# ── shared helpers ────────────────────────────────────────────────────────


_SAFETY_X = [
    "kill bomb attack violent",
    "shoot stab violent maim",
    "kill weapon attack",
    "shoot bomb violent",
    "happy flowers nice poem safe",
    "safe text response calm",
    "happy response calm safe",
    "nice flowers calm safe",
]
_SAFETY_Y = ["violence", "violence", "violence", "violence",
             "safe", "safe", "safe", "safe"]


def _train_safety_candidate(candidates_dir: Path, version: str = "v1") -> Path:
    candidates_dir.mkdir(parents=True, exist_ok=True)
    return SafetyTrainer().train(_SAFETY_X, _SAFETY_Y, version=version,
                                  candidates_dir=candidates_dir)


_SCHEMA_X = [
    json.dumps({"choices": [{"message": {"content": "Hello, this is a long response with many words."}}]}),
    json.dumps({"choices": [{"message": {"content": "Another long response containing natural language tokens."}}]}),
    json.dumps({"choices": [{"message": {"content": "Yet a third example response with many words."}}]}),
    json.dumps({"usage": {"prompt_tokens": 42}}),
    json.dumps({"usage": {"prompt_tokens": 89}}),
    json.dumps({"usage": {"prompt_tokens": 17}}),
]
_SCHEMA_Y = ["content", "content", "content",
             "prompt_tokens", "prompt_tokens", "prompt_tokens"]


def _train_schema_candidate(candidates_dir: Path, version: str = "v1") -> Path:
    candidates_dir.mkdir(parents=True, exist_ok=True)
    return SchemaMapperTrainer().train(_SCHEMA_X, _SCHEMA_Y, version=version,
                                        candidates_dir=candidates_dir)


def _sidecar(candidates_dir: Path, model: str, version: str, suffix: str) -> Path:
    return candidates_dir / f"{model}-{version}.{suffix}"


# ── safety adapter ────────────────────────────────────────────────────────


def test_safety_adapter_loads_sidecars_and_returns_labels(tmp_path):
    """Happy path — trained candidate → adapter → label string back."""
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_safety_candidate(candidates_dir, version="v1")
    labels = json.loads((candidates_dir / "safety-v1.labels.json").read_text())

    infer = sanity_adapters.build_infer_fn("safety", candidate_path)
    out = infer("kill bomb attack violent")
    # The adapter must return one of the production label strings —
    # decoded from the int64 ONNX output via the trainer's labels.json.
    assert out in labels


def test_safety_adapter_blocks_when_labels_sidecar_missing(tmp_path):
    """A candidate emitted by a stale trainer with no labels.json must block."""
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_safety_candidate(candidates_dir, version="v1")
    # Delete labels.json after training to simulate stale output.
    (candidates_dir / "safety-v1.labels.json").unlink()

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("safety", candidate_path)
    msg = str(exc.value)
    assert "labels.json" in msg
    assert "safety" in msg


def test_safety_adapter_blocks_when_featurizer_ref_missing(tmp_path):
    """featurizer_ref.json absence is a sanity contract failure too."""
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_safety_candidate(candidates_dir, version="v1")
    (candidates_dir / "safety-v1.featurizer_ref.json").unlink()

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("safety", candidate_path)
    assert "featurizer_ref.json" in str(exc.value)


def test_safety_adapter_uses_production_featurizer_dim(tmp_path):
    """Featurization shape is `(None, 200)` — matching production SVD output."""
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_safety_candidate(candidates_dir, version="v1")

    # Hook into the adapter at construction to confirm featurization
    # produces the expected dimension. We do this by reaching into
    # the trainer's helpers directly — the same code path the
    # adapter runs.
    from gateway.intelligence.distillation.trainers.safety_trainer import (
        _load_production_featurizer, featurize_batch,
    )
    feat = _load_production_featurizer()
    matrix = featurize_batch(["kill bomb"], feat)
    assert matrix.shape == (1, 200)
    assert matrix.dtype == np.float32

    # And the adapter actually runs through to produce a label.
    infer = sanity_adapters.build_infer_fn("safety", candidate_path)
    out = infer("kill bomb attack violent")
    assert isinstance(out, str)


def test_safety_trainer_writes_all_sidecars(tmp_path):
    """Drive the trainer end-to-end and verify both side-cars land."""
    candidate_path = _train_safety_candidate(tmp_path, version="v3")
    assert candidate_path.exists()

    # New side-cars present.
    assert (tmp_path / "safety-v3.labels.json").exists()
    assert (tmp_path / "safety-v3.featurizer_ref.json").exists()

    # Old-style side-cars MUST NOT be emitted.
    assert not (tmp_path / "safety-v3.vocab.json").exists()
    assert not (tmp_path / "safety-v3.idf.npy").exists()

    # Labels file is a list of strings drawn from the production set.
    labels = json.loads((tmp_path / "safety-v3.labels.json").read_text())
    prod_labels = json.loads(
        (Path("src/gateway/content/safety_classifier_labels.json")).read_text()
    )
    assert labels == prod_labels

    ref = json.loads((tmp_path / "safety-v3.featurizer_ref.json").read_text())
    assert ref["expected_input_dim"] == 200


# ── schema_mapper adapter ─────────────────────────────────────────────────


def test_schema_mapper_adapter_loads_sidecars_and_returns_labels(tmp_path):
    """Happy path — trained candidate → adapter → label string back."""
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_schema_candidate(candidates_dir, version="v1")
    labels = json.loads((candidates_dir / "schema_mapper-v1.labels.json").read_text())

    infer = sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    # FlatField-shaped row — direct featurization path.
    out = infer({
        "path": "choices.0.message.content", "key": "content",
        "value": "long natural language content with many words to classify",
        "value_type": "string", "depth": 3, "parent_key": "message",
        "sibling_keys": ["role", "content"], "sibling_types": ["string", "string"],
        "int_siblings": [],
    })
    assert out in labels


def test_schema_mapper_adapter_blocks_when_labels_sidecar_missing(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_schema_candidate(candidates_dir, version="v1")
    (candidates_dir / "schema_mapper-v1.labels.json").unlink()

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    assert "labels.json" in str(exc.value)


def test_schema_mapper_adapter_blocks_when_featurizer_ref_missing(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_schema_candidate(candidates_dir, version="v1")
    (candidates_dir / "schema_mapper-v1.featurizer_ref.json").unlink()

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    assert "featurizer_ref.json" in str(exc.value)


def test_schema_mapper_adapter_handles_raw_response_input(tmp_path):
    """Adapter accepts raw response JSON (flatten + extract_features path)."""
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_schema_candidate(candidates_dir, version="v1")
    labels = json.loads((candidates_dir / "schema_mapper-v1.labels.json").read_text())

    infer = sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    # Raw response — adapter calls flatten_json + extract_features.
    out = infer({"choices": [{"message": {"content": "hello world"}}]})
    assert out in labels


def test_schema_mapper_adapter_handles_empty_input(tmp_path):
    """Empty / un-coercible inputs become zero-vectors and still classify."""
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_schema_candidate(candidates_dir, version="v1")
    labels = json.loads((candidates_dir / "schema_mapper-v1.labels.json").read_text())

    infer = sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    assert infer({}) in labels
    assert infer("not a json") in labels


def test_schema_mapper_trainer_writes_all_sidecars(tmp_path):
    """Drive the trainer end-to-end and verify both side-cars land."""
    candidate_path = _train_schema_candidate(tmp_path, version="v7")
    assert candidate_path.exists()

    assert (tmp_path / "schema_mapper-v7.labels.json").exists()
    assert (tmp_path / "schema_mapper-v7.featurizer_ref.json").exists()
    # Old-style side-cars must not be emitted.
    assert not (tmp_path / "schema_mapper-v7.dictvec.pkl").exists()
    assert not (tmp_path / "schema_mapper-v7.feature_names.json").exists()

    labels = json.loads((tmp_path / "schema_mapper-v7.labels.json").read_text())
    prod_labels = json.loads(
        (Path("src/gateway/schema/schema_mapper_labels.json")).read_text()
    )
    assert labels == prod_labels

    ref = json.loads((tmp_path / "schema_mapper-v7.featurizer_ref.json").read_text())
    assert ref["expected_input_dim"] == 139


# ── strategy table contract ───────────────────────────────────────────────


def test_wired_models_contains_all_three():
    """Backward-compat invariant: previously-deferred models now wired."""
    assert sanity_adapters.WIRED_MODELS == frozenset(
        {"intent", "safety", "schema_mapper"}
    )
    assert sanity_adapters.is_wired("intent") is True
    assert sanity_adapters.is_wired("safety") is True
    assert sanity_adapters.is_wired("schema_mapper") is True
    assert sanity_adapters.is_wired("not_a_model") is False


# ── SanityRunner end-to-end (adapter + runner together) ──────────────────


def test_sanity_runner_blocks_when_adapter_predicts_below_floor(tmp_path):
    """Adapter→runner integration. Force a misclassification and verify the
    per-class accuracy floor catches it.

    Build a schema_mapper candidate, then author a fixture whose
    expected labels include one the candidate was never trained on.
    The runner should fail the per-class floor for that label while
    leaving the others intact.
    """
    candidates_dir = tmp_path / "candidates"
    candidate_path = _train_schema_candidate(candidates_dir, version="v1")

    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "schema_mapper_sanity.json").write_text(json.dumps({
        "model_name": "schema_mapper",
        "examples": [
            # Big-content rows the candidate was trained to call "content".
            {"input": {
                "path": "choices.0.message.content", "key": "content",
                "value": "long natural language content",
                "value_type": "string", "depth": 3, "parent_key": "message",
                "sibling_keys": ["role", "content"],
                "sibling_types": ["string", "string"],
                "int_siblings": [],
             }, "label": "content"},
            {"input": {
                "path": "choices.0.message.content", "key": "content",
                "value": "another natural language response with many words",
                "value_type": "string", "depth": 3, "parent_key": "message",
                "sibling_keys": ["role", "content"],
                "sibling_types": ["string", "string"],
                "int_siblings": [],
             }, "label": "content"},
            # Force a label that won't match the candidate's predictions.
            {"input": {}, "label": "definitely_wrong"},
            {"input": {}, "label": "definitely_wrong"},
        ],
    }))

    infer = sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    runner = SanityRunner(fixtures_dir=fixtures_dir)
    result = runner.run("schema_mapper", infer, min_per_class_accuracy=0.7)
    assert result.passed is False
    assert "definitely_wrong" in result.failing_classes
