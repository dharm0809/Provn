"""Phase 25 Task 19: SchemaMapper + Safety trainer tests.

Both use the same `Trainer` base contract. After the classifier-only
refactor (skl2onnx can't convert `analyzer='char_wb'` TfidfVectorizer
in-graph, and DictVectorizer in-graph silently diverges from
`pipeline.predict`), the trainers serialize the classifier sub-step
alone and ship featurizer state as side-cars. These tests therefore
drive a real end-to-end conversion + side-car emission and verify the
ONNX graph reproduces `pipeline.predict()` after manual featurization.

The pickle reads in this file load the schema trainer's own
`dictvec.pkl` side-car from the test's `tmp_path` — a controlled local
fixture path the test itself just produced. Same trust contract as
the production schema sanity adapter (see `schema_trainer.py`
docstring).
"""
from __future__ import annotations

import json
import pickle  # noqa: S403 — local-only fixtures from tmp_path; never network-deserialized
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


def _featurize_safety(text: str, vocab: dict[str, int], idf: np.ndarray) -> np.ndarray:
    """Reproduce the trainer's char_wb TF-IDF (3-5 gram, sublinear, l2)."""
    text_lower = f" {text.lower()} "
    counts: dict[str, int] = {}
    for n in range(3, 6):
        for i in range(len(text_lower) - n + 1):
            gram = text_lower[i:i + n]
            if gram in vocab:
                counts[gram] = counts.get(gram, 0) + 1
    n_features = len(idf)
    vec = np.zeros(n_features, dtype=np.float32)
    for gram, c in counts.items():
        idx = vocab[gram]
        if idx < n_features:
            vec[idx] = np.log1p(c)
    vec *= idf
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec.reshape(1, -1)


