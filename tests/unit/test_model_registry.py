from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from gateway.intelligence.registry import ALLOWED_MODEL_NAMES, Candidate, ModelRegistry


def test_registry_ensures_directories(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    assert (tmp_path / "production").is_dir()
    assert (tmp_path / "candidates").is_dir()
    assert (tmp_path / "archive").is_dir()
    assert (tmp_path / "archive" / "failed").is_dir()


def test_ensure_structure_is_idempotent(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    r.ensure_structure()  # must not raise


def test_production_path_returns_expected_layout(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    p = r.production_path("intent")
    assert p == tmp_path / "production" / "intent.onnx"


def test_list_production_models_empty(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    assert r.list_production_models() == []


def test_list_production_models(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "production" / "intent.onnx").write_bytes(b"fake")
    (tmp_path / "production" / "schema_mapper.onnx").write_bytes(b"fake")
    assert set(r.list_production_models()) == {"intent", "schema_mapper"}


def test_list_production_models_ignores_non_onnx(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "production" / "intent.onnx").write_bytes(b"fake")
    (tmp_path / "production" / "notes.txt").write_text("ignore me")
    (tmp_path / "production" / ".hidden").write_text("also ignore")
    assert r.list_production_models() == ["intent"]


def test_list_production_models_skips_versioned_files(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "production" / "intent.onnx").write_bytes(b"fake")
    # A stray candidate-style file in production/ must NOT be treated as production.
    (tmp_path / "production" / "intent-v2.onnx").write_bytes(b"fake")
    assert r.list_production_models() == ["intent"]


def test_list_candidates_empty(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    assert r.list_candidates() == []


def test_list_candidates(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"fake")
    (tmp_path / "candidates" / "safety-v5.onnx").write_bytes(b"fake")
    (tmp_path / "candidates" / "schema_mapper-v10.onnx").write_bytes(b"fake")
    cands = r.list_candidates()
    assert len(cands) == 3
    by_model = {c.model: c for c in cands}
    assert by_model["intent"].version == "v2"
    assert by_model["safety"].version == "v5"
    assert by_model["schema_mapper"].version == "v10"
    assert by_model["intent"].path == tmp_path / "candidates" / "intent-v2.onnx"


def test_list_candidates_skips_malformed_filenames(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"fake")
    (tmp_path / "candidates" / "no_version.onnx").write_bytes(b"fake")  # no -version
    (tmp_path / "candidates" / "bad name.onnx").write_bytes(b"fake")  # space
    cands = r.list_candidates()
    assert len(cands) == 1
    assert cands[0].model == "intent"


def test_candidate_is_frozen(tmp_path):
    c = Candidate(model="intent", version="v1", path=Path("/tmp/x"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.model = "changed"  # type: ignore[misc]


def test_lock_for_returns_same_lock_per_model(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    l1 = r.lock_for("intent")
    l2 = r.lock_for("intent")
    assert l1 is l2  # same instance


def test_lock_for_returns_different_locks_per_model(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    l_intent = r.lock_for("intent")
    l_safety = r.lock_for("safety")
    assert l_intent is not l_safety


def test_production_path_rejects_unknown_model(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    with pytest.raises(ValueError, match="unknown model name"):
        r.production_path("../../etc/passwd")
    with pytest.raises(ValueError, match="unknown model name"):
        r.production_path("not_a_real_model")


def test_lock_for_rejects_unknown_model(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    with pytest.raises(ValueError, match="unknown model name"):
        r.lock_for("../../etc/passwd")


def test_list_candidates_filters_phantom_models(tmp_path):
    # Regex alone would parse `prefix-intent-v2.onnx` as model=prefix,
    # version=intent-v2. The ALLOWED_MODEL_NAMES filter must kill that.
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"fake")
    (tmp_path / "candidates" / "prefix-intent-v2.onnx").write_bytes(b"fake")
    cands = r.list_candidates()
    assert len(cands) == 1
    assert cands[0].model == "intent"
    assert cands[0].version == "v2"


def test_list_candidates_is_sorted(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "candidates" / "safety-v5.onnx").write_bytes(b"fake")
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"fake")
    (tmp_path / "candidates" / "intent-v1.onnx").write_bytes(b"fake")
    cands = r.list_candidates()
    # Sorted by (model, version) — deterministic across filesystems.
    assert [(c.model, c.version) for c in cands] == [
        ("intent", "v1"),
        ("intent", "v2"),
        ("safety", "v5"),
    ]


def test_allowed_model_names_matches_model_verdict():
    # Guard against drift: these names are duplicated in ModelVerdict
    # recording sites (intent.py, unified.py, schema/mapper.py, safety_classifier.py).
    # If someone changes one set without the other, this test fails loudly.
    assert ALLOWED_MODEL_NAMES == frozenset({"intent", "schema_mapper", "safety"})
