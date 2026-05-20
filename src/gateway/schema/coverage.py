"""Phase 4 coverage scorer — drives the perfect-score CI gate.

Given a ``CorpusCase``, ``score_case`` runs the production ``SchemaMapper``
over its raw payload and answers two questions:

1. Are every ``expected`` canonical field populated in the resulting
   ``CanonicalResponse``? (coverage_pct == 100.0 when yes.)
2. Did any field fall through to ``overflow``? (overflow_keys empty when no.)

A failure on either is treated as a hard build break in
``tests/production/test_schema_coverage_gate.py``. The remedy is NOT to xfail
the case — it's to extend ``_PROVIDER_PATH_MAP`` (or the path-fallback rules)
until the field maps cleanly. That's the Phase 5 work queue.

Envelope keys (``object``, ``created``, ``role``, ``index``, …) are tagged
via ``_apply_path_fallbacks`` and DO NOT count as overflow — they're
boilerplate, not unmapped data. The mapper already excludes them from
``overflow`` upstream; we surface the same convention here so a case author
doesn't have to enumerate them in ``expected``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gateway.schema.canonical import CanonicalResponse
from gateway.schema.corpus import CorpusCase
from gateway.schema.mapper import SchemaMapper


@dataclass(frozen=True)
class CoverageResult:
    """Outcome of scoring one corpus case.

    ``coverage_pct`` is a percent (0.0–100.0), not a fraction — operators
    read failure messages and "missing 2 of 7 (71.4%)" parses faster than
    "0.714". ``missing_fields`` and ``overflow_keys`` are sorted so failure
    messages diff cleanly between runs.
    """
    coverage_pct: float
    missing_fields: list[str]
    overflow_keys: list[str]
    total_expected: int


def _resolve_canonical_path(cr: CanonicalResponse, path: str) -> Any:
    """Walk a dotted path on ``CanonicalResponse``.

    Supports two shapes:
    - ``"content"`` → ``cr.content``
    - ``"usage.prompt_tokens"`` → ``cr.usage.prompt_tokens``

    Returns ``None`` when any segment can't be resolved — the caller treats
    that as "not populated", which is the right semantic: an expected field
    that the mapper never assigned to is exactly what the gate needs to
    flag.
    """
    obj: Any = cr
    for segment in path.split("."):
        try:
            obj = getattr(obj, segment)
        except AttributeError:
            return None
        if obj is None:
            return None
    return obj


def _is_populated(value: Any) -> bool:
    """Treat ``""``, ``0``, ``[]``, ``{}``, and ``None`` as "not populated".

    These match the ``CanonicalResponse`` default-construction values; a
    field that still holds its default after ``map_response`` ran is, by
    definition, not populated by the mapper.

    Note on ``0``: token counts default to 0 and the mapper writes the
    real count over the default. A case asserting ``usage.prompt_tokens``
    is expected MUST have a non-zero prompt-token count in its ``raw``
    payload — otherwise the assertion is vacuous and would pass for the
    wrong reason (the default value, not the mapper's work). Documented
    in the README rather than silently special-cased here.
    """
    if value is None:
        return False
    if isinstance(value, (str, list, dict)) and not value:
        return False
    if isinstance(value, (int, float)) and value == 0:
        return False
    return True


def score_case(case: CorpusCase, mapper: SchemaMapper | None = None) -> CoverageResult:
    """Map the case's raw payload and score it against ``expected``.

    A shared ``mapper`` instance can be passed in by callers that score
    many cases — constructing a fresh ``SchemaMapper`` loads the ONNX
    session (~tens of ms) and is wasteful when the same session can serve
    every case. Defaults to a fresh instance so unit tests stay simple.
    """
    m = mapper if mapper is not None else SchemaMapper()
    cr = m.map_response(case.raw)

    missing = [
        path
        for path, required in sorted(case.expected.items())
        if required and not _is_populated(_resolve_canonical_path(cr, path))
    ]
    total = sum(1 for required in case.expected.values() if required)
    populated = total - len(missing)
    coverage_pct = (populated / total * 100.0) if total else 100.0

    # ``overflow`` already excludes envelope keys upstream in the mapper —
    # see ``_is_envelope_field`` and ``_apply_path_fallbacks``. We surface
    # whatever survived as the gate's overflow list.
    overflow_keys = sorted(cr.overflow.keys())

    return CoverageResult(
        coverage_pct=coverage_pct,
        missing_fields=missing,
        overflow_keys=overflow_keys,
        total_expected=total,
    )
