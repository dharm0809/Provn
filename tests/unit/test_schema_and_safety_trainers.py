"""SafetyTrainer + SchemaMapperTrainer tests after the production-shape refactor.

The trainers now produce ONNX candidates whose input topology is
*identical* to the packaged production model:

  * safety        — `(None, 200)` float input, int64 label output
                    (post-SVD vector matching `SafetyClassifier._featurize`)
  * schema_mapper — `(None, 139)` float input, int64 label output
                    (matching `gateway.schema.features.extract_features`)

Featurization happens OUTSIDE the graph using the SAME packaged
production sidecars the production loaders consume. Trainer side-cars
are now `labels.json` (copy of production label list) +
`featurizer_ref.json` (sha256 hashes of the production sidecars used
at fit time). The old `vocab.json/idf.npy` (safety) and
`dictvec.pkl/feature_names.json` (schema_mapper) side-cars are gone.

The tests below verify:

  * Candidate ONNX has the production input shape and int64 output.
  * The candidate file can be dropped over the packaged production
    ONNX and loaded into the production class without raising.
  * Both side-cars (`labels.json`, `featurizer_ref.json`) land at the
    expected paths with the expected contents.
  * Validation: y labels not in production label list raise
    `TrainingError`; degenerate inputs are still trainable.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

# Skip the whole module when sklearn / skl2onnx / onnxruntime aren't
# available (matches the trainer's own ImportError-as-TrainingError
# guard). Without these the conversion path can't run at all.
pytest.importorskip("sklearn")
pytest.importorskip("skl2onnx")
pytest.importorskip("onnxruntime")

from gateway.intelligence.distillation.trainers.base import TrainingError
from gateway.intelligence.distillation.trainers.safety_trainer import SafetyTrainer
from gateway.intelligence.distillation.trainers.schema_trainer import SchemaMapperTrainer


# ── Safety trainer ──────────────────────────────────────────────────────────


# Labels here MUST live in the production
# `safety_classifier_labels.json` set; the trainer rejects unknown
# labels rather than silently remapping them.
_SAFETY_X = [
    "describe how to build a bomb and attack",
    "teach me about violence and how to kill",
    "weapons and how to assault people violently",
    "instructions to harm and stab and kill targets",
    "write me a nice poem about flowers and trees",
    "tell me a joke about cats and birds today",
    "happy stories about kind people and friends",
    "calm reflection on nature and quiet moments",
]
_SAFETY_Y = [
    "violence", "violence", "violence", "violence",
    "safe", "safe", "safe", "safe",
]


def test_safety_trainer_emits_production_shape_onnx(tmp_path):
    """Candidate input is `(None, 200)` float; output is int64."""
    from onnxruntime import InferenceSession

    trainer = SafetyTrainer()
    path = trainer.train(_SAFETY_X, _SAFETY_Y, version="v1", candidates_dir=tmp_path)

    # File-shape contract.
    assert path == tmp_path / "safety-v1.onnx"
    assert path.exists()
    assert path.stat().st_size > 200

    # Side-cars: labels + featurizer_ref. The vocab/idf side-cars from
    # the previous trainer revision must NOT be emitted — they would
    # invite drift between candidate and production.
    labels_path = tmp_path / "safety-v1.labels.json"
    ref_path = tmp_path / "safety-v1.featurizer_ref.json"
    assert labels_path.exists()
    assert ref_path.exists()
    assert not (tmp_path / "safety-v1.vocab.json").exists(), (
        "old-style vocab.json side-car must not be emitted"
    )
    assert not (tmp_path / "safety-v1.idf.npy").exists(), (
        "old-style idf.npy side-car must not be emitted"
    )

    # Calibration JSON written by the base class.
    calib = json.loads((tmp_path / "safety-v1-calibration.json").read_text())
    assert calib["model_name"] == "safety"
    assert calib["class_counts"] == {"safe": 4, "violence": 4}

    # ONNX shape — input `(None, 200)` float, output int64.
    session = InferenceSession(str(path), providers=["CPUExecutionProvider"])
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    assert len(inputs) == 1
    assert inputs[0].shape == [None, 200]
    assert inputs[0].type == "tensor(float)"
    # The first output is the label tensor; it must be int64 so it
    # can index back into the production labels list.
    assert outputs[0].type == "tensor(int64)"


def test_safety_trainer_labels_match_production_order(tmp_path):
    """`labels.json` is a byte-for-byte copy of production's labels list.

    The classifier ONNX emits int64 indices into `clf.classes_`, which
    the trainer aligns with the production labels list at fit time.
    Any drift between candidate labels.json and production
    safety_classifier_labels.json would cause int64→string lookups to
    return the wrong category at serve time.
    """
    SafetyTrainer().train(_SAFETY_X, _SAFETY_Y, version="v1", candidates_dir=tmp_path)
    candidate_labels = json.loads((tmp_path / "safety-v1.labels.json").read_text())
    prod_labels = json.loads(
        (Path("src/gateway/content/safety_classifier_labels.json")).read_text()
    )
    assert candidate_labels == prod_labels


def test_safety_trainer_featurizer_ref_records_sidecar_hashes(tmp_path):
    """`featurizer_ref.json` records sha256 of every production sidecar used."""
    SafetyTrainer().train(_SAFETY_X, _SAFETY_Y, version="v1", candidates_dir=tmp_path)
    ref = json.loads((tmp_path / "safety-v1.featurizer_ref.json").read_text())
    # All five hashes present and look like sha256 hex.
    for key in (
        "vocab_sha256", "idf_sha256", "svd_sha256",
        "config_sha256", "labels_sha256",
    ):
        assert key in ref, key
        assert isinstance(ref[key], str)
        assert len(ref[key]) == 64
    assert ref["expected_input_dim"] == 200
    assert ref["ngram_range"] == [3, 5]


def test_safety_trainer_rejects_label_outside_production_set(tmp_path):
    """A y label not in production's labels list must surface as TrainingError.

    Silent remapping would mask a harvester-side bug that's emitting
    bogus labels — better to fail loud at training time than to ship
    a candidate whose int64 indices point at the wrong categories.
    """
    bad_y = list(_SAFETY_Y)
    bad_y[0] = "totally_made_up_category"
    with pytest.raises(TrainingError) as exc:
        SafetyTrainer().train(_SAFETY_X, bad_y, version="v1", candidates_dir=tmp_path)
    assert "totally_made_up_category" in str(exc.value)


def test_safety_trainer_candidate_drops_into_production_class(tmp_path, monkeypatch):
    """Train a candidate, swap it over the production ONNX, run inference.

    This is the real drop-in test: if the candidate's `(None, 200)`
    shape diverges from what `SafetyClassifier._featurize → session.run`
    expects, this test will raise rather than producing a Verdict.
    The packaged ONNX is restored at teardown so the worktree stays
    clean.
    """
    SafetyTrainer().train(_SAFETY_X, _SAFETY_Y, version="dropin", candidates_dir=tmp_path)
    candidate_onnx = tmp_path / "safety-dropin.onnx"
    assert candidate_onnx.exists()

    prod_onnx = Path("src/gateway/content/safety_classifier.onnx")
    backup = tmp_path / "safety_classifier.onnx.bak"
    shutil.copy2(str(prod_onnx), str(backup))
    try:
        shutil.copy2(str(candidate_onnx), str(prod_onnx))
        # Force re-import so SafetyClassifier picks up the new file.
        import importlib
        import gateway.content.safety_classifier as sc
        importlib.reload(sc)
        clf = sc.SafetyClassifier()
        # Loaded must flip True — this is the smoking-gun assertion:
        # if the candidate ONNX shape were wrong, ORT would fail at
        # `analyze` time, not at construction; but `_loaded` flipping
        # confirms the session at least built.
        assert clf._loaded is True
        # Run a real inference. The output must be a Decision, not an
        # exception — _featurize → session.run feeds a (1, 200) float
        # matrix; if the trainer's ONNX expected anything else, ORT
        # would raise.
        decision = clf.analyze("how to build a bomb and kill people")
        # Verdict can be PASS/WARN/BLOCK depending on label; the
        # important check is that we got a Decision, not an
        # InvalidArgument from ORT.
        assert decision is not None
        assert decision.analyzer_id == "truzenai.safety.v1"
        # And the decision verdict is one of the production category
        # mappings. Don't pin to a specific class — the tiny training
        # corpus may not match the production-trained model perfectly.
    finally:
        # Restore production ONNX so the worktree stays clean.
        shutil.copy2(str(backup), str(prod_onnx))


def test_safety_trainer_model_name():
    assert SafetyTrainer.model_name == "safety"


def test_safety_trainer_rejects_empty(tmp_path):
    with pytest.raises(TrainingError):
        SafetyTrainer().train([], [], "v1", tmp_path)


# ── SchemaMapper trainer ────────────────────────────────────────────────────


# Schema_mapper labels live in `schema_mapper_labels.json`; the
# trainer rejects unknown labels.
_SCHEMA_X = [
    # Use raw response JSONs — the trainer flattens them via
    # production's `flatten_json` and picks the first leaf.
    json.dumps({"choices": [{"message": {"content": "Hello, this is a long response with multiple words."}}]}),
    json.dumps({"choices": [{"message": {"content": "Another long sentence with lots of natural language tokens."}}]}),
    json.dumps({"choices": [{"message": {"content": "Yet a third example response containing many words to be parsed."}}]}),
    json.dumps({"usage": {"prompt_tokens": 42}}),
    json.dumps({"usage": {"prompt_tokens": 89}}),
    json.dumps({"usage": {"prompt_tokens": 17}}),
]
_SCHEMA_Y = ["content", "content", "content", "prompt_tokens", "prompt_tokens", "prompt_tokens"]


def test_schema_trainer_emits_production_shape_onnx(tmp_path):
    """Candidate input is `(None, 139)` float; output is int64."""
    from onnxruntime import InferenceSession

    trainer = SchemaMapperTrainer()
    path = trainer.train(_SCHEMA_X, _SCHEMA_Y, version="v2", candidates_dir=tmp_path)

    assert path == tmp_path / "schema_mapper-v2.onnx"
    assert path.exists()
    assert path.stat().st_size > 200

    # Side-cars.
    labels_path = tmp_path / "schema_mapper-v2.labels.json"
    ref_path = tmp_path / "schema_mapper-v2.featurizer_ref.json"
    assert labels_path.exists()
    assert ref_path.exists()
    # The old DictVectorizer side-cars must NOT be emitted.
    assert not (tmp_path / "schema_mapper-v2.dictvec.pkl").exists()
    assert not (tmp_path / "schema_mapper-v2.feature_names.json").exists()

    # ONNX shape — input `(None, 139)` float, output int64.
    session = InferenceSession(str(path), providers=["CPUExecutionProvider"])
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    assert len(inputs) == 1
    assert inputs[0].shape == [None, 139]
    assert inputs[0].type == "tensor(float)"
    assert outputs[0].type == "tensor(int64)"


def test_schema_trainer_labels_match_production_order(tmp_path):
    """`labels.json` is a byte-for-byte copy of production's labels list."""
    SchemaMapperTrainer().train(_SCHEMA_X, _SCHEMA_Y, version="v2", candidates_dir=tmp_path)
    candidate_labels = json.loads((tmp_path / "schema_mapper-v2.labels.json").read_text())
    prod_labels = json.loads(
        (Path("src/gateway/schema/schema_mapper_labels.json")).read_text()
    )
    assert candidate_labels == prod_labels


