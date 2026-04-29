"""Validates that `schema_mapper_sanity.json` is well-formed for the new
sanity adapter (PR #21).

The adapter feeds each fixture row through the production featurizer
`gateway.intelligence.distillation.trainers.schema_trainer._featurize_row`,
which constructs a `FlatField` from FlatField-shaped dict rows and calls
`gateway.schema.features.extract_features`.

Tests guard against fixture drift (e.g. a maintainer reverting to the
old DictVectorizer-style flat dicts) — a row missing `path`/`value_type`
silently degrades to a placeholder zero vector and ALL classes look
identical to the model. This file lights up immediately if that
happens.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from gateway.schema.features import FEATURE_DIM, FlatField, extract_features


_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "gateway"
    / "intelligence"
    / "sanity_tests"
    / "schema_mapper_sanity.json"
)

_REQUIRED_FLATFIELD_KEYS = {
    "path",
    "key",
    "value",
    "value_type",
    "depth",
    "parent_key",
    "sibling_keys",
    "sibling_types",
    "int_siblings",
}


def _load_examples() -> list[dict]:
    payload = json.loads(_FIXTURE.read_text())
    assert payload["model_name"] == "schema_mapper"
    return payload["examples"]


def test_fixture_has_minimum_breadth():
    """At least 5 examples per labeled class, ≥3 distinct labels."""
    examples = _load_examples()
    by_label: dict[str, int] = {}
    for ex in examples:
        by_label[ex["label"]] = by_label.get(ex["label"], 0) + 1
    assert len(by_label) >= 3, f"expected ≥3 labels, got {by_label}"
    for label, count in by_label.items():
        assert count >= 5, f"label {label!r} has only {count} examples (need ≥5)"


def test_every_input_is_flatfield_shaped():
    """Each row's input must have ALL FlatField fields — old DictVectorizer
    rows like {len, word_count, has_role, depth} would zero-vector through
    the new featurizer and produce no signal.
    """
    examples = _load_examples()
    for ex in examples:
        inp = ex["input"]
        assert isinstance(inp, dict), f"input must be dict, got {type(inp)}"
        missing = _REQUIRED_FLATFIELD_KEYS - inp.keys()
        assert not missing, (
            f"label={ex['label']!r} row missing FlatField keys: "
            f"{sorted(missing)}; got keys {sorted(inp.keys())}"
        )


def test_extract_features_returns_139d_vector():
    """Round-trip every fixture row through the production featurizer."""
    examples = _load_examples()
    for ex in examples:
        inp = ex["input"]
        field = FlatField(
            path=str(inp["path"]),
            key=str(inp["key"]),
            value=inp["value"],
            value_type=str(inp["value_type"]),
            depth=int(inp["depth"]),
            parent_key=str(inp["parent_key"]),
            sibling_keys=list(inp["sibling_keys"]),
            sibling_types=list(inp["sibling_types"]),
            int_siblings=list(inp["int_siblings"]),
        )
        vec = extract_features(field)
        arr = np.asarray(vec, dtype=np.float32)
        assert arr.shape == (FEATURE_DIM,), (
            f"label={ex['label']!r} produced vec of shape {arr.shape}, "
            f"expected ({FEATURE_DIM},)"
        )
        # Featurizer must produce some signal — a zero vector means the
        # row degraded silently. (Placeholder rows from the trainer
        # _do_ produce non-zero output because depth/structural
        # features are still emitted, but our seeded rows should have
        # both key-name signal AND value signal.)
        assert np.any(arr != 0), (
            f"label={ex['label']!r} produced an all-zero feature vector"
        )


def test_featurize_row_via_trainer_helper():
    """End-to-end: every row should also round-trip through the trainer's
    `_featurize_row` (the same code the sanity adapter calls).
    """
    pytest.importorskip("numpy")
    try:
        from gateway.intelligence.distillation.trainers.schema_trainer import (
            _featurize_row,
        )
    except ImportError:
        pytest.skip("schema_trainer optional deps not installed")

    examples = _load_examples()
    for ex in examples:
        vec = _featurize_row(ex["input"], FEATURE_DIM)
        assert vec.shape == (FEATURE_DIM,), (
            f"label={ex['label']!r} _featurize_row returned shape "
            f"{vec.shape}, expected ({FEATURE_DIM},)"
        )
        assert np.any(vec != 0), (
            f"label={ex['label']!r} _featurize_row returned all-zero vector "
            "(row likely missing required FlatField keys)"
        )
