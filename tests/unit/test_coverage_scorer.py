"""Coverage scorer contract — drives the Phase 4 perfect-coverage gate."""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway.schema.corpus import CorpusCase
from gateway.schema.coverage import (
    CoverageResult,
    _is_populated,
    _resolve_canonical_path,
    score_case,
)


# ── Pure-function unit tests (no mapper construction) ──────────────────


def test_is_populated_default_values():
    """The defaults on CanonicalResponse/CanonicalUsage must read as
    'not populated' — otherwise the gate would pass cases where the
    mapper never assigned anything."""
    assert not _is_populated(None)
    assert not _is_populated("")
    assert not _is_populated(0)
    assert not _is_populated(0.0)
    assert not _is_populated([])
    assert not _is_populated({})


def test_is_populated_real_values():
    assert _is_populated("a")
    assert _is_populated(1)
    assert _is_populated(0.5)
    assert _is_populated([0])
    assert _is_populated({"k": "v"})


def test_resolve_canonical_path_nested():
    """Dotted paths walk attributes; missing segments return None so the
    caller can treat them uniformly as 'not populated'."""
    from gateway.schema.canonical import CanonicalResponse, CanonicalUsage

    cr = CanonicalResponse(content="hi", usage=CanonicalUsage(prompt_tokens=5))
    assert _resolve_canonical_path(cr, "content") == "hi"
    assert _resolve_canonical_path(cr, "usage.prompt_tokens") == 5
    # Missing field
    assert _resolve_canonical_path(cr, "no_such_field") is None
    # Path through a None: ``timing`` is None by default → None.
    assert _resolve_canonical_path(cr, "timing.value") is None


# ── score_case integration tests (require ONNX session) ────────────────


def _synthetic_case(raw: dict, expected: dict) -> CorpusCase:
    return CorpusCase(
        target="synthetic",
        variant="test",
        raw=raw,
        expected=expected,
        source=Path("/dev/null"),
    )


def test_score_case_full_coverage_on_openai_basic():
    """The seed OpenAI basic fixture is the floor — if this drops below
    100% we've regressed the deterministic map."""
    from gateway.schema.mapper import SchemaMapper

    mapper = SchemaMapper()
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    case = _synthetic_case(
        raw={
            "id": "chatcmpl-x",
            "model": "gpt-4o-mini",
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        },
        expected={
            "content": True,
            "finish_reason": True,
            "response_id": True,
            "model": True,
            "usage.prompt_tokens": True,
            "usage.completion_tokens": True,
            "usage.total_tokens": True,
        },
    )
    result = score_case(case, mapper=mapper)
    assert isinstance(result, CoverageResult)
    assert result.coverage_pct == 100.0, (
        f"missing fields: {result.missing_fields}"
    )
    assert result.total_expected == 7


def test_score_case_missing_field_is_reported_by_path():
    """A case that expects a field the mapper doesn't populate must show
    exactly that field in missing_fields — operators read this to know
    where to extend _PROVIDER_PATH_MAP."""
    from gateway.schema.mapper import SchemaMapper

    mapper = SchemaMapper()
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    # OpenAI-shaped response but missing the usage block entirely.
    # `usage.completion_tokens` must surface as missing.
    case = _synthetic_case(
        raw={
            "id": "x",
            "model": "gpt-4o",
            "choices": [{
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
        },
        expected={
            "content": True,
            "usage.completion_tokens": True,  # not provided → must be missing
        },
    )
    result = score_case(case, mapper=mapper)
    assert "usage.completion_tokens" in result.missing_fields
    assert result.coverage_pct < 100.0


def test_score_case_overflow_keys_sorted():
    """Overflow keys come from a dict so order is insertion-dependent;
    we sort for stable failure messages."""
    from gateway.schema.mapper import SchemaMapper

    mapper = SchemaMapper()
    if mapper._session is None:
        pytest.skip("ONNX session unavailable")

    case = _synthetic_case(
        raw={"choices": [{"message": {"content": "ok"}}]},
        expected={"content": True},
    )
    result = score_case(case, mapper=mapper)
    assert result.overflow_keys == sorted(result.overflow_keys)
