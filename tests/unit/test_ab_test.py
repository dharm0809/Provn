"""Unit tests for A/B model testing (B.9)."""
from __future__ import annotations

import pytest

from gateway.routing.ab_test import ABTest, ABVariant, load_ab_tests, resolve_ab_model


# ── ABTest / ABVariant construction ───────────────────────────────────────────

def test_single_variant_always_selected():
    test = ABTest(
        name="t",
        model_pattern="gpt-4*",
        variants=[ABVariant(model="gpt-4", weight=100)],
    )
    for _ in range(20):
        v = test.select_variant()
        assert v.model == "gpt-4"


def test_weighted_selection_distribution():
    """90/10 split: the 90% variant should dominate over 1000 samples."""
    test = ABTest(
        name="t",
        model_pattern="*",
        variants=[
            ABVariant(model="model-a", weight=90),
            ABVariant(model="model-b", weight=10),
        ],
    )
    results = [test.select_variant().model for _ in range(1000)]
    a_count = results.count("model-a")
    # With 90% weight we expect 750–1000 (generous bounds to avoid flakiness).
    assert 750 < a_count < 1000, f"Expected ~900 model-a selections, got {a_count}"


def test_equal_weight_both_variants_selected():
    """50/50 split: both variants must appear across 200 samples."""
    test = ABTest(
        name="t",
        model_pattern="*",
        variants=[
            ABVariant(model="a", weight=1),
            ABVariant(model="b", weight=1),
        ],
    )
    results = {test.select_variant().model for _ in range(200)}
    assert "a" in results
    assert "b" in results


def test_matches_fnmatch():
    test = ABTest(
        name="t",
        model_pattern="qwen3:*",
        variants=[ABVariant("qwen3:4b", 100)],
    )
    assert test.matches("qwen3:1.7b")
    assert test.matches("qwen3:4b")
    assert not test.matches("gpt-4")
    assert not test.matches("gemma3:1b")


def test_matches_case_insensitive():
    test = ABTest(
        name="t",
        model_pattern="GPT-4*",
        variants=[ABVariant("gpt-4", 100)],
    )
    assert test.matches("gpt-4-turbo")
    assert test.matches("GPT-4o")


def test_invalid_weight_raises():
    with pytest.raises(ValueError, match="positive"):
        ABTest(name="t", model_pattern="*", variants=[ABVariant("m", -1)])


def test_zero_weight_raises():
    with pytest.raises(ValueError, match="positive"):
        ABTest(name="t", model_pattern="*", variants=[ABVariant("m", 0)])


def test_empty_variants_raises():
    with pytest.raises(ValueError, match="at least one variant"):
        ABTest(name="t", model_pattern="*", variants=[])


# ── resolve_ab_model ──────────────────────────────────────────────────────────

def test_resolve_ab_model_match():
    tests = [
        ABTest(
            name="size-test",
            model_pattern="qwen3:*",
            variants=[ABVariant("qwen3:4b", 100)],
        )
    ]
    model, test_name = resolve_ab_model("qwen3:1.7b", tests)
    assert model == "qwen3:4b"
    assert test_name == "size-test"


def test_resolve_ab_model_no_match():
    tests = [
        ABTest(
            name="t",
            model_pattern="qwen3:*",
            variants=[ABVariant("qwen3:4b", 100)],
        )
    ]
    model, test_name = resolve_ab_model("gpt-4", tests)
    assert model == "gpt-4"
    assert test_name is None


def test_resolve_ab_model_no_tests():
    model, test_name = resolve_ab_model("any-model", [])
    assert model == "any-model"
    assert test_name is None


def test_multiple_tests_first_match_wins():
    tests = [
        ABTest("t1", "qwen3:*", [ABVariant("qwen3:1.7b", 100)]),
        ABTest("t2", "qwen*", [ABVariant("qwen3:4b", 100)]),
    ]
    model, test_name = resolve_ab_model("qwen3:latest", tests)
    # First test in the list matches → should use t1
    assert test_name == "t1"
    assert model == "qwen3:1.7b"


def test_resolve_preserves_model_when_single_variant_same():
    """If the variant model equals the original, returns same model + test name."""
    tests = [ABTest("t", "gpt*", [ABVariant("gpt-4", 100)])]
    model, test_name = resolve_ab_model("gpt-4", tests)
    assert model == "gpt-4"
    assert test_name == "t"


# ── load_ab_tests ─────────────────────────────────────────────────────────────

def test_load_ab_tests_valid():
    json_str = (
        '[{"name":"test","model_pattern":"qwen3:*",'
        '"variants":[{"model":"qwen3:1.7b","weight":50},{"model":"qwen3:4b","weight":50}]}]'
    )
    tests = load_ab_tests(json_str)
    assert len(tests) == 1
    assert tests[0].name == "test"
    assert tests[0].model_pattern == "qwen3:*"
    assert len(tests[0].variants) == 2
    assert tests[0].variants[0].model == "qwen3:1.7b"
    assert tests[0].variants[1].weight == 50


def test_load_ab_tests_multiple():
    json_str = (
        '[{"name":"t1","model_pattern":"gpt*","variants":[{"model":"gpt-4","weight":100}]},'
        '{"name":"t2","model_pattern":"claude*","variants":[{"model":"claude-3","weight":100}]}]'
    )
    tests = load_ab_tests(json_str)
    assert len(tests) == 2
    assert tests[0].name == "t1"
    assert tests[1].name == "t2"


def test_load_ab_tests_empty_string():
    assert load_ab_tests("") == []


def test_load_ab_tests_empty_array():
    assert load_ab_tests("[]") == []


def test_load_ab_tests_null():
    assert load_ab_tests("null") == []


def test_load_ab_tests_invalid_json_fail_open():
    """Malformed JSON must not raise — fail-open with empty list."""
    tests = load_ab_tests("not-json{{{")
    assert tests == []


def test_load_ab_tests_missing_key_fail_open():
    """Missing required key in variant must not raise — fail-open."""
    tests = load_ab_tests('[{"name":"t","model_pattern":"*","variants":[{"model":"m"}]}]')
    assert tests == []  # weight missing → KeyError → fail-open
