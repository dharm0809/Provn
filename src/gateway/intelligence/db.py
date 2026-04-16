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
from pathlib import Path
from typing import List

SCHEMA = """
CREATE TABLE IF NOT EXISTS onnx_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_features_json TEXT NOT NULL,
    prediction TEXT NOT NULL,
    confidence REAL NOT NULL,
    request_id TEXT,
    timestamp REAL NOT NULL,
    divergence_signal TEXT,
    divergence_source TEXT
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
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_model_version ON shadow_comparisons(model_name, candidate_version);

CREATE TABLE IF NOT EXISTS training_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    dataset_hash TEXT NOT NULL UNIQUE,
    row_ids_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
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
