"""A/B model testing — weighted random traffic splitting.

Usage:
  WALACOR_AB_TESTS_JSON='[
    {
      "name": "qwen-size-test",
      "model_pattern": "qwen3:*",
      "variants": [
        {"model": "qwen3:1.7b", "weight": 50},
        {"model": "qwen3:4b",   "weight": 50}
      ]
    }
  ]'

When a request matches ``model_pattern``, the gateway randomly selects a
variant according to the weights and rewrites the model field in the request.
The selected test name is stored in execution record metadata as ``ab_variant``.
The original requested model is stored as ``ab_original_model``.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import random
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ABVariant:
    model: str
    weight: int  # relative weight (does not need to sum to 100)


@dataclass
class ABTest:
    name: str
    model_pattern: str  # fnmatch pattern matched against requested model_id
    variants: list[ABVariant] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.variants:
            raise ValueError(f"ABTest '{self.name}' must have at least one variant")
        if any(v.weight <= 0 for v in self.variants):
            raise ValueError(f"ABTest '{self.name}' all variant weights must be positive")

    def select_variant(self) -> ABVariant:
        """Weighted random selection — O(n) but n is tiny (2–5 variants)."""
        total = sum(v.weight for v in self.variants)
        r = random.uniform(0, total)
        cumulative = 0.0
        for variant in self.variants:
            cumulative += variant.weight
            if r <= cumulative:
                return variant
        return self.variants[-1]  # unreachable in normal operation; guards float rounding

    def matches(self, model_id: str) -> bool:
        """Return True if model_id matches this test's fnmatch pattern (case-insensitive)."""
        return fnmatch.fnmatch(model_id.lower(), self.model_pattern.lower())


def load_ab_tests(json_config: str) -> list[ABTest]:
    """Parse A/B test definitions from a JSON string.

    Returns an empty list (fail-open) on any parse error so a misconfigured
    env var never breaks the gateway.
    """
    if not json_config or json_config.strip() in ("", "[]", "null"):
        return []
    try:
        raw = json.loads(json_config)
        tests: list[ABTest] = []
        for item in raw:
            variants = [
                ABVariant(model=str(v["model"]), weight=int(v["weight"]))
                for v in item["variants"]
            ]
            tests.append(ABTest(
                name=str(item["name"]),
                model_pattern=str(item["model_pattern"]),
                variants=variants,
            ))
        return tests
    except Exception as exc:
        logger.warning("Failed to parse WALACOR_AB_TESTS_JSON: %s", exc)
        return []


def resolve_ab_model(model_id: str, ab_tests: list[ABTest]) -> tuple[str, str | None]:
    """Return ``(resolved_model_id, test_name)`` for the first matching A/B test.

    If no test matches, returns ``(model_id, None)`` unchanged.
    First-match semantics mean test order in the JSON array is meaningful.
    """
    for test in ab_tests:
        if test.matches(model_id):
            variant = test.select_variant()
            if variant.model != model_id:
                logger.debug(
                    "A/B test '%s': %s → %s", test.name, model_id, variant.model
                )
            return variant.model, test.name
    return model_id, None
