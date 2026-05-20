"""SQLite-backed store for chain verification results.

This store is the persistent cache the ``ChainIntegrityWorker`` writes to
on every tick and the compliance API reads from on every dashboard
request. It is intentionally process-local-by-file but shared-by-disk:
a single SQLite file at ``{wal_path}/chain_verification.db`` opened with
``journal_mode=WAL`` so all uvicorn workers can read concurrently while
the leader (see ``chain_worker.ChainIntegrityWorker._acquire_leadership``)
writes.

Schema
------
One row per verified session::

    CREATE TABLE chain_verifications (
        session_id          TEXT    PRIMARY KEY,
        valid               INTEGER NOT NULL,        -- 0/1
        verification_level  TEXT    NOT NULL,
        errors_json         TEXT    NOT NULL,        -- "[]" when none
        records_checked     INTEGER NOT NULL,
        verified_at         TEXT    NOT NULL         -- ISO-8601 UTC
    );

    CREATE TABLE chain_meta (
        key                 TEXT PRIMARY KEY,
        value               TEXT NOT NULL
    );

The ``chain_meta`` table holds singleton state — currently just the
``last_tick_at`` ISO timestamp so the API can surface "last verification
at HH:MM:SS" in the compliance report.

Why not Walacor
---------------
This is gateway-internal precomputed state, not an audit artifact. The
authoritative chain data lives in Walacor; this store is a derived
projection that the worker rebuilds from scratch on every tick anyway.
Storing it locally avoids round-tripping a derived value through Walacor
and keeps the read path on a fast in-process SQLite query.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS chain_verifications (
        session_id          TEXT    PRIMARY KEY,
        valid               INTEGER NOT NULL,
        verification_level  TEXT    NOT NULL,
        errors_json         TEXT    NOT NULL,
        records_checked     INTEGER NOT NULL,
        verified_at         TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chain_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chain_verifications_verified_at ON chain_verifications(verified_at)",
]


class ChainVerificationStore:
    """SQLite-backed store of per-session chain verification results.

    Thread-safe: each method opens its own short-lived connection (a
    `threading.local` cache would also work, but the store is so cold
    relative to the request path that the open cost is irrelevant).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def db_path(self) -> str:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL mode lets multiple uvicorn workers read concurrently while
        # the leader writes — no SQLITE_BUSY storms under the dashboard
        # load.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                for stmt in _SCHEMA:
                    conn.execute(stmt)
            finally:
                conn.close()

    # ------------------------------------------------------------------ writes

    def upsert_many(self, results: Iterable[dict[str, Any]]) -> int:
        """Insert/replace verification results. Returns count written.

        Each item must look like the dict returned by ``reader.verify_chain``::

            {"session_id", "valid", "verification_level", "errors",
             "records_checked"}
        """
        now = datetime.now(timezone.utc).isoformat()
        rows: list[tuple] = []
        for r in results:
            sid = r.get("session_id")
            if not sid:
                continue
            rows.append((
                sid,
                1 if r.get("valid") else 0,
                r.get("verification_level") or "unknown",
                json.dumps(r.get("errors") or []),
                int(r.get("records_checked") or 0),
                now,
            ))
        if not rows:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    """
                    INSERT INTO chain_verifications
                        (session_id, valid, verification_level, errors_json,
                         records_checked, verified_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        valid              = excluded.valid,
                        verification_level = excluded.verification_level,
                        errors_json        = excluded.errors_json,
                        records_checked    = excluded.records_checked,
                        verified_at        = excluded.verified_at
                    """,
                    rows,
                )
            finally:
                conn.close()
        return len(rows)

    def prune_keep(self, session_ids: Iterable[str]) -> int:
        """Delete every row whose session_id is NOT in the given set.

        Called by the worker after each tick to drop sessions that have
        aged out of the verification window.  Returns the number of
        rows deleted.
        """
        ids = [s for s in session_ids if s]
        with self._lock:
            conn = self._connect()
            try:
                if not ids:
                    cur = conn.execute("DELETE FROM chain_verifications")
                    return cur.rowcount or 0
                # Build a temp table to avoid 999-parameter limits on big windows.
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep (sid TEXT PRIMARY KEY)")
                conn.execute("DELETE FROM _keep")
                conn.executemany("INSERT OR IGNORE INTO _keep(sid) VALUES (?)",
                                 [(s,) for s in ids])
                cur = conn.execute(
                    "DELETE FROM chain_verifications "
                    "WHERE session_id NOT IN (SELECT sid FROM _keep)"
                )
                deleted = cur.rowcount or 0
                conn.execute("DROP TABLE _keep")
                return deleted
            finally:
                conn.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO chain_meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            finally:
                conn.close()

    # ------------------------------------------------------------------- reads

    def get_meta(self, key: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM chain_meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chain_verifications"
            ).fetchone()
            return int(row["n"] or 0) if row else 0
        finally:
            conn.close()

    def get_all(self) -> list[dict[str, Any]]:
        """Return every verification result currently in the store.

        Shape matches what ``reader.verify_chain`` returns so the
        compliance API can drop them straight into the ``sessions`` list.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session_id, valid, verification_level, errors_json, "
                "records_checked, verified_at "
                "FROM chain_verifications "
                "ORDER BY verified_at DESC"
            ).fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                errors = json.loads(r["errors_json"]) if r["errors_json"] else []
            except Exception:
                errors = []
            out.append({
                "session_id": r["session_id"],
                "valid": bool(r["valid"]),
                "verification_level": r["verification_level"],
                "errors": errors,
                "records_checked": int(r["records_checked"] or 0),
                "verified_at": r["verified_at"],
            })
        return out
