"""training dataset builder.

Pulls divergent verdict rows, dedupes them, caps per-session
contribution to prevent a single noisy session from skewing the
distillation target, and class-balances the majority/minority split.
The output is what every trainer in Task 18/19 consumes.

Design decisions
----------------
* The builder takes a model name, not a list of row ids — the worker
  always wants "everything divergent since the last snapshot for this
  model", and encoding that in one place is simpler than plumbing the
  query through every trainer.
* Text-based models (intent, safety) require `training_text` to be
  populated by the harvester when it wrote the divergence signal.
  Feature-based `schema_mapper` uses `input_features_json` directly —
  its features are numeric and already serialized at verdict time.
* Per-session cap is applied AFTER dedupe but BEFORE class balance, so
  adversarial sessions can't dodge the cap by flooding a minority
  class.
* Class balance caps the majority at `2 × minority_count` to keep the
  trainer from learning a trivial "always predict majority" solution.
  Asymmetric caps are intentional — we don't upsample the minority.
* `row_ids` are returned in order so the caller can fingerprint the
  training snapshot (Task 20) and later mark the snapshot used.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any

from gateway.intelligence.db import IntelligenceDB

logger = logging.getLogger(__name__)


# Models whose training input lives in `training_text` — the trainer
# tokenizes strings. Anything else uses `input_features_json`.
_TEXT_BASED_MODELS: frozenset[str] = frozenset({"intent", "safety"})


@dataclass
class TrainingDataset:
    """What the builder returns.

    `X` / `y` parallel lists (one entry per training example).
    `row_ids` — source verdict ids, used by Task 20 to produce a dataset
    fingerprint and record a `training_snapshots` row so the next cycle
    doesn't re-train on the same data.
    """
    X: list[str] = field(default_factory=list)
    y: list[str] = field(default_factory=list)
    row_ids: list[int] = field(default_factory=list)


class DatasetBuilder:
    def __init__(
        self,
        db: IntelligenceDB,
        *,
        per_session_cap_ratio: float = 0.1,
        majority_cap_multiplier: int = 2,
    ) -> None:
        self._db = db
        self._per_session_cap_ratio = float(per_session_cap_ratio)
        self._majority_cap_multiplier = int(majority_cap_multiplier)

    def build(
        self,
        model_name: str,
        *,
        since_timestamp: str | None = None,
        min_samples: int = 0,
    ) -> TrainingDataset:
        """Produce a deduped, capped, balanced training set for `model_name`.

        Returns an empty `TrainingDataset` when the resulting set has
        fewer than `min_samples` rows (the worker uses this to decide
        whether to skip a training cycle).
        """
        rows = self._query_rows(model_name, since_timestamp)
        if not rows:
            return TrainingDataset()

        rows = _dedupe_by_hash(rows)
        rows = _apply_per_session_cap(rows, self._per_session_cap_ratio)
        rows = _apply_class_balance(rows, self._majority_cap_multiplier)

        if len(rows) < min_samples:
            return TrainingDataset()

        return TrainingDataset(
            X=[r["x"] for r in rows],
            y=[r["y"] for r in rows],
            row_ids=[r["id"] for r in rows],
        )

    def _query_rows(
        self, model_name: str, since_timestamp: str | None,
    ) -> list[dict[str, Any]]:
        """Pull eligible rows for `model_name`.

        Text-based models require `training_text IS NOT NULL`; feature-
        based models fall back to `input_features_json` (which is always
        present because it's stored at verdict time).
        """
        text_col = "training_text" if model_name in _TEXT_BASED_MODELS else "input_features_json"
        # Use the correct column in WHERE too — skip rows that have no
        # usable training input (NULL or empty-JSON '{}').
        where_clauses = [
            "model_name = ?",
            "divergence_signal IS NOT NULL",
            "request_id IS NOT NULL",
        ]
        params: list[Any] = [model_name]
        if model_name in _TEXT_BASED_MODELS:
            where_clauses.append("training_text IS NOT NULL AND training_text != ''")
        if since_timestamp:
            where_clauses.append("timestamp >= ?")
            params.append(since_timestamp)

        sql = (
            f"SELECT id, request_id, input_hash, {text_col} AS x, divergence_signal AS y, timestamp "
            f"FROM onnx_verdicts "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY timestamp ASC, id ASC"
        )
        conn = sqlite3.connect(self._db.path)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def _dedupe_by_hash(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the earliest row per `input_hash`.

    Ordering is insertion-stable — `OrderedDict.setdefault` doesn't
    overwrite, so the first-seen row wins.
    """
    seen: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for r in rows:
        seen.setdefault(r["input_hash"], r)
    return list(seen.values())


def _session_of(row: dict[str, Any]) -> str:
    """Derive a session identifier from `request_id`.

    Verdict rows don't carry an explicit session_id — we use the
    request_id's leading component up to the first `-` as a proxy. For
    real traffic the `request_id` is a UUID (no hyphen split is
    meaningful) so each real request maps to its own "session" and the
    per-session cap effectively becomes a per-request cap — still a
    reasonable adversarial-robustness bound (one request can't
    dominate, either).
    """
    rid = row.get("request_id") or ""
    return rid.rsplit("-", 1)[0] if "-" in rid else rid


def _apply_per_session_cap(
    rows: list[dict[str, Any]], ratio: float,
) -> list[dict[str, Any]]:
    """Cap each session's contribution to `ceil(ratio * total)` rows.

    Walks the rows in insertion order so per-session prefixes are kept
    deterministically (first-seen rows survive the cap).
    """
    if ratio >= 1.0 or ratio <= 0.0 or not rows:
        return rows
    cap = max(1, math.ceil(len(rows) * ratio))
    counts: Counter = Counter()
    kept: list[dict[str, Any]] = []
    for r in rows:
        sess = _session_of(r)
        if counts[sess] >= cap:
            continue
        counts[sess] += 1
        kept.append(r)
    return kept


def _apply_class_balance(
    rows: list[dict[str, Any]], majority_cap_multiplier: int,
) -> list[dict[str, Any]]:
    """Cap each class at `minority_count × multiplier`.

    If there's only one class we skip — a trainer shouldn't be called
    on a single-class dataset anyway, but the builder doesn't need to
    raise here; the caller's `min_samples` gate catches degenerate sets.
    """
    if not rows or majority_cap_multiplier <= 0:
        return rows
    counts = Counter(r["y"] for r in rows)
    if len(counts) < 2:
        return rows
    minority = min(counts.values())
    per_class_cap = max(1, minority * majority_cap_multiplier)
    seen: "defaultdict[str, int]" = defaultdict(int)
    kept: list[dict[str, Any]] = []
    for r in rows:
        label = r["y"]
        if seen[label] >= per_class_cap:
            continue
        seen[label] += 1
        kept.append(r)
    return kept
