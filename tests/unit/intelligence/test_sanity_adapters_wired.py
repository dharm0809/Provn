"""SanityAdapter wiring tests for safety + schema_mapper.

Exercises the adapter strategy table in
`gateway.intelligence.sanity_adapters`:

  * trainers emit side-cars next to their candidate ONNX,
  * adapters require those side-cars,
  * a candidate without side-cars (older trainer revision) surfaces
    as a sanity FAILURE, not a silent skip.

After the trainer refactor (classifier-only ONNX + side-car-applied
TF-IDF in Python), the adapters expect a `FloatTensorType` input.
Test fixtures here build the same classifier-only topology so the
adapter contract — "feed featurized input via vocab/idf side-cars,
read predicted-label string back" — is exercised end-to-end.
"""
from __future__ import annotations

import json
import pickle  # noqa: S403 — local-only fixtures, never network-deserialized
from pathlib import Path

import numpy as np
import pytest

# Skip the whole module when sklearn / skl2onnx / onnxruntime aren't
# available (matches the trainer's own ImportError-as-TrainingError
# guard). Without these we can't build a real fixture ONNX.
pytest.importorskip("sklearn")
pytest.importorskip("skl2onnx")
pytest.importorskip("onnxruntime")

from gateway.intelligence import sanity_adapters
from gateway.intelligence.distillation.trainers.safety_trainer import SafetyTrainer
from gateway.intelligence.distillation.trainers.schema_trainer import SchemaMapperTrainer
from gateway.intelligence.sanity_runner import SanityRunner


# ── shared helpers ────────────────────────────────────────────────────────


