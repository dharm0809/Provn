"""Phase 25 Task 18: IntentTrainer tests.

After the classifier-only refactor (skl2onnx can't convert
`analyzer='char_wb'` TfidfVectorizer in-graph), the trainer serializes
the LogisticRegression sub-step alone and ships TF-IDF state as
side-cars. These tests drive a real end-to-end conversion +
side-car emission and verify the ONNX graph reproduces
`pipeline.predict()` after manual featurization.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

# Skip the whole module when sklearn / skl2onnx / onnxruntime aren't
# available — the conversion path can't run without them.
pytest.importorskip("sklearn")
pytest.importorskip("skl2onnx")
pytest.importorskip("onnxruntime")

from gateway.intelligence.distillation.trainers.base import TrainingError
from gateway.intelligence.distillation.trainers.intent_trainer import IntentTrainer


_X = [
    "search for the latest iPhone news online",
    "find online reviews of this car please",
    "look up today's weather forecast",
    "search the web for breaking stories",
    "tell me a joke about cats and dogs",
    "what is the meaning of life and time",
    "how do birds fly through the sky",
    "explain quantum mechanics in simple terms",
]
_Y = [
    "web_search", "web_search", "web_search", "web_search",
    "normal", "normal", "normal", "normal",
]


def _featurize(text: str, vocab: dict[str, int], idf: np.ndarray) -> np.ndarray:
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


def test_train_writes_runnable_classifier_only_onnx(tmp_path):
    """End-to-end: train, convert, load via ORT, match `pipeline.predict()`."""
    from onnxruntime import InferenceSession

    trainer = IntentTrainer()
    path = trainer.train(_X, _Y, version="v1", candidates_dir=tmp_path)

    assert path == tmp_path / "intent-v1.onnx"
    assert path.exists()
    assert path.stat().st_size > 200

    # Side-cars (vocab + idf + labels).
    vocab_path = tmp_path / "intent-v1.vocab.json"
    idf_path = tmp_path / "intent-v1.idf.npy"
    labels_path = tmp_path / "intent-v1.labels.json"
    assert vocab_path.exists()
    assert idf_path.exists()
    assert labels_path.exists()

    # Calibration JSON.
    calib = json.loads((tmp_path / "intent-v1-calibration.json").read_text())
    assert calib["model_name"] == "intent"
    assert calib["version"] == "v1"
    assert calib["total_samples"] == 8
    assert calib["class_counts"] == {"normal": 4, "web_search": 4}
    assert abs(sum(calib["class_priors"].values()) - 1.0) < 1e-6

    # Run the candidate ONNX through ORT using the side-cars to
    # featurize, and compare to a fresh pipeline trained with the
    # same topology.
    vocab = json.loads(vocab_path.read_text())
    idf = np.load(str(idf_path))

    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    expected_pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5),
            min_df=1, max_df=1.0, sublinear_tf=True,
        )),
        ("clf", LogisticRegression(
            solver="liblinear", max_iter=1000, class_weight="balanced",
        )),
    ])
    expected_pipeline.fit(_X, _Y)
    expected = expected_pipeline.predict(_X)

    session = InferenceSession(str(path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    onnx_preds: list[str] = []
    for x in _X:
        feat = _featurize(x, vocab, idf).astype(np.float32)
        outs = session.run(None, {input_name: feat})
        onnx_preds.append(str(outs[0][0]))

    assert list(onnx_preds) == list(expected)


def test_train_creates_candidates_dir(tmp_path):
    trainer = IntentTrainer()
    nested = tmp_path / "nested" / "candidates"
    assert not nested.exists()
    # Two-class minimum and at least a few rows so TF-IDF has a
    # non-empty vocab to convert.
    trainer.train(
        ["alpha bravo charlie", "delta echo foxtrot", "alpha bravo", "delta echo"],
        ["x", "y", "x", "y"],
        "v2",
        nested,
    )
    assert nested.is_dir()
    assert (nested / "intent-v2.onnx").exists()


def test_train_raises_on_empty_dataset(tmp_path):
    trainer = IntentTrainer()
    with pytest.raises(TrainingError, match="empty"):
        trainer.train([], [], "v1", tmp_path)


def test_train_raises_on_xy_length_mismatch(tmp_path):
    trainer = IntentTrainer()
    with pytest.raises(TrainingError, match="length mismatch"):
        trainer.train(["a", "b"], ["x"], "v1", tmp_path)


def test_train_raises_on_single_class(tmp_path):
    trainer = IntentTrainer()
    with pytest.raises(TrainingError, match="at least 2 classes"):
        trainer.train(["a", "b", "c"], ["x", "x", "x"], "v1", tmp_path)


def test_train_rejects_path_traversal_version(tmp_path):
    trainer = IntentTrainer()
    with pytest.raises(TrainingError, match="invalid version"):
        trainer.train(["a", "b"], ["x", "y"], "../evil", tmp_path)
    with pytest.raises(TrainingError, match="invalid version"):
        trainer.train(["a", "b"], ["x", "y"], "v1/escape", tmp_path)


def test_model_name_constant():
    assert IntentTrainer.model_name == "intent"
