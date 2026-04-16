from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from gateway.intelligence.registry import ALLOWED_MODEL_NAMES, Candidate, ModelRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


@pytest.mark.anyio
async def test_promote_swaps_atomically(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "production" / "intent.onnx").write_bytes(b"v1")
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"v2")
    await r.promote("intent", "v2")
    # Production now holds the candidate bytes
    assert (tmp_path / "production" / "intent.onnx").read_bytes() == b"v2"
    # Candidate file is gone
    assert not (tmp_path / "candidates" / "intent-v2.onnx").exists()
    # Old production archived
    archived = list((tmp_path / "archive").glob("intent-*.onnx"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"v1"


@pytest.mark.anyio
async def test_promote_with_empty_production(tmp_path):
    # Migration case: no existing production, promote candidate cleanly.
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "candidates" / "intent-v1.onnx").write_bytes(b"first")
    await r.promote("intent", "v1")
    assert (tmp_path / "production" / "intent.onnx").read_bytes() == b"first"
    # No archive file created (nothing to archive)
    assert list((tmp_path / "archive").glob("intent-*.onnx")) == []


@pytest.mark.anyio
async def test_promote_missing_candidate_raises(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    with pytest.raises(FileNotFoundError):
        await r.promote("intent", "v99")


@pytest.mark.anyio
async def test_promote_rejects_unknown_model(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    with pytest.raises(ValueError, match="unknown model name"):
        await r.promote("../../etc/passwd", "v1")


@pytest.mark.anyio
async def test_promote_archive_filenames_are_unique_under_rapid_fire(tmp_path):
    # Promote-promote-promote in the same second must produce distinct archives.
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    # Seed production + 3 candidates
    (tmp_path / "production" / "intent.onnx").write_bytes(b"v0")
    for i in range(3):
        (tmp_path / "candidates" / f"intent-v{i+1}.onnx").write_bytes(f"v{i+1}".encode())
    await r.promote("intent", "v1")
    await r.promote("intent", "v2")
    await r.promote("intent", "v3")
    archived = list((tmp_path / "archive").glob("intent-*.onnx"))
    # Three previous productions (v0, v1, v2) should all be archived
    assert len(archived) == 3
    names = {p.name for p in archived}
    assert len(names) == 3  # all unique


@pytest.mark.anyio
async def test_rollback_restores_archived_version(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    # Establish: current prod is v2, v1 was archived previously
    (tmp_path / "production" / "intent.onnx").write_bytes(b"v2")
    (tmp_path / "archive" / "intent-archived-20260101T000000.000000Z.onnx").write_bytes(b"v1")
    await r.rollback("intent", "intent-archived-20260101T000000.000000Z.onnx")
    assert (tmp_path / "production" / "intent.onnx").read_bytes() == b"v1"
    # v2 was archived before overwriting
    remaining = list((tmp_path / "archive").glob("intent-*.onnx"))
    assert len(remaining) == 1
    assert remaining[0].read_bytes() == b"v2"


@pytest.mark.anyio
async def test_rollback_missing_archive_raises(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    with pytest.raises(FileNotFoundError):
        await r.rollback("intent", "intent-archived-nonexistent.onnx")


@pytest.mark.anyio
async def test_rollback_rejects_path_traversal(tmp_path):
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    with pytest.raises(ValueError, match="invalid archived_filename"):
        await r.rollback("intent", "../production/intent.onnx")
    with pytest.raises(ValueError, match="invalid archived_filename"):
        await r.rollback("intent", "archive/sub/intent-v1.onnx")


@pytest.mark.anyio
async def test_rollback_rejects_wrong_model_prefix(tmp_path):
    # Rolling back `intent` to a safety-*.onnx archive must fail.
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "archive" / "safety-v5.onnx").write_bytes(b"fake")
    with pytest.raises(ValueError, match="does not belong to model"):
        await r.rollback("intent", "safety-v5.onnx")


@pytest.mark.anyio
async def test_promote_is_serialized_per_model(tmp_path):
    # Two concurrent promotes of the same model must serialize — no race on
    # archive filename collision or half-moved files.
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    (tmp_path / "production" / "intent.onnx").write_bytes(b"v0")
    (tmp_path / "candidates" / "intent-v1.onnx").write_bytes(b"v1")
    (tmp_path / "candidates" / "intent-v2.onnx").write_bytes(b"v2")
    # Fire two promotes concurrently — the lock serializes them.
    await asyncio.gather(r.promote("intent", "v1"), r.promote("intent", "v2"))
    # End state: some version is in production, the other two were archived.
    prod = (tmp_path / "production" / "intent.onnx").read_bytes()
    assert prod in (b"v1", b"v2")
    archived = sorted(p.read_bytes() for p in (tmp_path / "archive").glob("intent-*.onnx"))
    assert b"v0" in archived
    assert len(archived) == 2
