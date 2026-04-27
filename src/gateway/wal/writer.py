"""Write-ahead log: SQLite WAL mode. Append-only with fsync. Crash-safe."""

from __future__ import annotations

import logging
import stat
import time

import gateway.util.json_utils as json
import os
import queue
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gateway.core.models.execution import ExecutionRecord

logger = logging.getLogger(__name__)


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables, indexes, and run idempotent column migrations.

    Called from both _ensure_conn (main thread) and _ensure_thread_conn
    (writer thread) so the schema is always ready before the first write.
    """
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
    # Pillar 1 — events the message-diff engine inferred from successive
    # ``messages[]`` arrays. ``source='reconstructed'`` distinguishes them from
    # the gateway's own active-tool-loop ``tool_events``.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reconstructed_tool_events (
            event_id        TEXT    PRIMARY KEY,
            execution_id    TEXT,
            tenant_id       TEXT    NOT NULL,
            caller_key      TEXT    NOT NULL,
            timestamp       TEXT    NOT NULL,
            kind            TEXT    NOT NULL,
            tool_name       TEXT,
            tool_call_id    TEXT,
            args_hash       TEXT,
            content_hash    TEXT,
            trace_id        TEXT,
            agent_run_id    TEXT,
            turn_seq        INTEGER,
            source          TEXT    NOT NULL DEFAULT 'reconstructed'
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recon_tool_events_caller"
        " ON reconstructed_tool_events (caller_key, timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recon_tool_events_trace"
        " ON reconstructed_tool_events (trace_id, timestamp)"
        " WHERE trace_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recon_tool_events_run"
        " ON reconstructed_tool_events (agent_run_id, timestamp)"
        " WHERE agent_run_id IS NOT NULL"
    )
    # Pillar 2 — content fingerprints over tool calls + results. Kept in its
    # own table because the join shape is "find rows with the same hash in a
    # different caller_key" which dominates the WHERE clauses; a partial
    # index per hash kind keeps the lookups O(log n).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tool_fingerprints (
            fp_id          TEXT    PRIMARY KEY,
            tenant_id      TEXT    NOT NULL,
            record_id      TEXT,
            caller_key     TEXT,
            tool_call_id   TEXT,
            tool_name      TEXT,
            tc_hash        TEXT,
            tr_hash        TEXT,
            trace_id       TEXT,
            agent_run_id   TEXT,
            kind           TEXT    NOT NULL,
            seen_at        TEXT    NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fp_tenant_tc"
        " ON tool_fingerprints (tenant_id, tc_hash)"
        " WHERE tc_hash IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fp_tenant_tr"
        " ON tool_fingerprints (tenant_id, tr_hash)"
        " WHERE tr_hash IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fp_tenant_trace"
        " ON tool_fingerprints (tenant_id, trace_id)"
        " WHERE trace_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gateway_attempts_tenant_disp"
        " ON gateway_attempts (tenant_id, disposition)"
    )

    # ── Idempotent column migrations ──────────────────────────────────────
    _add_columns = [
        # gateway_attempts (Phase 21)
        ("gateway_attempts", "user", "TEXT"),
        ("gateway_attempts", "reason", "TEXT"),
        # Tier 0 of agent tracing — caller-supplied correlation IDs.
        # All nullable; uninstrumented agents leave them blank.
        ("gateway_attempts", "trace_id", "TEXT"),
        ("gateway_attempts", "parent_span_id", "TEXT"),
        ("gateway_attempts", "agent_run_id", "TEXT"),
        ("gateway_attempts", "agent_name", "TEXT"),
        ("gateway_attempts", "parent_observation_id", "TEXT"),
        ("gateway_attempts", "parent_record_id", "TEXT"),
        ("gateway_attempts", "previous_response_id", "TEXT"),
        ("gateway_attempts", "conversation_id", "TEXT"),
        # Mirror onto wal_records so executions can be queried by trace.
        ("wal_records", "trace_id", "TEXT"),
        ("wal_records", "agent_run_id", "TEXT"),
        ("wal_records", "agent_name", "TEXT"),
        # wal_records — extracted hot columns for indexed lineage queries
        ("wal_records", "event_type", "TEXT NOT NULL DEFAULT 'execution'"),
        ("wal_records", "session_id", "TEXT"),
        ("wal_records", "timestamp", "TEXT"),
        ("wal_records", "model_id", "TEXT"),
        ("wal_records", "provider", "TEXT"),
        ("wal_records", "user", "TEXT"),
        ("wal_records", "prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("wal_records", "completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("wal_records", "total_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("wal_records", "latency_ms", "REAL"),
        ("wal_records", "sequence_number", "INTEGER"),
        ("wal_records", "policy_result", "TEXT"),
        ("wal_records", "parent_execution_id", "TEXT"),
        ("wal_records", "tool_name", "TEXT"),
        ("wal_records", "tool_type", "TEXT"),
        # hot metadata fields promoted to columns so the sessions-list
        # query (_SESSIONS_AGG_SUBQUERY in lineage/reader.py) stops calling
        # json_extract() 6x per row on a large record_json blob.
        ("wal_records", "request_type", "TEXT"),
    ]
    for table, col, col_type in _add_columns:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # ── Indexes on extracted columns ──────────────────────────────────────
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wal_session"
        " ON wal_records (session_id, event_type, sequence_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wal_time"
        " ON wal_records (event_type, timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wal_tool_parent"
        " ON wal_records (parent_execution_id)"
        " WHERE event_type = 'tool_call'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wal_model"
        " ON wal_records (model_id, provider)"
        " WHERE event_type = 'execution'"
    )
    # Speeds up the sessions-list GROUP BY over non-system-task rows.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wal_session_request_type"
        " ON wal_records (session_id, request_type)"
        " WHERE event_type = 'execution'"
    )
    # Tier 0 of agent tracing — index only when the caller tagged traffic, so
    # the indexes stay tiny on uninstrumented workloads. Created here, AFTER
    # the ALTER TABLE migrations above have added the referenced columns.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gateway_attempts_trace"
        " ON gateway_attempts (trace_id, timestamp)"
        " WHERE trace_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gateway_attempts_agent_run"
        " ON gateway_attempts (agent_run_id, timestamp)"
        " WHERE agent_run_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wal_records_trace"
        " ON wal_records (trace_id, timestamp)"
        " WHERE trace_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wal_records_agent_run"
        " ON wal_records (agent_run_id, timestamp)"
        " WHERE agent_run_id IS NOT NULL"
    )

    # ── Backfill: populate extracted columns from existing record_json ────
    # Only runs once — rows with NULL timestamp have not yet been backfilled.
    try:
        needs_backfill = conn.execute(
            "SELECT COUNT(*) FROM wal_records WHERE timestamp IS NULL LIMIT 1"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        needs_backfill = 0

    if needs_backfill > 0:
        logger.info("Backfilling %d wal_records with extracted columns...", needs_backfill)
        conn.execute("""
            UPDATE wal_records SET
                event_type = CASE
                    WHEN json_extract(record_json, '$.event_type') = 'tool_call' THEN 'tool_call'
                    ELSE 'execution'
                END,
                session_id = json_extract(record_json, '$.session_id'),
                timestamp = COALESCE(json_extract(record_json, '$.timestamp'), created_at),
                model_id = json_extract(record_json, '$.model_id'),
                provider = json_extract(record_json, '$.provider'),
                user = json_extract(record_json, '$.user'),
                prompt_tokens = COALESCE(json_extract(record_json, '$.prompt_tokens'), 0),
                completion_tokens = COALESCE(json_extract(record_json, '$.completion_tokens'), 0),
                total_tokens = COALESCE(json_extract(record_json, '$.total_tokens'), 0),
                latency_ms = json_extract(record_json, '$.latency_ms'),
                sequence_number = json_extract(record_json, '$.sequence_number'),
                policy_result = json_extract(record_json, '$.policy_result'),
                parent_execution_id = CASE
                    WHEN json_extract(record_json, '$.event_type') = 'tool_call'
                    THEN json_extract(record_json, '$.execution_id')
                    ELSE NULL
                END,
                tool_name = json_extract(record_json, '$.tool_name'),
                tool_type = json_extract(record_json, '$.tool_type'),
                request_type = COALESCE(
                    json_extract(record_json, '$.metadata.request_type'),
                    'user_message'
                )
            WHERE timestamp IS NULL
        """)
        conn.commit()
        logger.info("Backfill complete.")


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

    _BATCH_MAX = 50
    _BATCH_TIMEOUT = 0.01  # 10ms

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
            # synchronous=NORMAL in WAL mode is crash-safe (atomicity preserved;
            # only the last in-flight transaction can be lost on power failure,
            # and never database corruption). Saves one fsync per commit batch,
            # which at 50 writes/10ms → ~5-15ms per batch under sustained load.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            db_path = Path(self._path)
            if db_path.exists():
                os.chmod(str(db_path), stat.S_IRUSR | stat.S_IWUSR)
            _apply_schema(conn)
            self._thread_conn = conn
        return self._thread_conn

    def _writer_loop(self) -> None:
        """Process write operations from the queue in a single dedicated thread.

        Batches up to _BATCH_MAX items (or _BATCH_TIMEOUT seconds) and issues
        a single conn.commit() per batch — dramatically reducing fsync calls
        under load (e.g. 1 commit per 50 writes instead of 50 commits).
        """
        conn = self._ensure_thread_conn()
        while True:
            # Block until at least one item arrives
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is None:  # sentinel — graceful exit
                break

            # We have at least one item; drain more up to BATCH_MAX / BATCH_TIMEOUT
            batch: list[tuple[Any, tuple]] = [item]
            deadline = time.monotonic() + self._BATCH_TIMEOUT
            while len(batch) < self._BATCH_MAX:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    next_item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if next_item is None:  # sentinel mid-batch
                    self._execute_batch(conn, batch)
                    return  # exit after committing pending work
                batch.append(next_item)

            self._execute_batch(conn, batch)

    def _execute_batch(self, conn: sqlite3.Connection, batch: list[tuple[Any, tuple]]) -> None:
        """Execute a batch of (fn, args) writes and issue a single commit."""
        for fn, args in batch:
            try:
                fn(conn, *args)
            except Exception:
                logger.error("WAL dedicated writer error", exc_info=True)
        try:
            conn.commit()
        except Exception:
            logger.error("WAL batch commit error", exc_info=True)

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
        # Tier 0 of agent tracing — pull caller-supplied correlation IDs out of
        # the metadata bag onto hot columns so /v1/lineage queries by trace can
        # use the partial index instead of json_extract.
        _meta = data.get("metadata") or {}
        _trace_id = _meta.get("trace_id") if isinstance(_meta, dict) else None
        _agent_run_id = _meta.get("agent_run_id") if isinstance(_meta, dict) else None
        _agent_name = _meta.get("agent_name") if isinstance(_meta, dict) else None
        conn.execute(
            """INSERT OR REPLACE INTO wal_records
               (execution_id, record_json, created_at, delivered,
                event_type, session_id, timestamp, model_id, provider, user,
                prompt_tokens, completion_tokens, total_tokens, latency_ms,
                sequence_number, policy_result,
                trace_id, agent_run_id, agent_name)
               VALUES (?, ?, ?, 0,
                       'execution', ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?,
                       ?, ?, ?)""",
            (execution_id, record_json, now,
             data.get("session_id"), data.get("timestamp") or now,
             data.get("model_id"), data.get("provider"), data.get("user"),
             data.get("prompt_tokens") or 0, data.get("completion_tokens") or 0,
             data.get("total_tokens") or 0, data.get("latency_ms"),
             data.get("sequence_number"), data.get("policy_result"),
             _trace_id, _agent_run_id, _agent_name),
        )
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
        reason: str | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        agent_run_id: str | None = None,
        agent_name: str | None = None,
        parent_observation_id: str | None = None,
        parent_record_id: str | None = None,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO gateway_attempts
               (request_id, timestamp, tenant_id, provider, model_id, path, disposition,
                execution_id, status_code, user, reason,
                trace_id, parent_span_id, agent_run_id, agent_name,
                parent_observation_id, parent_record_id,
                previous_response_id, conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id, now, tenant_id, provider or None, model_id or None, path, disposition,
                execution_id or None, status_code, user or None, reason or None,
                trace_id or None, parent_span_id or None, agent_run_id or None, agent_name or None,
                parent_observation_id or None, parent_record_id or None,
                previous_response_id or None, conversation_id or None,
            ),
        )
        logger.debug("WAL (thread) gateway_attempts request_id=%s disposition=%s", request_id, disposition)

    @staticmethod
    def _do_write_tool_event(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        event_id = record["event_id"]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO wal_records
               (execution_id, record_json, created_at, delivered,
                event_type, session_id, timestamp, parent_execution_id,
                tool_name, tool_type)
               VALUES (?, ?, ?, 0,
                       'tool_call', ?, ?, ?, ?, ?)""",
            (event_id, json.dumps(record), now,
             record.get("session_id"), record.get("timestamp") or now,
             record.get("execution_id"), record.get("tool_name"),
             record.get("tool_type")),
        )
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
        reason: str | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        agent_run_id: str | None = None,
        agent_name: str | None = None,
        parent_observation_id: str | None = None,
        parent_record_id: str | None = None,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """Non-blocking enqueue of an attempt record to the dedicated writer thread."""
        self._queue.put((
            self._do_write_attempt,
            (
                request_id, tenant_id, path, disposition, status_code,
                provider, model_id, execution_id, user, reason,
                trace_id, parent_span_id, agent_run_id, agent_name,
                parent_observation_id, parent_record_id,
                previous_response_id, conversation_id,
            ),
        ))

    def enqueue_write_tool_event(self, record: dict[str, Any]) -> None:
        """Non-blocking enqueue of a tool event record to the dedicated writer thread."""
        self._queue.put((self._do_write_tool_event, (record,)))

    @staticmethod
    def _do_write_recon_event(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO reconstructed_tool_events
               (event_id, execution_id, tenant_id, caller_key, timestamp, kind,
                tool_name, tool_call_id, args_hash, content_hash,
                trace_id, agent_run_id, turn_seq, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["event_id"],
                record.get("execution_id"),
                record.get("tenant_id") or "",
                record.get("caller_key") or "",
                record.get("timestamp"),
                record.get("kind"),
                record.get("tool_name"),
                record.get("tool_call_id"),
                record.get("args_hash"),
                record.get("content_hash"),
                record.get("trace_id"),
                record.get("agent_run_id"),
                record.get("turn_seq"),
                record.get("source", "reconstructed"),
            ),
        )

    def enqueue_write_recon_event(self, record: dict[str, Any]) -> None:
        """Non-blocking enqueue for a Pillar-1 reconstructed_tool_events row."""
        self._queue.put((self._do_write_recon_event, (record,)))

    @staticmethod
    def _do_write_fingerprint(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO tool_fingerprints
               (fp_id, tenant_id, record_id, caller_key, tool_call_id, tool_name,
                tc_hash, tr_hash, trace_id, agent_run_id, kind, seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["fp_id"],
                record.get("tenant_id") or "",
                record.get("record_id"),
                record.get("caller_key"),
                record.get("tool_call_id"),
                record.get("tool_name"),
                record.get("tc_hash"),
                record.get("tr_hash"),
                record.get("trace_id"),
                record.get("agent_run_id"),
                record.get("kind"),
                record.get("seen_at"),
            ),
        )

    def enqueue_write_fingerprint(self, record: dict[str, Any]) -> None:
        """Non-blocking enqueue for a Pillar-2 tool_fingerprints row."""
        self._queue.put((self._do_write_fingerprint, (record,)))

    # ------------------------------------------------------------------
    # Synchronous public API (used by delivery worker, startup, health, batch writer)
    # All callers run on the asyncio event-loop thread — no concurrent access.
    # ------------------------------------------------------------------

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL is crash-safe in WAL mode (see matching comment in
            # _ensure_thread_conn above).
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA secure_delete=ON")
            db_path = Path(self._path)
            if db_path.exists():
                os.chmod(str(db_path), stat.S_IRUSR | stat.S_IWUSR)
            _apply_schema(self._conn)
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
        request_type = (data.get("metadata") or {}).get("request_type") or "user_message"
        conn.execute(
            """INSERT OR REPLACE INTO wal_records
               (execution_id, record_json, created_at, delivered,
                event_type, session_id, timestamp, model_id, provider, user,
                prompt_tokens, completion_tokens, total_tokens, latency_ms,
                sequence_number, policy_result, request_type)
               VALUES (?, ?, ?, 0,
                       'execution', ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?)""",
            (execution_id, record_json, now,
             data.get("session_id"), data.get("timestamp") or now,
             data.get("model_id"), data.get("provider"), data.get("user"),
             data.get("prompt_tokens") or 0, data.get("completion_tokens") or 0,
             data.get("total_tokens") or 0, data.get("latency_ms"),
             data.get("sequence_number"), data.get("policy_result"),
             request_type),
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
            is_tool = data.get("event_type") == "tool_call"
            pk = data.get("event_id", "") if is_tool else data.get("execution_id", "")
            record_json = json.dumps(data)
            req_type = None if is_tool else ((data.get("metadata") or {}).get("request_type") or "user_message")
            rows.append((
                pk, record_json, now, 0,
                "tool_call" if is_tool else "execution",
                data.get("session_id"),
                data.get("timestamp") or now,
                data.get("model_id"),
                data.get("provider"),
                data.get("user"),
                data.get("prompt_tokens") or 0,
                data.get("completion_tokens") or 0,
                data.get("total_tokens") or 0,
                data.get("latency_ms"),
                data.get("sequence_number"),
                data.get("policy_result"),
                data.get("execution_id") if is_tool else None,
                data.get("tool_name"),
                data.get("tool_type"),
                req_type,
            ))
        conn.executemany(
            """INSERT OR REPLACE INTO wal_records
               (execution_id, record_json, created_at, delivered,
                event_type, session_id, timestamp, model_id, provider, user,
                prompt_tokens, completion_tokens, total_tokens, latency_ms,
                sequence_number, policy_result, parent_execution_id,
                tool_name, tool_type, request_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        logger.debug("WAL write_batch count=%d", len(rows))

    def get_chain_heads(self, ttl_hours: int = 24) -> list[tuple[str, int, str, str | None]]:
        """Return (session_id, max_sequence_number, last_record_hash, last_record_id) for recent sessions.

        Used to warm the SessionChainTracker on startup so chains survive restarts.
        Only loads sessions active within *ttl_hours* to avoid loading stale data.
        """
        conn = self._ensure_conn()
        cur = conn.execute(
            """SELECT session_id, MAX(sequence_number) AS seq,
                      json_extract(record_json, '$.record_hash') AS rh,
                      json_extract(record_json, '$.record_id') AS rid
               FROM wal_records
               WHERE event_type = 'execution'
                 AND session_id IS NOT NULL
                 AND sequence_number IS NOT NULL
                 AND timestamp >= datetime('now', ?)
               GROUP BY session_id""",
            (f"-{ttl_hours} hours",),
        )
        results = []
        for row in cur.fetchall():
            sid, seq, rh, rid = row
            if sid and seq is not None and (rh or rid):
                results.append((sid, int(seq), str(rh or ""), rid))
        return results

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
        reason: str | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        agent_run_id: str | None = None,
        agent_name: str | None = None,
        parent_observation_id: str | None = None,
        parent_record_id: str | None = None,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """Append one row to gateway_attempts for the completeness invariant."""
        conn = self._ensure_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO gateway_attempts
               (request_id, timestamp, tenant_id, provider, model_id, path, disposition,
                execution_id, status_code, user, reason,
                trace_id, parent_span_id, agent_run_id, agent_name,
                parent_observation_id, parent_record_id,
                previous_response_id, conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id, now, tenant_id, provider or None, model_id or None, path, disposition,
                execution_id or None, status_code, user or None, reason or None,
                trace_id or None, parent_span_id or None, agent_run_id or None, agent_name or None,
                parent_observation_id or None, parent_record_id or None,
                previous_response_id or None, conversation_id or None,
            ),
        )
        conn.commit()
        logger.debug("gateway_attempts request_id=%s disposition=%s user=%s", request_id, disposition, user)

    def write_tool_event(self, record: dict[str, Any]) -> None:
        """Append one tool event record to wal_records using event_id as the primary key."""
        conn = self._ensure_conn()
        event_id = record["event_id"]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO wal_records
               (execution_id, record_json, created_at, delivered,
                event_type, session_id, timestamp, parent_execution_id,
                tool_name, tool_type)
               VALUES (?, ?, ?, 0,
                       'tool_call', ?, ?, ?, ?, ?)""",
            (event_id, json.dumps(record), now,
             record.get("session_id"), record.get("timestamp") or now,
             record.get("execution_id"), record.get("tool_name"),
             record.get("tool_type")),
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
