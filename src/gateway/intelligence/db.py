"""SQLite store for the Phase 25 self-learning intelligence layer.

Holds three tables: `onnx_verdicts` (per-inference predictions), `shadow_comparisons`
(candidate vs. production side-by-side), and `training_snapshots` (dataset fingerprints).

Connections are opened in **autocommit mode** (`isolation_level=None`) — the `with`
statement around `_connect()` does NOT provide transaction rollback on exception.
Callers that need atomic multi-statement writes (e.g., batch verdict flushes) must
issue an explicit `BEGIN IMMEDIATE` / `COMMIT` themselves.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class AccuracySnapshot:
    """Rolling-window accuracy reading for a model + version.

    `accuracy` and `coverage` are independent: a model can have 100%
    accuracy on the rows that *do* have a `divergence_signal` while
    `coverage` is 0.05 because only 5% of verdicts ever get a teacher
    label. Drift / post-promotion validators must read both — a high
    accuracy with low coverage is statistically meaningless.
    """
    model: str
    version: str | None
    sample_count: int       # rows with a usable ground-truth signal
    total_rows: int         # all verdict rows in the window
    accuracy: float         # 0.0 .. 1.0; 0.0 when sample_count == 0
    coverage: float         # sample_count / total_rows; 0.0 when total_rows == 0
    window_start: datetime
    window_end: datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS onnx_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_features_json TEXT NOT NULL,
    prediction TEXT NOT NULL,
    confidence REAL NOT NULL,
    request_id TEXT,
    timestamp TEXT NOT NULL,
    divergence_signal TEXT,
    divergence_source TEXT,
    training_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_model_time ON onnx_verdicts(model_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_verdicts_divergence ON onnx_verdicts(divergence_signal, model_name) WHERE divergence_signal IS NOT NULL;

CREATE TABLE IF NOT EXISTS shadow_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    production_prediction TEXT NOT NULL,
    production_confidence REAL NOT NULL,
    candidate_prediction TEXT,
    candidate_confidence REAL,
    candidate_error TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_model_version ON shadow_comparisons(model_name, candidate_version);

CREATE TABLE IF NOT EXISTS training_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    dataset_hash TEXT NOT NULL UNIQUE,
    row_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lifecycle_events_mirror (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    walacor_record_id TEXT,
    write_status TEXT NOT NULL,
    error_reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    written_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_mirror_type_time
    ON lifecycle_events_mirror(event_type, written_at);
"""


class IntelligenceDB:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # migrate pre-existing DBs to the post-Task-17
            # layout. `ALTER TABLE ... ADD COLUMN` is the most compatible
            # migration path (no data movement). Wrap in try/except so a
            # second startup, where the column already exists, is a no-op.
            try:
                conn.execute("ALTER TABLE onnx_verdicts ADD COLUMN training_text TEXT")
            except sqlite3.OperationalError:
                # Column already present — expected on every run after the
                # first, not an error.
                pass
            # Phase 25 hardening — production model version stamped onto
            # each verdict so post-promotion validation and drift can
            # filter precisely instead of approximating with a
            # promoted_at timestamp window. Pre-existing rows keep
            # NULL — they're treated as "version unknown" by callers.
            try:
                conn.execute("ALTER TABLE onnx_verdicts ADD COLUMN version TEXT")
            except sqlite3.OperationalError:
                pass

    def accuracy_in_window(
        self,
        model: str,
        *,
        version: str | None = None,
        start: datetime,
        end: datetime,
    ) -> AccuracySnapshot:
        """Compute rolling accuracy for `model` against `divergence_signal`.

        Definition of correct: a verdict row whose `divergence_signal` is
        non-null AND `prediction == divergence_signal`. Rows without a
        signal are excluded from the accuracy calculation but still count
        toward `coverage` (the denominator).

        `version` filter is by `prediction` — that's the closest column we
        have to "which model version emitted this." When None, the window
        aggregates across all versions of the model.

        SQLite stores the timestamp as ISO-8601; `datetime.isoformat()`
        sorts lexicographically the same as time-order, so a string range
        query is correct without parsing.
        """
        where = ["model_name = ?", "timestamp >= ?", "timestamp < ?"]
        args: list = [model, start.isoformat(), end.isoformat()]
        if version is not None:
            # Pre-migration rows (no version) intentionally excluded
            # when a caller asks for a specific version — the validator
            # MUST NOT count old-version verdicts toward the new
            # version's accuracy. Callers that want all versions
            # aggregate by passing version=None.
            where.append("version = ?")
            args.append(version)
        sql = (
            "SELECT prediction, divergence_signal "
            "FROM onnx_verdicts "
            "WHERE " + " AND ".join(where)
        )
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()

        total = len(rows)
        with_signal = 0
        correct = 0
        for pred, sig in rows:
            if sig is None:
                continue
            with_signal += 1
            if pred == sig:
                correct += 1

        return AccuracySnapshot(
            model=model,
            version=version,
            sample_count=with_signal,
            total_rows=total,
            accuracy=(correct / with_signal) if with_signal else 0.0,
            coverage=(with_signal / total) if total else 0.0,
            window_start=start,
            window_end=end,
        )

    def write_lifecycle_event(
        self,
        event: "Any",
        *,
        walacor_id: str | None = None,
        status: str = "local_only",
        error_reason: str | None = None,
        attempts: int = 1,
    ) -> int:
        """INSERT a lifecycle event row into the local mirror, unconditionally.

        Local persistence is the audit-trail invariant — every emitted
        event must land here even when no remote walacor writer is
        wired. The optional `LifecycleEventWriter` handles the remote
        leg AND the mirror in one go; callers that don't have a writer
        configured still call this method directly so the event is
        never lost. `status='local_only'` distinguishes these rows
        from `'written'` (mirrored after a successful remote write)
        and `'failed'` (mirrored after the remote leg gave up).

        Returns the inserted rowid.
        """
        import json
        from datetime import datetime, timezone
        record = event.to_record()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO lifecycle_events_mirror "
                "(event_type, payload_json, timestamp, walacor_record_id, "
                " write_status, error_reason, attempts, written_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_type.value,
                    json.dumps(record, sort_keys=True),
                    event.timestamp,
                    walacor_id,
                    status,
                    error_reason,
                    attempts,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return int(cur.lastrowid)

    def list_tables(self) -> List[str]:
        # Filter `sqlite_%` so SQLite's internal bookkeeping tables — created
        # eagerly by AUTOINCREMENT columns — don't leak to callers doing set
        # comparisons or iterating for DDL operations.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
            return [r[0] for r in rows]
