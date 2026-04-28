"""Unit tests for the 5 filter-discipline rules in synthesize._validate_variant."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from synthesize import _validate_variant  # noqa: E402


def _seed_raw():
    return {"id": "abc", "usage": {"prompt_tokens": 5, "completion_tokens": 3}}


def test_kept_when_well_formed():
    variant = {
        "raw": {"id": "xyz", "usage": {"prompt_tokens": 8, "completion_tokens": 4}},
        "expected_labels": {
            "id": "response_id",
            "usage.prompt_tokens": "prompt_tokens",
            "usage.completion_tokens": "completion_tokens",
        },
    }
    kept, reason = _validate_variant(variant, _seed_raw())
    assert kept and reason is None


def test_dropped_on_non_canonical_label():
    variant = {
        "raw": {"id": "xyz"},
        "expected_labels": {"id": "RESPONSE_ID_TYPO"},
    }
    kept, reason = _validate_variant(variant, _seed_raw())
    assert not kept and reason == "non_canonical_label"


def test_dropped_on_path_label_mismatch():
    variant = {
        "raw": {"id": "xyz", "usage": {"prompt_tokens": 5}},
        "expected_labels": {
            "id": "response_id",
            # missing usage.prompt_tokens
        },
    }
    kept, reason = _validate_variant(variant, _seed_raw())
    assert not kept and reason == "path_label_mismatch"


def test_dropped_on_trivial_echo():
    variant = {
        "raw": {"prompt_tokens": 5},
        "expected_labels": {"prompt_tokens": "prompt_tokens"},
    }
    # NOTE: this is technically the canonical labelling for the path; the
    # echo rule fires because path string == label string. That's the
    # intent of the rule (don't let the model memorize this exact mapping
    # from path string equality alone). Real provider data has paths like
    # 'usage.prompt_tokens' that don't trip the echo, so this stays a
    # narrow defensive filter.
    kept, reason = _validate_variant(variant, _seed_raw())
    assert not kept and reason == "trivial_echo"


def test_dropped_on_identical_to_seed():
    seed = _seed_raw()
    variant = {
        "raw": seed,
        "expected_labels": {"id": "response_id", "usage.prompt_tokens": "prompt_tokens", "usage.completion_tokens": "completion_tokens"},
    }
    kept, reason = _validate_variant(variant, seed)
    assert not kept and reason == "identical_to_seed"


def test_dropped_on_degenerate_unknown():
    variant = {
        "raw": {f"k{i}": "x" for i in range(10)},
        "expected_labels": {f"k{i}": "UNKNOWN" for i in range(10)},
    }
    kept, reason = _validate_variant(variant, _seed_raw())
    assert not kept and reason == "degenerate_unknown"


def test_malformed_variant_rejected():
    kept, reason = _validate_variant({"only_raw": True}, _seed_raw())
    assert not kept
