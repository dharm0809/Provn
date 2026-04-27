"""Regression test for DeepSeek-Reasoner / xAI Grok co-existence of
content + thinking_content under the SAME parent object.

Loads the actual provider-spec fixtures and asserts the architectural
invariant: under a reasoning-model parent dict, both labels are valid
simultaneously. Pairs with the EXCLUSIVE_GROUPS removal of
`primary_content` and the COOCCUR_BIAS positive prior.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from canonical_schema import COOCCUR_BIAS, EXCLUSIVE_GROUPS  # noqa: E402
from paths import parent_object_path  # noqa: E402

SPECS_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "provider_specs"


def _find_reasoner_example(spec_path: pathlib.Path) -> tuple[str, str, dict]:
    """Return (path_with_content, path_with_thinking, expected_labels) for the
    spec example whose expected_labels contain both `content` and
    `thinking_content` under the same parent object."""
    spec = json.loads(spec_path.read_text())
    for ex in spec["examples"]:
        labels = ex["expected_labels"]
        content_paths = [p for p, l in labels.items() if l == "content"]
        thinking_paths = [p for p, l in labels.items() if l == "thinking_content"]
        for cp in content_paths:
            for tp in thinking_paths:
                if parent_object_path(cp) == parent_object_path(tp):
                    return cp, tp, labels
    pytest.skip(f"{spec_path.name}: no example with co-located content+thinking")


def test_deepseek_reasoner_content_and_thinking_coexist_under_same_parent():
    cp, tp, labels = _find_reasoner_example(SPECS_DIR / "deepseek.json")
    assert parent_object_path(cp) == parent_object_path(tp)
    assert labels[cp] == "content"
    assert labels[tp] == "thinking_content"


def test_xai_grok_content_and_thinking_coexist_under_same_parent():
    cp, tp, labels = _find_reasoner_example(SPECS_DIR / "xai_grok.json")
    assert parent_object_path(cp) == parent_object_path(tp)
    assert labels[cp] == "content"
    assert labels[tp] == "thinking_content"


def test_no_exclusive_group_blocks_content_thinking_coexistence():
    """Belt-and-braces — even if a future commit re-introduces a group,
    no group can contain both `content` and `thinking_content` together."""
    for group_name, members in EXCLUSIVE_GROUPS.items():
        s = set(members)
        assert not (
            "content" in s and "thinking_content" in s
        ), f"EXCLUSIVE_GROUPS[{group_name!r}] would forbid the canonical reasoner pair"


def test_content_thinking_cooccur_prior_is_positive():
    """The pair lives in COOCCUR_BIAS as a positive transition prior."""
    found = False
    for a, b, w in COOCCUR_BIAS:
        if {a, b} == {"content", "thinking_content"}:
            assert w > 0.0, f"prior weight must be positive, got {w}"
            found = True
    assert found, "missing (content, thinking_content) in COOCCUR_BIAS"