def _build_safety_onnx(path: Path) -> tuple[list[str], dict[str, int], np.ndarray]:
    """Train a tiny char_wb TF-IDF + GBC and export the CLASSIFIER alone.

    Mirrors the production trainer's classifier-only topology: skl2onnx
    can't convert char_wb in-graph, so we ship the classifier with a
    `FloatTensorType` input matching the fitted TF-IDF dimension. The
    adapter applies char_wb TF-IDF in Python from the side-cars before
    invoking the session.

    Returns (labels, vocab, idf) so the caller can write side-cars.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import Pipeline
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType

    X = [
        "kill bomb attack violent",
        "shoot stab violent maim",
        "kill weapon attack",
        "shoot bomb violent",
        "happy flowers nice poem safe",
        "safe text response calm",
        "happy response calm safe",
        "nice flowers calm safe",
    ]
    y = ["violence", "violence", "violence", "violence", "safe", "safe", "safe", "safe"]

    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5),
            min_df=1, sublinear_tf=True,
        )),
        ("clf", GradientBoostingClassifier(n_estimators=5, random_state=42)),
    ])
    pipeline.fit(X, y)

    tfidf = pipeline.named_steps["tfidf"]
    clf = pipeline.named_steps["clf"]
    n_features = len(tfidf.vocabulary_)

    initial_type = [("features", FloatTensorType([None, n_features]))]
    onx = convert_sklearn(clf, initial_types=initial_type)
    path.write_bytes(onx.SerializeToString())

    return (
        [str(c) for c in clf.classes_],
        {k: int(v) for k, v in tfidf.vocabulary_.items()},
        np.asarray(tfidf.idf_, dtype=np.float32),
    )


def _build_schema_classifier_only_onnx(path: Path):
    """Build a classifier-only ONNX matching production schema_mapper.onnx.

    Production's `schema_mapper.onnx` takes a `(None, n_features)` float
    tensor (DictVectorizer is NOT in the graph). The trainer's current
    Pipeline-based export emits an INVALID ONNX (DictVectorizer node
    with float input — a separate pre-existing issue). For the adapter
    test we mirror the production topology directly.

    Returns (labels, fitted DictVectorizer) so the caller can write
    side-cars matching the trained ordering.
    """
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.ensemble import GradientBoostingClassifier
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType

    rows = [
        {"len": 400, "word_count": 65, "has_role": 1, "depth": 1},
        {"len": 620, "word_count": 110, "has_role": 1, "depth": 1},
        {"len": 12, "word_count": 2, "is_int": 1, "magnitude": 2},
        {"len": 14, "word_count": 2, "is_int": 1, "magnitude": 3},
        {"len": 12, "word_count": 2, "is_int": 1, "magnitude": 3},
        {"len": 14, "word_count": 2, "is_int": 1, "magnitude": 3},
        {"len": 4, "word_count": 1, "has_role": 0, "depth": 0},
        {"len": 3, "word_count": 1, "has_role": 0, "depth": 0},
    ]
    y = [
        "content", "content",
        "prompt_tokens", "prompt_tokens",
        "completion_tokens", "completion_tokens",
        "finish_reason", "finish_reason",
    ]

    vec = DictVectorizer(sparse=False)
    X_mat = vec.fit_transform(rows)
    clf = GradientBoostingClassifier(n_estimators=5, random_state=42)
    clf.fit(X_mat, y)

    onx = convert_sklearn(
        clf,
        initial_types=[("features", FloatTensorType([None, X_mat.shape[1]]))],
    )
    path.write_bytes(onx.SerializeToString())
    return [str(c) for c in clf.classes_], vec


def _sidecar(candidates_dir: Path, model: str, version: str, suffix: str) -> Path:
    return candidates_dir / f"{model}-{version}.{suffix}"


def _write_safety_sidecars(
    candidates_dir: Path,
    version: str,
    labels: list[str],
    vocab: dict[str, int],
    idf: np.ndarray,
) -> None:
    _sidecar(candidates_dir, "safety", version, "labels.json").write_text(json.dumps(labels))
    _sidecar(candidates_dir, "safety", version, "vocab.json").write_text(
        json.dumps(vocab, sort_keys=True)
    )
    np.save(str(_sidecar(candidates_dir, "safety", version, "idf.npy")), idf)


def _write_schema_sidecars(candidates_dir: Path, version: str, vec) -> None:
    _sidecar(candidates_dir, "schema_mapper", version, "feature_names.json").write_text(
        json.dumps(list(vec.feature_names_))
    )
    with open(_sidecar(candidates_dir, "schema_mapper", version, "dictvec.pkl"), "wb") as fh:
        pickle.dump(vec, fh, protocol=pickle.HIGHEST_PROTOCOL)


# ── safety adapter ────────────────────────────────────────────────────────


def test_safety_adapter_loads_sidecars_and_returns_labels(tmp_path):
    """Happy path — full candidate (.onnx + 3 side-cars) → label string back."""
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "safety-v1.onnx"
    labels, vocab, idf = _build_safety_onnx(candidate_path)
    _write_safety_sidecars(candidates_dir, "v1", labels, vocab, idf)

    infer = sanity_adapters.build_infer_fn("safety", candidate_path)
    # The fixture is trained on a 2-class dataset so we just verify the
    # adapter returns one of the trained labels — exact accuracy is the
    # SanityRunner's job, not the adapter's.
    out = infer("kill bomb attack violent")
    assert out in labels


def test_safety_adapter_blocks_when_labels_sidecar_missing(tmp_path):
    """Older trainer revision left only the .onnx — adapter must raise."""
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "safety-v1.onnx"
    labels, vocab, idf = _build_safety_onnx(candidate_path)
    # Deliberately skip labels.json.
    _sidecar(candidates_dir, "safety", "v1", "vocab.json").write_text(
        json.dumps(vocab, sort_keys=True)
    )
    np.save(str(_sidecar(candidates_dir, "safety", "v1", "idf.npy")), idf)

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("safety", candidate_path)
    msg = str(exc.value)
    assert "labels.json" in msg
    # The error message names the model so an operator can correlate
    # without having to grep file paths.
    assert "safety" in msg


def test_safety_adapter_blocks_when_vocab_sidecar_missing(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "safety-v1.onnx"
    labels, _vocab, idf = _build_safety_onnx(candidate_path)
    _sidecar(candidates_dir, "safety", "v1", "labels.json").write_text(json.dumps(labels))
    np.save(str(_sidecar(candidates_dir, "safety", "v1", "idf.npy")), idf)

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("safety", candidate_path)
    assert "vocab.json" in str(exc.value)


def test_safety_adapter_blocks_when_idf_sidecar_missing(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "safety-v1.onnx"
    labels, vocab, _idf = _build_safety_onnx(candidate_path)
    _sidecar(candidates_dir, "safety", "v1", "labels.json").write_text(json.dumps(labels))
    _sidecar(candidates_dir, "safety", "v1", "vocab.json").write_text(
        json.dumps(vocab, sort_keys=True)
    )

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("safety", candidate_path)
    assert "idf.npy" in str(exc.value)


def test_safety_trainer_writes_all_sidecars(tmp_path):
    """Drive the trainer's _write_sidecars hook directly.

    Skips the broken skl2onnx export (char_wb is unsupported) so the
    test stays focused on side-car emission. Verifies all three
    files land at the expected paths with parseable contents.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import Pipeline

    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(min_df=1)),
        ("clf", GradientBoostingClassifier(n_estimators=2, random_state=42)),
    ])
    pipeline.fit(
        ["kill bomb attack", "happy nice poem"],
        ["violence", "safe"],
    )

    SafetyTrainer()._write_sidecars(pipeline, "v3", tmp_path)

    labels = json.loads((tmp_path / "safety-v3.labels.json").read_text())
    vocab = json.loads((tmp_path / "safety-v3.vocab.json").read_text())
    idf = np.load(str(tmp_path / "safety-v3.idf.npy"))
    assert sorted(labels) == ["safe", "violence"]
    assert all(isinstance(v, int) for v in vocab.values())
    assert idf.dtype == np.float32
    assert idf.shape[0] == len(vocab)


