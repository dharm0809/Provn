"""Schema corpus loader — closed-set fixture cases for the Phase 4 gate.

The corpus is the source of truth for "what canonical fields the deterministic
map must populate for every supported provider × variant." Each case lives at
``tests/fixtures/schema_corpus/<target>/<variant>.json`` with this shape:

    {
      "target": "openai",
      "variant": "nonstream_basic",
      "raw": { ...the exact provider response JSON... },
      "expected": {
        "content": true,
        "finish_reason": true,
        "usage.prompt_tokens": true,
        ...
      }
    }

``expected`` lists the canonical-response field paths that MUST be populated
when ``SchemaMapper.map_response(raw)`` runs over ``raw``. Adding a case is
the way to lock in the deterministic map's current behavior; the Phase 4 gate
(``tests/production/test_schema_coverage_gate.py``) fails the build on any
regression.

Adding new cases — DO read ``docs/plans/2026-05-16-schema-mapping-perfect-score.md``
first. New cases should be derived from REAL provider responses (Phase 8
capture harness), not synthesized — synthetic samples drift from real shapes
silently.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CORPUS_ROOT = (
    Path(__file__).resolve().parents[3]
    / "tests" / "fixtures" / "schema_corpus"
)


@dataclass(frozen=True)
class CorpusCase:
    """One fixture case loaded from disk.

    Frozen so callers (parametrized tests) can use it as a dict key
    or a pytest id without surprise mutation.
    """
    target: str
    variant: str
    raw: dict[str, Any]
    # Canonical field paths the mapper MUST populate for this case.
    # Dotted paths address nested fields on ``CanonicalResponse``
    # (e.g. ``"usage.prompt_tokens"``); a bare key addresses a top-level
    # attribute (e.g. ``"content"``).
    expected: dict[str, bool]
    # Original path on disk — surfaces in failure messages so an
    # operator can jump straight to the offending fixture.
    source: Path


def load_corpus(root: Path | None = None) -> list[CorpusCase]:
    """Discover every ``<target>/<variant>.json`` under the corpus root.

    Returns a stable-sorted list (by ``target/variant``) so parametrized
    test ids are deterministic — flaky id order breaks pytest's
    ``-k`` selection and makes CI diffs noisy.

    Empty corpus is a hard error: the gate exists to PREVENT regressions,
    and an empty corpus would let any PR pass with no signal. The check
    is intentional, not defensive.
    """
    base = root if root is not None else _CORPUS_ROOT
    if not base.is_dir():
        raise FileNotFoundError(
            f"schema corpus root not found: {base}. The Phase 4 gate "
            f"requires at least one case; see {base.parent}/README.md."
        )

    cases: list[CorpusCase] = []
    for case_path in sorted(base.glob("*/*.json")):
        payload = json.loads(case_path.read_text())
        # Validate the case here, not at score time, so a malformed
        # fixture fails loudly during collection (with the path), not
        # mid-parametrize with a cryptic KeyError on the case.
        for key in ("target", "variant", "raw", "expected"):
            if key not in payload:
                raise ValueError(
                    f"corpus case {case_path} missing required key {key!r}"
                )
        if not isinstance(payload["expected"], dict) or not payload["expected"]:
            raise ValueError(
                f"corpus case {case_path}: 'expected' must be a non-empty "
                f"dict of canonical-field-path → True. An empty expectation "
                f"set would let the case pass with zero coverage."
            )
        cases.append(CorpusCase(
            target=str(payload["target"]),
            variant=str(payload["variant"]),
            raw=dict(payload["raw"]),
            expected=dict(payload["expected"]),
            source=case_path,
        ))

    if not cases:
        raise FileNotFoundError(
            f"schema corpus is empty under {base}; the Phase 4 gate "
            f"needs at least one fixture to catch regressions."
        )
    return cases