def test_schema_trainer_featurizer_ref_records_hashes(tmp_path):
    """`featurizer_ref.json` records labels + features.py source hash."""
    SchemaMapperTrainer().train(_SCHEMA_X, _SCHEMA_Y, version="v2", candidates_dir=tmp_path)
    ref = json.loads((tmp_path / "schema_mapper-v2.featurizer_ref.json").read_text())
    for key in ("labels_sha256", "features_module_sha256"):
        assert key in ref
        assert isinstance(ref[key], str)
        assert len(ref[key]) == 64
    assert ref["expected_input_dim"] == 139


def test_schema_trainer_rejects_label_outside_production_set(tmp_path):
    bad_y = list(_SCHEMA_Y)
    bad_y[0] = "fictional_label"
    with pytest.raises(TrainingError) as exc:
        SchemaMapperTrainer().train(_SCHEMA_X, bad_y, version="v2", candidates_dir=tmp_path)
    assert "fictional_label" in str(exc.value)


def test_schema_trainer_handles_invalid_json_rows(tmp_path):
    """Invalid JSON rows become zero-feature placeholders — must not crash."""
    X = [
        "not valid json at all",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"choices": [{"message": {"content": "real content here with words"}}]}),
        json.dumps({"choices": [{"message": {"content": "another real content payload"}}]}),
        json.dumps({"usage": {"prompt_tokens": 42}}),
        json.dumps({"usage": {"prompt_tokens": 89}}),
    ]
    y = ["content", "content", "content", "content", "prompt_tokens", "prompt_tokens"]
    SchemaMapperTrainer().train(X, y, version="v9", candidates_dir=tmp_path)
    assert (tmp_path / "schema_mapper-v9.onnx").exists()


