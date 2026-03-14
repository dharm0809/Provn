"""Write-ahead log: SQLite WAL mode. Append-only with fsync. Crash-safe."""

from __future__ import annotations

import logging

from gateway.util import json_utils as json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from walacor_core.models.execution import ExecutionRecord

logger = logging.getLogger(__name__)


class WALWriter:
    """SQLite WAL mode. Tables: wal_records (execution records), gateway_attempts (completeness invariant)."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS wal_records (
                    execution_id  TEXT    PRIMARY KEY,
                    record_json   TEXT    NOT NULL,
                    created_at    TEXT    NOT NULL,
                    delivered     INTEGER NOT NULL DEFAULT 0,
                    delivered_at  TEXT
                )"""
            )
            # Composite index: delivery worker queries undelivered rows ordered by age.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wal_records_pending"
                " ON wal_records (delivered, created_at)"
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS gateway_attempts (
                    request_id    TEXT    PRIMARY KEY,
                    timestamp     TEXT    NOT NULL,
                    tenant_id     TEXT    NOT NULL,
                    provider      TEXT,
                    model_id      TEXT,
                    path          TEXT    NOT NULL,
                    disposition   TEXT    NOT NULL,
                    execution_id  TEXT,
                    status_code   INTEGER NOT NULL
                )"""
            )
            # Index for time-range purge queries.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gateway_attempts_timestamp"
                " ON gateway_attempts (timestamp)"
            )
            # Composite index for per-tenant disposition reporting.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gateway_attempts_tenant_disp"
                " ON gateway_attempts (tenant_id, disposition)"
            )
            # Phase 21: add user column to gateway_attempts (non-destructive migration)
            try:
                self._conn.execute("ALTER TABLE gateway_attempts ADD COLUMN user TEXT")
                self._conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise  # Only suppress "duplicate column" errors
        return self._conn

    def write_and_fsync(self, record: ExecutionRecord | dict[str, Any]) -> None:
        """Append record to WAL. Blocks until fsync (synchronous=FULL). Accepts ExecutionRecord or dict."""
        conn = self._ensure_conn()
        if isinstance(record, dict):
            data = record
        else:
            data = record.model_dump(mode="json")
        record_json = json.dumps(data)
        now = datetime.now(timezone.utc).isoformat()
        execution_id = data["execution_id"] if isinstance(record, dict) else record.execution_id
        conn.execute(
            "INSERT OR REPLACE INTO wal_records (execution_id, record_json, created_at, delivered) VALUES (?, ?, ?, 0)",
            (execution_id, record_json, now),
        )
        conn.commit()
        logger.debug("WAL write execution_id=%s", execution_id)

    def write_batch(self, records: list[dict[str, Any]]) -> None:
        """Write multiple records in a single transaction using executemany."""
        if not records:
            return
        conn = self._ensure_conn()
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for data in records:
            execution_id = data.get("execution_id") or data.get("event_id", "")
            record_json = json.dumps(data)
            rows.append((execution_id, record_json, now, 0))
        conn.executemany(
            "INSERT OR REPLACE INTO wal_records (execution_id, record_json, created_at, delivered) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        logger.debug("WAL write_batch count=%d", len(rows))

    def get_undelivered(self, limit: int = 50) -> list[tuple[str, str, str]]:
        """Return list of (execution_id, record_json, created_at) for undelivered records, oldest first."""
        conn = self._ensure_conn()
        cur = conn.execute(
            "SELECT execution_id, record_json, created_at FROM wal_records WHERE delivered = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        return list(cur.fetchall())

    def mark_delivered(self, execution_id: str) -> None:
        conn = self._ensure_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE wal_records SET delivered = 1, delivered_at = ? WHERE execution_id = ?",
            (now, execution_id),
        )
        conn.commit()

    def pending_count(self) -> int:
        conn = self._ensure_conn()
        cur = conn.execute("SELECT COUNT(*) FROM wal_records WHERE delivered = 0")
        return cur.fetchone()[0]

    def oldest_pending_seconds(self) -> float | None:
        """Seconds since oldest undelivered record's created_at. None if no pending records."""
        conn = self._ensure_conn()
        cur = conn.execute(
            "SELECT created_at FROM wal_records WHERE delivered = 0 ORDER BY created_at ASC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        from datetime import datetime, timezone
        try:
            created = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - created).total_seconds()
        except (ValueError, TypeError):
            return None

    def disk_usage_bytes(self) -> int:
        if not os.path.exists(self._path):
            return 0
        return os.path.getsize(self._path) + (
            os.path.getsize(self._path + "-wal") if os.path.exists(self._path + "-wal") else 0
        ) + (os.path.getsize(self._path + "-shm") if os.path.exists(self._path + "-shm") else 0)

    def write_attempt(
        self,
        request_id: str,
        tenant_id: str,
        path: str,
        disposition: str,
        status_code: int,
        provider: str | None = None,
        model_id: str | None = None,
        execution_id: str | None = None,
        user: str | None = None,
    ) -> None:
        """Append one row to gateway_attempts for the completeness invariant."""
        conn = self._ensure_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO gateway_attempts
               (request_id, timestamp, tenant_id, provider, model_id, path, disposition, execution_id, status_code, user)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request_id, now, tenant_id, provider or None, model_id or None, path, disposition, execution_id or None, status_code, user or None),
        )
        conn.commit()
        logger.debug("gateway_attempts request_id=%s disposition=%s user=%s", request_id, disposition, user)

    def write_tool_event(self, record: dict[str, Any]) -> None:
        """Append one tool event record to wal_records using event_id as the primary key."""
        conn = self._ensure_conn()
        event_id = record["event_id"]
        conn.execute(
            "INSERT OR REPLACE INTO wal_records (execution_id, record_json, created_at, delivered) VALUES (?, ?, ?, 0)",
            (event_id, json.dumps(record), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        logger.debug("WAL write_tool_event event_id=%s", event_id)

    def purge_delivered(self, max_age_hours: float) -> int:
        """Delete delivered wal_records older than max_age_hours. Returns count deleted."""
        conn = self._ensure_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        cur = conn.execute(
            "DELETE FROM wal_records WHERE delivered = 1 AND delivered_at < ?",
            (cutoff,),
        )
        conn.commit()
        deleted = cur.rowcount
        if deleted > 0:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted

    def purge_attempts(self, max_age_hours: float) -> int:
        """Delete gateway_attempts older than max_age_hours. Returns count deleted."""
        conn = self._ensure_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        cur = conn.execute(
            "DELETE FROM gateway_attempts WHERE timestamp < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
