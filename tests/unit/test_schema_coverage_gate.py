"""PERFECT-SCORE GATE — Phase 4 of docs/plans/2026-05-16-schema-mapping-perfect-score.md.

Definition of done for the mapping-coverage phases. Fails the build if ANY
closed-set corpus case is < 100% coverage or produces overflow.

DO NOT xfail/skip individual cases to make this pass. Extend the
deterministic ``_PROVIDER_PATH_MAP`` (``src/gateway/schema/mapper.py:146``) or
the path-fallback rules instead. If a case is genuinely invalid, delete it
from the corpus — a skipped case is silent and silence is the failure mode
this gate exists to prevent.

The corpus lives at ``tests/fixtures/schema_corpus/``. See its README for
authoring rules.
"""
from __future__ import annotations

import pytest

from gateway.schema.corpus import load_corpus
from gateway.schema.coverage import score_case
from gateway.schema.mapper import SchemaMapper

# A single mapper instance amortizes ONNX session construction across cases
# (constructing one per case adds ~tens of ms × N cases to the CI test job).
# Module-scoped so pytest tears it down only after every parametrized case
# has run.
_CORPUS = load_corpus()


@pytest.fixture(scope="module")
def shared_mapper() -> SchemaMapper:
    mapper = SchemaMapper()
    if mapper._session is None:
        pytest.skip("ONNX session unavailable; coverage gate requires a loaded model")
    return mapper


@pytest.mark.parametrize(
    "case",
    _CORPUS,
    ids=lambda c: f"{c.target}/{c.variant}",
)
def test_perfect_coverage_and_zero_overflow(case, shared_mapper):
    result = score_case(case, mapper=shared_mapper)
    assert result.coverage_pct == 100.0, (
        f"{case.target}/{case.variant} ({case.source.name}): "
        f"coverage {result.coverage_pct:.1f}% — missing {result.missing_fields}. "
        f"Extend _PROVIDER_PATH_MAP in src/gateway/schema/mapper.py to cover these."
    )
    assert result.overflow_keys == [], (
        f"{case.target}/{case.variant} ({case.source.name}): "
        f"overflow keys present {result.overflow_keys}. The deterministic "
        f"map should classify these, not leave them in overflow."
    )