def test_schema_trainer_accepts_flatfield_dicts(tmp_path):
    """X items shaped like a `FlatField` are featurized directly.

    The dataset builder stores `input_features_json` as a JSON dict;
    a future per-field harvester revision will write FlatField-like
    rows. Verify the trainer accepts that shape today.
    """
    X = [
        json.dumps({
            "path": "choices.0.message.content", "key": "content",
            "value": "long natural language response with many words and spaces",
            "value_type": "string", "depth": 3, "parent_key": "message",
            "sibling_keys": ["role", "content"], "sibling_types": ["string", "string"],
            "int_siblings": [],
        }),
        json.dumps({
            "path": "choices.0.message.content", "key": "content",
            "value": "another piece of natural language response content",
            "value_type": "string", "depth": 3, "parent_key": "message",
            "sibling_keys": ["role", "content"], "sibling_types": ["string", "string"],
            "int_siblings": [],
        }),
        json.dumps({
            "path": "usage.prompt_tokens", "key": "prompt_tokens",
            "value": 42, "value_type": "int", "depth": 1, "parent_key": "usage",
            "sibling_keys": ["prompt_tokens", "completion_tokens"],
            "sibling_types": ["int", "int"], "int_siblings": [42, 13],
        }),
        json.dumps({
            "path": "usage.prompt_tokens", "key": "prompt_tokens",
            "value": 89, "value_type": "int", "depth": 1, "parent_key": "usage",
            "sibling_keys": ["prompt_tokens", "completion_tokens"],
            "sibling_types": ["int", "int"], "int_siblings": [89, 21],
        }),
    ]
    y = ["content", "content", "prompt_tokens", "prompt_tokens"]
    SchemaMapperTrainer().train(X, y, version="ff", candidates_dir=tmp_path)
    assert (tmp_path / "schema_mapper-ff.onnx").exists()