def test_safety_trainer_writes_runnable_classifier_only_onnx(tmp_path):
    """End-to-end: train, convert, load via ORT, match `pipeline.predict()`."""
    from onnxruntime import InferenceSession

    trainer = SafetyTrainer()
    path = trainer.train(_SAFETY_X, _SAFETY_Y, version="v1", candidates_dir=tmp_path)

    # Candidate file landed and is non-trivially sized.
    assert path == tmp_path / "safety-v1.onnx"
    assert path.exists()
    assert path.stat().st_size > 200  # ORT models are at least that

    # Side-cars present (Agent N's contract).
    vocab_path = tmp_path / "safety-v1.vocab.json"
    idf_path = tmp_path / "safety-v1.idf.npy"
    labels_path = tmp_path / "safety-v1.labels.json"
    assert vocab_path.exists()
    assert idf_path.exists()
    assert labels_path.exists()

    # Calibration JSON written by the base.
    calib = json.loads((tmp_path / "safety-v1-calibration.json").read_text())
    assert calib["model_name"] == "safety"
    assert calib["class_counts"] == {"safe": 4, "violence": 4}
    assert calib["total_samples"] == 8

    # Now run the candidate ONNX through ORT using the side-cars to
    # featurize, and compare the predictions to the in-memory pipeline.
    vocab = json.loads(vocab_path.read_text())
    idf = np.load(str(idf_path))

    # Re-fit a pipeline with identical seed / topology to compare against.
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import Pipeline

    expected_pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5),
            min_df=1, max_df=1.0, sublinear_tf=True,
        )),
        ("clf", GradientBoostingClassifier(
            n_estimators=50, max_depth=4, learning_rate=0.1, random_state=42,
        )),
    ])
    expected_pipeline.fit(_SAFETY_X, _SAFETY_Y)
    expected = expected_pipeline.predict(_SAFETY_X)

    session = InferenceSession(str(path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    onnx_preds: list[str] = []
    for x in _SAFETY_X:
        feat = _featurize_safety(x, vocab, idf).astype(np.float32)
        outs = session.run(None, {input_name: feat})
        onnx_preds.append(str(outs[0][0]))

    # Each prediction must match `pipeline.predict()` — the classifier-only
    # graph is a faithful serialization once we've reproduced the
    # featurizer in Python from the side-cars.
    assert list(onnx_preds) == list(expected)


def test_safety_trainer_model_name():
    assert SafetyTrainer.model_name == "safety"


def test_safety_trainer_rejects_empty(tmp_path):
    with pytest.raises(TrainingError):
        SafetyTrainer().train([], [], "v1", tmp_path)


# ── SchemaMapper trainer ────────────────────────────────────────────────────


_SCHEMA_X = [
    {"len": 400, "word_count": 65, "has_role": 1, "depth": 1},
    {"len": 620, "word_count": 110, "has_role": 1, "depth": 1},
    {"len": 510, "word_count": 80, "has_role": 1, "depth": 1},
    {"len": 12, "word_count": 2, "is_int": 1, "magnitude": 2},
    {"len": 14, "word_count": 2, "is_int": 1, "magnitude": 3},
    {"len": 16, "word_count": 2, "is_int": 1, "magnitude": 3},
]
_SCHEMA_Y = ["content", "content", "content", "prompt_tokens", "prompt_tokens", "prompt_tokens"]


def test_schema_trainer_writes_runnable_classifier_only_onnx(tmp_path):
    """End-to-end: train, convert, load via ORT, match `pipeline.predict()`."""
    from onnxruntime import InferenceSession

    # Pass the dicts as JSON strings (matches the dataset builder).
    X_json = [json.dumps(x) for x in _SCHEMA_X]
    trainer = SchemaMapperTrainer()
    path = trainer.train(X_json, _SCHEMA_Y, version="v2", candidates_dir=tmp_path)

    assert path == tmp_path / "schema_mapper-v2.onnx"
    assert path.exists()
    assert path.stat().st_size > 200

    # Side-cars (Agent N's contract).
    pkl_path = tmp_path / "schema_mapper-v2.dictvec.pkl"
    names_path = tmp_path / "schema_mapper-v2.feature_names.json"
    assert pkl_path.exists()
    assert names_path.exists()

    calib = json.loads((tmp_path / "schema_mapper-v2-calibration.json").read_text())
    assert calib["model_name"] == "schema_mapper"
    assert calib["class_counts"] == {"content": 3, "prompt_tokens": 3}

    # Reload the pickled DictVectorizer, featurize fixtures, run ORT,
    # compare to a fresh pipeline trained with the same seed/topology.
    with open(pkl_path, "rb") as fh:
        vec = pickle.load(fh)  # noqa: S301 — controlled tmp_path produced by this test

    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.pipeline import Pipeline

    expected_pipeline = Pipeline([
        ("vec", DictVectorizer(sparse=False)),
        ("clf", GradientBoostingClassifier(
            n_estimators=50, max_depth=3, learning_rate=0.1, random_state=42,
        )),
    ])
    expected_pipeline.fit(_SCHEMA_X, _SCHEMA_Y)
    expected = expected_pipeline.predict(_SCHEMA_X)

    session = InferenceSession(str(path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    onnx_preds: list[str] = []
    for x in _SCHEMA_X:
        feat = vec.transform([x]).astype(np.float32)
        feat = np.nan_to_num(feat, nan=0.0, posinf=1.0, neginf=-1.0)
        outs = session.run(None, {input_name: feat})
        onnx_preds.append(str(outs[0][0]))

    assert list(onnx_preds) == list(expected)


def test_schema_trainer_accepts_raw_dicts(tmp_path):
    # The dataset builder uses JSON strings, but a trainer caller may
    # pass parsed dicts directly — both must work.
    SchemaMapperTrainer().train(_SCHEMA_X, _SCHEMA_Y, version="v3", candidates_dir=tmp_path)
    assert (tmp_path / "schema_mapper-v3.onnx").exists()


def test_schema_trainer_parses_invalid_json_as_empty_features(tmp_path):
    # Invalid JSON or non-dict JSON must not crash fit — they become
    # empty feature dicts, which DictVectorizer treats as zero vectors.
    # We still need at least one row with usable features so DictVectorizer
    # has at least one column (otherwise classifier-only ONNX conversion
    # has nothing to fit).
    X = [
        "not valid json",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"f1": 1.0, "f2": 0.5}),
        json.dumps({"f1": 2.0, "f2": 0.7}),
        json.dumps({"f1": 1.5, "f2": 0.6}),
        json.dumps({"f1": 2.5, "f2": 0.8}),
    ]
    y = ["content", "content", "prompt_tokens", "prompt_tokens", "content", "prompt_tokens"]
    SchemaMapperTrainer().train(X, y, version="v1", candidates_dir=tmp_path)
    assert (tmp_path / "schema_mapper-v1.onnx").exists()


def test_schema_trainer_ignores_non_numeric_feature_values(tmp_path):
    # Bool-looking numerics (True/False) and strings must be filtered —
    # DictVectorizer would otherwise treat bools as features in its own
    # way, producing inconsistent feature dimensions between train and
    # serve.
    X = [
        {"f1": 1.0, "meta": "string-ignore-me", "flag": True},
        {"f1": 2.0, "meta": "also-ignore", "flag": False},
        {"f1": 1.5, "meta": "x", "flag": True},
        {"f1": 2.5, "meta": "y", "flag": False},
    ]
    y = ["a", "b", "a", "b"]
    SchemaMapperTrainer().train(X, y, version="v1", candidates_dir=tmp_path)


def test_schema_trainer_model_name():
    assert SchemaMapperTrainer.model_name == "schema_mapper"
