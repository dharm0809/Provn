"""Round-trip every provider_specs/*.json against the canonical labels +
the flatten_json convention.

This is the discipline-enforcing test for Phase 2: every leaf path in
each spec must have an `expected_labels` entry (and vice versa), and
every label must be in CANONICAL_LABELS.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from canonical_schema import CANONICAL_LABELS  # noqa: E402
from paths import flatten_json  # noqa: E402

SPECS_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "provider_specs"


def _spec_paths() -> list[pathlib.Path]:
    return sorted(SPECS_DIR.glob("*.json"))


@pytest.mark.parametrize("spec_path", _spec_paths(), ids=lambda p: p.stem)
def test_every_leaf_labelled_and_no_orphan_labels(spec_path: pathlib.Path):
    spec = json.loads(spec_path.read_text())
    assert "examples" in spec, f"{spec_path.name}: missing 'examples'"
    for i, ex in enumerate(spec["examples"]):
        actual_paths = {f.path for f in flatten_json(ex["raw"])}
        labelled_paths = set(ex["expected_labels"])
        unlabelled = actual_paths - labelled_paths
        orphan = labelled_paths - actual_paths
        assert not unlabelled, (
            f"{spec_path.name} example[{i}]: unlabelled paths {sorted(unlabelled)}"
        )
        assert not orphan, (
            f"{spec_path.name} example[{i}]: orphan labels {sorted(orphan)}"
        )


@pytest.mark.parametrize("spec_path", _spec_paths(), ids=lambda p: p.stem)
def test_every_label_is_canonical(spec_path: pathlib.Path):
    spec = json.loads(spec_path.read_text())
    for i, ex in enumerate(spec["examples"]):
        for path, label in ex["expected_labels"].items():
            assert label in CANONICAL_LABELS, (
                f"{spec_path.name} example[{i}] {path}: non-canonical label {label!r}"
            )


def test_at_least_three_specs():
    # Phase 2 mid-checkpoint must have at least 3; final must have ≥ 22.
    assert len(_spec_paths()) >= 3