# ── schema_mapper adapter ─────────────────────────────────────────────────


def test_schema_mapper_adapter_loads_sidecars_and_returns_labels(tmp_path):
    """Happy path — full candidate (.onnx + dictvec.pkl + names.json)."""
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "schema_mapper-v1.onnx"
    labels, vec = _build_schema_classifier_only_onnx(candidate_path)
    _write_schema_sidecars(candidates_dir, "v1", vec)

    infer = sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    out = infer({"len": 400, "word_count": 65, "has_role": 1, "depth": 1})
    assert out in labels


def test_schema_mapper_adapter_blocks_when_pkl_missing(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "schema_mapper-v1.onnx"
    _labels, vec = _build_schema_classifier_only_onnx(candidate_path)
    # Write feature_names.json but NOT dictvec.pkl.
    _sidecar(candidates_dir, "schema_mapper", "v1", "feature_names.json").write_text(
        json.dumps(list(vec.feature_names_))
    )

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    assert "dictvec.pkl" in str(exc.value)


def test_schema_mapper_adapter_blocks_when_feature_names_missing(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "schema_mapper-v1.onnx"
    _labels, vec = _build_schema_classifier_only_onnx(candidate_path)
    # Write pickle but NOT feature_names.json.
    with open(_sidecar(candidates_dir, "schema_mapper", "v1", "dictvec.pkl"), "wb") as fh:
        pickle.dump(vec, fh, protocol=pickle.HIGHEST_PROTOCOL)

    with pytest.raises(FileNotFoundError) as exc:
        sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    assert "feature_names.json" in str(exc.value)


def test_schema_mapper_adapter_handles_missing_features_in_row(tmp_path):
    """A fixture row with extra/missing keys still produces a label.

    DictVectorizer fills unknown keys with 0 — same behavior as the
    production featurizer; the adapter shouldn't crash on incomplete
    inputs.
    """
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "schema_mapper-v1.onnx"
    labels, vec = _build_schema_classifier_only_onnx(candidate_path)
    _write_schema_sidecars(candidates_dir, "v1", vec)

    infer = sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    # Empty dict — DictVectorizer.transform → all-zeros.
    out = infer({})
    assert out in labels
    # Row with a feature the vectorizer never saw — silently dropped.
    out = infer({"never_seen_feature": 999.0})
    assert out in labels


def test_schema_mapper_trainer_writes_all_sidecars(tmp_path):
    """Drive the trainer's _write_sidecars hook directly.

    Mirrors the safety variant: build a fitted pipeline, emit side-cars,
    confirm both files land. Skips the (broken) skl2onnx export step.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.pipeline import Pipeline

    pipeline = Pipeline([
        ("vec", DictVectorizer(sparse=False)),
        ("clf", GradientBoostingClassifier(n_estimators=2, random_state=42)),
    ])
    pipeline.fit(
        [{"a": 1.0}, {"a": 2.0}, {"b": 1.0}, {"a": 3.0, "b": 2.0}],
        ["x", "y", "x", "y"],
    )

    SchemaMapperTrainer()._write_sidecars(pipeline, "v7", tmp_path)

    names = json.loads((tmp_path / "schema_mapper-v7.feature_names.json").read_text())
    assert names == ["a", "b"]

    with open(tmp_path / "schema_mapper-v7.dictvec.pkl", "rb") as fh:
        loaded_vec = pickle.load(fh)  # noqa: S301 — controlled tmp_path
    assert list(loaded_vec.feature_names_) == ["a", "b"]
    # The pickled vec must round-trip transform identically to the
    # original — that's the contract the adapter relies on.
    out = loaded_vec.transform([{"a": 0.5}])
    assert out.tolist() == [[0.5, 0.0]]


# ── strategy table contract ───────────────────────────────────────────────


def test_wired_models_contains_all_three_now():
    """Backward-compat invariant: previously-deferred models now wired."""
    assert sanity_adapters.WIRED_MODELS == frozenset(
        {"intent", "safety", "schema_mapper"}
    )
    assert sanity_adapters.is_wired("intent") is True
    assert sanity_adapters.is_wired("safety") is True
    assert sanity_adapters.is_wired("schema_mapper") is True
    # Unknown name is not wired.
    assert sanity_adapters.is_wired("not_a_model") is False


# ── SanityRunner end-to-end (adapter + runner together) ──────────────────


def test_sanity_runner_blocks_when_adapter_predicts_below_floor(tmp_path):
    """The runner uses the adapter and detects per-class floor failures.

    Build a candidate that predicts a single class for everything, then
    check that the runner correctly fails the per-class accuracy gate
    for the OTHER class. This exercises the full adapter→runner path
    we just wired.
    """
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    candidate_path = candidates_dir / "schema_mapper-v1.onnx"
    labels, vec = _build_schema_classifier_only_onnx(candidate_path)
    _write_schema_sidecars(candidates_dir, "v1", vec)

    # Author a custom fixture with two labels: one the candidate gets
    # right (a class it was trained on) and one it's guaranteed to
    # get wrong (a label outside its training set).
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "schema_mapper_sanity.json").write_text(json.dumps({
        "model_name": "schema_mapper",
        "examples": [
            # Big-content rows — the candidate has been trained to call
            # these "content" so they pass.
            {"input": {"len": 400, "word_count": 65, "has_role": 1, "depth": 1},
             "label": "content"},
            {"input": {"len": 620, "word_count": 110, "has_role": 1, "depth": 1},
             "label": "content"},
            # Force a label that doesn't exist in the candidate — every
            # one of these is wrong, so the per-class floor (0.7) fails.
            {"input": {"len": 0, "word_count": 0}, "label": "definitely_wrong"},
            {"input": {"len": 0, "word_count": 0}, "label": "definitely_wrong"},
        ],
    }))

    infer = sanity_adapters.build_infer_fn("schema_mapper", candidate_path)
    runner = SanityRunner(fixtures_dir=fixtures_dir)
    result = runner.run("schema_mapper", infer, min_per_class_accuracy=0.7)
    assert result.passed is False
    assert "definitely_wrong" in result.failing_classes
    # The candidate scored 100% on `content` — verify the gate doesn't
    # blanket-fail the model just because one class missed.
    assert result.per_class_accuracy.get("content") == 1.0
