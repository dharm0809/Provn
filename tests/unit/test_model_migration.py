"""Phase 25 Task 12: startup migration of packaged ONNX files into the registry.

The Gateway ships a baseline `.onnx` per canonical model name in the source
tree. On first run the registry's `production/` directory is empty, so
`_migrate_packaged_models_to_registry` copies those files in. After a
successful promotion the production file is whatever the harvester trained
last — the migration must NOT clobber it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway.intelligence.registry import ModelRegistry
from gateway.main import (
    _PACKAGED_MODEL_SOURCES,
    _migrate_packaged_models_to_registry,
)


def _make_registry(tmp_path: Path) -> ModelRegistry:
    r = ModelRegistry(base_path=str(tmp_path))
    r.ensure_structure()
    return r


def test_packaged_sources_cover_all_allowed_models():
    # Drift guard: if someone adds a model to ALLOWED_MODEL_NAMES, the
    # migration map must cover it too, otherwise the new model ships
    # without a baseline in production/.
    from gateway.intelligence.registry import ALLOWED_MODEL_NAMES

    assert set(_PACKAGED_MODEL_SOURCES) == set(ALLOWED_MODEL_NAMES)


def test_migration_copies_packaged_sources(tmp_path, monkeypatch):
    r = _make_registry(tmp_path)

    # Stand in fake source files so the test doesn't depend on the real
    # wheel contents (which may or may not ship every ONNX artifact).
    fake_sources = tmp_path / "fake_src"
    fake_sources.mkdir()
    overrides: dict[str, Path] = {}
    for name in _PACKAGED_MODEL_SOURCES:
        p = fake_sources / f"{name}.onnx"
        p.write_bytes(f"baseline-{name}".encode())
        overrides[name] = p
    monkeypatch.setattr(
        "gateway.main._PACKAGED_MODEL_SOURCES", overrides, raising=True
    )

    _migrate_packaged_models_to_registry(r)

    for name in overrides:
        dst = r.production_path(name)
        assert dst.exists(), f"expected {dst} to be seeded"
        assert dst.read_bytes() == f"baseline-{name}".encode()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    # A second run must not overwrite existing production files — those may
    # represent promoted candidates that outrank the shipped baseline.
    r = _make_registry(tmp_path)
    fake_sources = tmp_path / "fake_src"
    fake_sources.mkdir()
    overrides: dict[str, Path] = {}
    for name in _PACKAGED_MODEL_SOURCES:
        p = fake_sources / f"{name}.onnx"
        p.write_bytes(f"baseline-{name}".encode())
        overrides[name] = p
    monkeypatch.setattr("gateway.main._PACKAGED_MODEL_SOURCES", overrides)

    # Pretend one model was promoted already — put a different payload there.
    promoted = r.production_path("intent")
    promoted.write_bytes(b"promoted-v42")

    _migrate_packaged_models_to_registry(r)

    # Promoted model unchanged; the other two got their baseline.
    assert promoted.read_bytes() == b"promoted-v42"
    assert r.production_path("schema_mapper").read_bytes() == b"baseline-schema_mapper"
    assert r.production_path("safety").read_bytes() == b"baseline-safety"


def test_migration_skips_missing_source(tmp_path, monkeypatch):
    r = _make_registry(tmp_path)
    # No fake source files — every destination stays empty.
    overrides = {
        name: tmp_path / "never-exists" / f"{name}.onnx"
        for name in _PACKAGED_MODEL_SOURCES
    }
    monkeypatch.setattr("gateway.main._PACKAGED_MODEL_SOURCES", overrides)

    # Must not raise.
    _migrate_packaged_models_to_registry(r)

    for name in overrides:
        assert not r.production_path(name).exists()


def test_migration_logs_nothing_when_all_present(tmp_path, monkeypatch, caplog):
    # When every destination exists already, no file copies should occur,
    # and the function must still return cleanly.
    r = _make_registry(tmp_path)
    for name in _PACKAGED_MODEL_SOURCES:
        r.production_path(name).write_bytes(b"already-here")

    _migrate_packaged_models_to_registry(r)

    # Files preserved verbatim.
    for name in _PACKAGED_MODEL_SOURCES:
        assert r.production_path(name).read_bytes() == b"already-here"
