"""Corpus loader contract — feeds the Phase 4 perfect-coverage gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.schema.corpus import CorpusCase, load_corpus


def test_load_corpus_discovers_seed_fixtures():
    """Smoke: the loader returns every <target>/<variant>.json under the corpus
    root, parses it into a CorpusCase, and produces a stable-sorted list.
    """
    cases = load_corpus()
    assert cases, "seed corpus must not be empty — the gate needs cases"
    targets = sorted({c.target for c in cases})
    # Seed corpus covers the closed-set primary targets. Asserting these
    # exist locks the floor; new targets/variants append to this set
    # rather than replace it.
    for needed in ("openai", "anthropic", "ollama"):
        assert needed in targets, (
            f"seed corpus is missing target {needed!r}; have {targets}"
        )


def test_load_corpus_ids_are_deterministic():
    """Two consecutive load_corpus() calls must return cases in the same order.

    Parametrized pytest ids depend on stable ordering — flaky order breaks
    -k selection and makes CI diffs noisy.
    """
    first = load_corpus()
    second = load_corpus()
    assert [(c.target, c.variant) for c in first] == [
        (c.target, c.variant) for c in second
    ]


def test_load_corpus_raises_on_missing_root(tmp_path):
    missing = tmp_path / "no_such_corpus"
    with pytest.raises(FileNotFoundError, match="schema corpus root not found"):
        load_corpus(missing)


def test_load_corpus_raises_on_empty_root(tmp_path):
    (tmp_path / "openai").mkdir()  # exists, but no .json files
    with pytest.raises(FileNotFoundError, match="schema corpus is empty"):
        load_corpus(tmp_path)


def test_load_corpus_rejects_case_missing_required_key(tmp_path):
    (tmp_path / "openai").mkdir()
    bad = tmp_path / "openai" / "broken.json"
    # Missing "expected"
    bad.write_text(json.dumps({
        "target": "openai", "variant": "broken", "raw": {},
    }))
    with pytest.raises(ValueError, match="missing required key 'expected'"):
        load_corpus(tmp_path)


def test_load_corpus_rejects_empty_expected(tmp_path):
    """An empty expected dict would let a case pass with zero coverage —
    that's a footgun and the loader catches it at collection."""
    (tmp_path / "anthropic").mkdir()
    bad = tmp_path / "anthropic" / "broken.json"
    bad.write_text(json.dumps({
        "target": "anthropic", "variant": "broken",
        "raw": {"id": "x"}, "expected": {},
    }))
    with pytest.raises(ValueError, match="'expected' must be a non-empty dict"):
        load_corpus(tmp_path)


def test_corpus_case_is_frozen():
    cases = load_corpus()
    with pytest.raises((AttributeError, Exception)):
        cases[0].target = "mutated"  # type: ignore[misc]


def test_corpus_case_source_resolves_to_an_existing_file():
    """`source` is the on-disk path — should always resolve so failure
    messages can include a clickable file ref."""
    for c in load_corpus():
        assert isinstance(c.source, Path)
        assert c.source.exists(), f"case source path doesn't exist: {c.source}"