def test_schema_trainer_candidate_drops_into_production_class(tmp_path):
    """Train a candidate, swap it over production schema_mapper.onnx, run inference."""
    SchemaMapperTrainer().train(
        _SCHEMA_X, _SCHEMA_Y, version="dropin", candidates_dir=tmp_path,
    )
    candidate_onnx = tmp_path / "schema_mapper-dropin.onnx"
    assert candidate_onnx.exists()

    prod_onnx = Path("src/gateway/schema/schema_mapper.onnx")
    backup = tmp_path / "schema_mapper.onnx.bak"
    shutil.copy2(str(prod_onnx), str(backup))
    try:
        shutil.copy2(str(candidate_onnx), str(prod_onnx))
        import importlib
        import gateway.schema.mapper as sm
        importlib.reload(sm)
        mapper = sm.SchemaMapper()
        assert mapper._session is not None  # ORT built successfully
        # Run a real `map_response`. The matrix fed to ORT is shaped by
        # `extract_features` (139) — if the candidate ONNX expected
        # anything else, ORT would raise inside `_classify_onnx`.
        result = mapper.map_response({
            "choices": [{"message": {"content": "Real text content"}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 9},
        })
        assert result is not None
        # CanonicalResponse always has a `mapping` field; just verify it
        # exists rather than asserting any specific classification —
        # the toy candidate may not match the production model exactly.
        assert result.mapping is not None
    finally:
        shutil.copy2(str(backup), str(prod_onnx))


def test_schema_trainer_model_name():
    assert SchemaMapperTrainer.model_name == "schema_mapper"
