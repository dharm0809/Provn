"""Write-ahead log: SQLite WAL mode. Append-only with fsync. Crash-safe."""

from __future__ import annotations

import logging

import gateway.util.json_utils as json
import os
import queue
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from walacor_core.models.execution import ExecutionRecord

logger = logging.getLogger(__name__)


class WALWriter:
    """SQLite WAL mode. Tables: wal_records (execution records), gateway_attempts (completeness invariant).

    The writer runs a dedicated background thread that owns a single SQLite
    connection for all enqueued writes.  This eliminates thread-pool dispatch
    overhead (no asyncio.to_thread per write) and provides natural write
    batching.

    Direct synchronous methods (write_and_fsync, write_attempt, write_tool_event,
    get_undelivered, mark_delivered, …) still work through self._conn and are
    used by callers that run on the asyncio event-loop thread (delivery worker,
    startup self-test, health checks, batch writer).  Those callers all run on
    a single thread so there is no concurrent-access risk on self._conn.

    Fire-and-forget enqueue methods (enqueue_write_execution, enqueue_write_attempt,
    enqueue_write_tool_event) push work onto a thread-safe queue processed by
    the dedicated writer thread using its own separate connection.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

        # Dedicated writer thread state
        self._queue: queue.Queue[tuple[Any, tuple] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._thread_conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the dedicated background writer thread. Call once at startup."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="wal-writer"
        )
        self._thread.start()
        logger.info("WALWriter dedicated thread started")

    def stop(self) -> None:
        """Stop the writer thread gracefully, draining the queue. Call once at shutdown."""
        self._running = False
        self._queue.put(None)  # sentinel — tells the loop to exit
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._thread_conn:
            try:
                self._thread_conn.close()
            except Exception:
                pass
            self._thread_conn = None
        logger.info("WALWriter dedicated thread stopped")

    # ------------------------------------------------------------------
    # Internal: dedicated writer thread
    # ------------------------------------------------------------------

    def _ensure_thread_conn(self) -> sqlite3.Connection:
        """Open the SQLite connection for the dedicated writer thread (called from that thread only).

        Creates the schema if not already present — this ensures the tables exist even
        if the writer thread receives its first enqueued item before the main-thread
        connection has been opened (e.g. in tests or when batch writer is enabled
        but the startup self-test has not yet run).
        """
        if self._thread_conn is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS wal_records (
                    execution_id  TEXT    PRIMARY KEY,
                    record_json   TEXT    NOT NULL,
                    created_at    TEXT    NOT NULL,
                    delivered     INTEGER NOT NULL DEFAULT 0,
                    delivered_at  TEXT
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wal_records_pending"
                " ON wal_records (delivered, created_at)"
            )
            conn.execute(
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gateway_attempts_timestamp"
                " ON gateway_attempts (timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gateway_attempts_tenant_disp"
                " ON gateway_attempts (tenant_id, disposition)"
            )
            try:
                conn.execute("ALTER TABLE gateway_attempts ADD COLUMN user TEXT")
                conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
            self._thread_conn = conn
        return self._thread_conn

    def _writer_loop(self) -> None:
        """Process write operations from the queue in a single dedicated thread."""
        conn = self._ensure_thread_conn()
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:  # sentinel — graceful exit
                break
            fn, args = item
            try:
                fn(conn, *args)
            except Exception:
                logger.error("WAL dedicated writer error", exc_info=True)

    # ------------------------------------------------------------------
    # Inner write functions (accept an explicit conn; called from writer thread)
    # ------------------------------------------------------------------

    @staticmethod
    def _do_write_execution(conn: sqlite3.Connection, record: ExecutionRecord | dict[str, Any]) -> None:
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
        logger.debug("WAL (thread) write execution_id=%s", execution_id)

    @staticmethod
    def _do_write_attempt(
        conn: sqlite3.Connection,
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
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO gateway_attempts
               (request_id, timestamp, tenant_id, provider, model_id, path, disposition, execution_id, status_code, user)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request_id, now, tenant_id, provider or None, model_id or None, path, disposition, execution_id or None, status_code, user or None),
        )
        conn.commit()
        logger.debug("WAL (thread) gateway_attempts request_id=%s disposition=%s", request_id, disposition)

    @staticmethod
    def _do_write_tool_event(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        event_id = record["event_id"]
        conn.execute(
            "INSERT OR REPLACE INTO wal_records (execution_id, record_json, created_at, delivered) VALUES (?, ?, ?, 0)",
            (event_id, json.dumps(record), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        logger.debug("WAL (thread) write_tool_event event_id=%s", event_id)

    # ------------------------------------------------------------------
    # Fire-and-forget enqueue API (used by WALBackend)
    # ------------------------------------------------------------------

    def enqueue_write_execution(self, record: ExecutionRecord | dict[str, Any]) -> None:
        """Non-blocking enqueue of an execution record to the dedicated writer thread."""
        self._queue.put((self._do_write_execution, (record,)))

    def enqueue_write_attempt(
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
        """Non-blocking enqueue of an attempt record to the dedicated writer thread."""
        self._queue.put((
            self._do_write_attempt,
            (request_id, tenant_id, path, disposition, status_code, provider, model_id, execution_id, user),
        ))

    def enqueue_write_tool_event(self, record: dict[str, Any]) -> None:
        """Non-blocking enqueue of a tool event record to the dedicated writer thread."""
        self._queue.put((self._do_write_tool_event, (record,)))

    # ------------------------------------------------------------------
    # Synchronous public API (used by delivery worker, startup, health, batch writer)
    # All callers run on the asyncio event-loop thread — no concurrent access.
    # ------------------------------------------------------------------

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
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
        self.stop()
        if self._conn:
            self._conn.close()
            self._conn = None
