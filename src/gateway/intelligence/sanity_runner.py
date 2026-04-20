"""offline sanity test runner.

Before a candidate is allowed to enter live shadow validation, we run
it against a fixed labeled set and confirm it clears a per-class
accuracy floor. Rationale: a candidate that scores 90% overall while
totally missing the `child_safety` class should never reach
production — the aggregate metric can mask a critical class blind spot.

The runner is intentionally pluggable:

  * Fixtures live in `sanity_tests/<model>_sanity.json` as
    `{"model_name": ..., "examples": [{"input": ..., "label": ...}]}`.
  * The inference function is supplied by the caller. For text-based
    candidates (intent, safety) it wraps the ONNX session with a
    "prompt"-style string input; for feature-based candidates (schema)
    it transforms the dict input into a feature tensor. Keeping the
    inference function out of this module means sanity runs can be
    exercised in unit tests without requiring ORT.
  * Per-class accuracy gate is configurable via `min_per_class_accuracy`
    (defaults to 0.7 per plan).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


_DEFAULT_FIXTURES_DIR: Path = Path(__file__).parent / "sanity_tests"


@dataclass(frozen=True)
class SanityResult:
    """Outcome of running a candidate against a sanity fixture."""
    passed: bool
    overall_accuracy: float
    per_class_accuracy: dict[str, float]
    per_class_counts: dict[str, int]
    per_class_wrong: dict[str, int]
    failing_classes: list[str] = field(default_factory=list)
    total_examples: int = 0
    error_count: int = 0


class SanityRunner:
    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self._fixtures_dir = fixtures_dir or _DEFAULT_FIXTURES_DIR

    def fixture_path(self, model_name: str) -> Path:
        return self._fixtures_dir / f"{model_name}_sanity.json"

    def load(self, model_name: str) -> list[dict[str, Any]]:
        """Read `<model>_sanity.json` and return the example list.

        Returns an empty list when the fixture file is missing so the
        gate can note the absence and (conservatively) reject the
        candidate — promoting a model whose sanity set hasn't been
        authored yet would be risky.
        """
        path = self.fixture_path(model_name)
        if not path.exists():
            logger.warning("sanity fixture missing: %s", path)
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning("sanity fixture unreadable: %s", path, exc_info=True)
            return []
        examples = data.get("examples") if isinstance(data, dict) else None
        if not isinstance(examples, list):
            return []
        return [ex for ex in examples if isinstance(ex, dict) and "input" in ex and "label" in ex]

    def run(
        self,
        model_name: str,
        infer_fn: Callable[[Any], str],
        *,
        min_per_class_accuracy: float = 0.7,
    ) -> SanityResult:
        """Run `infer_fn` over the sanity fixture for `model_name`.

        `infer_fn(input)` returns the candidate's predicted label for
        that example. Caller's responsibility to set up / load whatever
        ORT session the inference requires — this keeps the runner
        decoupled from model-specific plumbing.
        """
        examples = self.load(model_name)
        if not examples:
            # Empty / missing fixture — gate rejects so production
            # never promotes a candidate whose sanity set is absent.
            return SanityResult(
                passed=False,
                overall_accuracy=0.0,
                per_class_accuracy={},
                per_class_counts={},
                per_class_wrong={},
                failing_classes=["<no fixture>"],
                total_examples=0,
            )

        counts: Counter[str] = Counter()
        correct: Counter[str] = Counter()
        wrong: Counter[str] = Counter()
        errors = 0

        for ex in examples:
            label = str(ex["label"])
            counts[label] += 1
            try:
                predicted = infer_fn(ex["input"])
            except Exception:
                errors += 1
                wrong[label] += 1
                logger.debug("sanity inference raised", exc_info=True)
                continue
            if str(predicted) == label:
                correct[label] += 1
            else:
                wrong[label] += 1

        per_class_accuracy = {
            cls: (correct[cls] / counts[cls]) if counts[cls] > 0 else 0.0
            for cls in counts
        }
        failing = sorted(
            cls for cls, acc in per_class_accuracy.items()
            if acc < float(min_per_class_accuracy)
        )
        total = sum(counts.values())
        overall_correct = sum(correct.values())
        overall_accuracy = overall_correct / total if total else 0.0

        return SanityResult(
            passed=not failing,
            overall_accuracy=round(overall_accuracy, 6),
            per_class_accuracy={k: round(v, 6) for k, v in per_class_accuracy.items()},
            per_class_counts=dict(counts),
            per_class_wrong=dict(wrong),
            failing_classes=failing,
            total_examples=total,
            error_count=errors,
        )
