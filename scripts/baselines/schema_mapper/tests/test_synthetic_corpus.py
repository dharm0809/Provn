"""Unit tests for the synthetic corpus generator's augmentation classes."""
from __future__ import annotations

import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "data"))

from data.synthetic_corpus import (  # noqa: E402
    Variant,
    _to_camel,
    _to_kebab,
    _to_pascal,
    _to_snake,
    _validate_variant,
    aug_key_naming,
    aug_nuisance_inject,
    aug_rename_attack,
    aug_sibling_shuffle,
    aug_streaming_fragment,
    aug_value_perturb,
    generate_variants,
)
from canonical_schema import CANONICAL_LABELS  # noqa: E402


def _seed_variant() -> Variant:
    return Variant(
        raw={
            "id": "chatcmpl-AbCdEfGhIjKl",
            "model": "gpt-4o",
            "choices": [{"message": {"content": "<reply>"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
        labels={
            "id": "response_id",
            "model": "model",
            "choices[0].message.content": "content",
            "choices[0].finish_reason": "finish_reason",
            "usage.prompt_tokens": "prompt_tokens",
            "usage.completion_tokens": "completion_tokens",
            "usage.total_tokens": "total_tokens",
        },
        augmentations_applied=[],
    )


# ── naming-style ──────────────────────────────────────────────────────────────

def test_naming_styles_are_invertible_for_simple_keys():
    assert _to_snake("completionTokens") == "completion_tokens"
    assert _to_camel("completion_tokens") == "completionTokens"
    assert _to_kebab("completionTokens") == "completion-tokens"
    assert _to_pascal("completion_tokens") == "CompletionTokens"


def test_aug_key_naming_renames_keys_and_labels_in_lockstep():
    rng = random.Random(0)
    seed = _seed_variant()
    v = aug_key_naming(seed, rng)
    # Labels are still all canonical; paths still match the data
    assert _validate_variant(v)
    assert all(l in CANONICAL_LABELS for l in v.labels.values())
    assert v.augmentations_applied[0].startswith("key_naming:")


# ── rename attack ─────────────────────────────────────────────────────────────

def test_aug_rename_attack_moves_label_to_new_key():
    rng = random.Random(1)
    seed = _seed_variant()
    table = {"completion_tokens": ["completionTokens", "output_tokens"]}
    v = aug_rename_attack(seed, table, rng)
    assert _validate_variant(v)
    # The original key should no longer exist
    paths = {p for p in v.labels}
    assert "usage.completion_tokens" not in paths
    # The replacement should appear with the same canonical label
    completion_label_paths = [p for p, l in v.labels.items() if l == "completion_tokens"]
    assert len(completion_label_paths) == 1
    assert "usage." in completion_label_paths[0]


# ── value perturbation ────────────────────────────────────────────────────────

def test_aug_value_perturb_preserves_paths_and_labels():
    rng = random.Random(2)
    seed = _seed_variant()
    v = aug_value_perturb(seed, rng)
    assert _validate_variant(v)
    assert v.labels == seed.labels  # path-set unchanged


# ── sibling shuffle ───────────────────────────────────────────────────────────

def test_aug_sibling_shuffle_preserves_label_set():
    rng = random.Random(3)
    seed = _seed_variant()
    v = aug_sibling_shuffle(seed, rng)
    assert _validate_variant(v)
    assert v.labels == seed.labels


# ── nuisance injection ────────────────────────────────────────────────────────

def test_aug_nuisance_adds_unknown_labels_only():
    rng = random.Random(4)
    seed = _seed_variant()
    v = aug_nuisance_inject(seed, rng)
    assert _validate_variant(v)
    assert len(v.labels) > len(seed.labels)
    new_paths = set(v.labels) - set(seed.labels)
    for p in new_paths:
        assert v.labels[p] == "UNKNOWN"


# ── streaming fragment ────────────────────────────────────────────────────────

def test_aug_streaming_fragment_produces_chunk_shape():
    rng = random.Random(5)
    seed = _seed_variant()
    v = aug_streaming_fragment(seed, rng)
    assert _validate_variant(v)
    assert "delta.content" in v.labels
    assert v.labels["delta.content"] == "content"
    assert v.labels["finish_reason"] == "finish_reason"


# ── orchestration ─────────────────────────────────────────────────────────────

def test_generate_variants_produces_valid_corpus():
    seed = _seed_variant()
    rng = random.Random(20260427)
    spec_example = {"raw": seed.raw, "expected_labels": seed.labels}
    variants = generate_variants(spec_example, rename_table={}, n_variants=20, rng=rng)
    # Most variants pass validation (some may be dropped on edge cases)
    assert len(variants) >= 10
    for v in variants:
        assert _validate_variant(v)
        assert v.augmentations_applied  # composed at least one aug
