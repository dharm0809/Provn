"""shadow validation metrics + McNemar paired test.

Reads `shadow_comparisons` rows for a given `(model, candidate_version)`,
joins with the verdict log's `divergence_signal` (when available) to get
per-row ground truth, and returns everything Task 24's gate needs to
make a promote/reject decision.

Ground truth semantics
----------------------
A `shadow_comparisons` row matches a verdict row by `(model_name,
input_hash)`. The verdict row's `divergence_signal` (populated by the
Phase D harvesters) is treated as the correct label. Rows without
ground truth don't contribute to accuracy but DO contribute to
disagreement / error rate — the raw signal still tells us when
candidate and production disagree, even if we can't say who's right.

McNemar test
------------
We test whether the two classifiers make the SAME correctness pattern
on the ground-truth subset. For each paired row:

  b = production correct, candidate wrong
  c = production wrong, candidate correct

Using the EXACT binomial formulation (preferred for small n): under
the null hypothesis that the two classifiers are equally good, `b/(b+c)`
is distributed Binomial(n=b+c, p=0.5). `scipy.stats.binomtest` gives
the two-sided p-value. When `b+c == 0` we return `p=1.0` — there's
nothing to distinguish, so the gate should NOT claim statistical
significance.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from gateway.intelligence.db import IntelligenceDB

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowMetrics:
    model_name: str
    candidate_version: str
    sample_count: int                     # total shadow_comparisons rows
    labeled_count: int                    # rows with ground truth available
    candidate_accuracy: float             # on labeled rows (0.0 when labeled_count=0)
    production_accuracy: float            # on labeled rows
    disagreement_rate: float              # across all rows (not just labeled)
    candidate_error_rate: float           # candidate_error IS NOT NULL
    mcnemar_p_value: float                # exact binomial test; 1.0 when no disagreement


def compute_metrics(
    db: IntelligenceDB,
    model_name: str,
    candidate_version: str,
) -> ShadowMetrics:
    """Aggregate shadow_comparisons + verdict ground truth for `candidate_version`."""
    rows = _fetch_rows(db, model_name, candidate_version)
    sample_count = len(rows)
    if sample_count == 0:
        return _empty(model_name, candidate_version)

    # Candidate error rate is computed against ALL rows — a candidate
    # that crashes even without ground truth is a candidate that's
    # going to crash in production.
    error_rows = [r for r in rows if r["candidate_error"] is not None]
    candidate_error_rate = len(error_rows) / sample_count

    # Disagreement rate also spans every row where both predictions are
    # present (candidate_error rows implicitly count as disagreements
    # because we have no candidate prediction to compare).
    disagreement = 0
    for r in rows:
        if r["candidate_error"] is not None:
            disagreement += 1
            continue
        if r["candidate_prediction"] != r["production_prediction"]:
            disagreement += 1
    disagreement_rate = disagreement / sample_count

    labeled = [r for r in rows if r["ground_truth"] is not None and r["candidate_error"] is None]
    labeled_count = len(labeled)
    if labeled_count == 0:
        return ShadowMetrics(
            model_name=model_name,
            candidate_version=candidate_version,
            sample_count=sample_count,
            labeled_count=0,
            candidate_accuracy=0.0,
            production_accuracy=0.0,
            disagreement_rate=round(disagreement_rate, 6),
            candidate_error_rate=round(candidate_error_rate, 6),
            mcnemar_p_value=1.0,
        )

    prod_correct = 0
    cand_correct = 0
    b_prod_only = 0  # production right, candidate wrong
    c_cand_only = 0  # candidate right, production wrong
    for r in labeled:
        gt = r["ground_truth"]
        p_ok = r["production_prediction"] == gt
        c_ok = r["candidate_prediction"] == gt
        if p_ok:
            prod_correct += 1
        if c_ok:
            cand_correct += 1
        if p_ok and not c_ok:
            b_prod_only += 1
        elif c_ok and not p_ok:
            c_cand_only += 1

    production_accuracy = prod_correct / labeled_count
    candidate_accuracy = cand_correct / labeled_count
    mcnemar_p = _mcnemar_exact(b_prod_only, c_cand_only)

    return ShadowMetrics(
        model_name=model_name,
        candidate_version=candidate_version,
        sample_count=sample_count,
        labeled_count=labeled_count,
        candidate_accuracy=round(candidate_accuracy, 6),
        production_accuracy=round(production_accuracy, 6),
        disagreement_rate=round(disagreement_rate, 6),
        candidate_error_rate=round(candidate_error_rate, 6),
        mcnemar_p_value=round(mcnemar_p, 6),
    )


# ── helpers ────────────────────────────────────────────────────────────────


def _empty(model_name: str, candidate_version: str) -> ShadowMetrics:
    return ShadowMetrics(
        model_name=model_name,
        candidate_version=candidate_version,
        sample_count=0,
        labeled_count=0,
        candidate_accuracy=0.0,
        production_accuracy=0.0,
        disagreement_rate=0.0,
        candidate_error_rate=0.0,
        mcnemar_p_value=1.0,
    )


def _fetch_rows(
    db: IntelligenceDB, model_name: str, candidate_version: str,
) -> list[dict[str, Any]]:
    """Left-join shadow_comparisons with onnx_verdicts for ground truth.

    `ground_truth` is the verdict row's `divergence_signal`; when the
    verdict has no divergence (or no matching verdict exists) it stays
    NULL.
    """
    sql = """
        SELECT
            sc.production_prediction,
            sc.candidate_prediction,
            sc.candidate_error,
            (
                SELECT v.divergence_signal
                FROM onnx_verdicts v
                WHERE v.model_name = sc.model_name
                  AND v.input_hash = sc.input_hash
                  AND v.divergence_signal IS NOT NULL
                ORDER BY v.timestamp DESC
                LIMIT 1
            ) AS ground_truth
        FROM shadow_comparisons sc
        WHERE sc.model_name = ?
          AND sc.candidate_version = ?
    """
    conn = sqlite3.connect(db.path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, (model_name, candidate_version)).fetchall()]
    finally:
        conn.close()


def _mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar via `scipy.stats.binomtest`.

    * `b` = production correct, candidate wrong
    * `c` = candidate correct, production wrong
    * `b+c` = total discordant pairs
    Under H0 (classifiers equivalent): min(b,c) ~ Binomial(b+c, 0.5).
    """
    n = b + c
    if n == 0:
        return 1.0
    try:
        from scipy.stats import binomtest
    except ImportError:
        # Scipy is expected to be available but guard anyway — fall
        # back to a conservative "no significance" verdict so the gate
        # doesn't erroneously pass a candidate.
        logger.debug("scipy unavailable; McNemar returns 1.0")
        return 1.0
    try:
        result = binomtest(min(b, c), n, 0.5, alternative="two-sided")
        return float(result.pvalue)
    except Exception:
        logger.debug("binomtest failed; McNemar returns 1.0", exc_info=True)
        return 1.0
