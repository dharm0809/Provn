"""Read-only SQLite reader for the WAL database. Separate connection from WALWriter."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gateway.lineage._normalize import normalize_record as _normalize_record
from gateway.pipeline.session_chain import GENESIS_HASH

logger = logging.getLogger(__name__)


def _empty_verify_result(session_id: str) -> dict:
    """Shape returned for sessions with zero records on file.

    ``valid`` is True (there's nothing to disprove) but ``verification_level``
    is explicitly ``"structural"`` rather than ``"verified"`` — we have
    absolutely no Walacor evidence either way. Dashboard renders this as a
    neutral state, not a green check.
    """
    from gateway.crypto.signing import signing_key_available
    return {
        "valid": True,
        "verification_level": "structural",
        "records_checked": 0,
        "errors": [],
        "session_id": session_id,
        "checks": {
            "structural": {"passed": 0, "failed": 0},
            "signatures": {"valid": 0, "invalid": 0, "absent": 0, "unverifiable": 0,
                           "verify_key_loaded": signing_key_available()},
            "anchors":    {"verified": 0, "present": 0, "absent": 0, "mismatched": 0,
                           "unverifiable": 0, "independent_roundtrip": False},
        },
        "records": [],
        "walacor_attestation": [],
    }

# request_type is an indexed column (see wal/writer.py); the six-way repeated
# json_extract() in earlier revisions triggered a full scan of record_json for
# every row. A single CASE over the column short-circuits all of that.
_SESSIONS_AGG_SUBQUERY = """
                SELECT
                    session_id,
                    COUNT(*) AS record_count,
                    SUM(CASE WHEN COALESCE(request_type, 'user_message')
                        NOT LIKE 'system_task%' THEN 1 ELSE 0 END) AS user_message_count,
                    MAX(timestamp) AS last_activity,
                    model_id AS model,
                    user,
                    MAX(CASE WHEN COALESCE(request_type, 'user_message') NOT LIKE 'system_task%'
                        THEN json_extract(record_json, '$.metadata.walacor_audit.user_question')
                        ELSE NULL END) AS user_question,
                    MAX(CASE WHEN COALESCE(request_type, 'user_message') NOT LIKE 'system_task%'
                        THEN json_extract(record_json, '$.metadata.walacor_audit.has_rag_context')
                        ELSE NULL END) AS has_rag_context,
                    MAX(CASE WHEN COALESCE(request_type, 'user_message') NOT LIKE 'system_task%'
                        THEN json_extract(record_json, '$.metadata.walacor_audit.has_files')
                        ELSE NULL END) AS has_files,
                    MAX(CASE WHEN COALESCE(request_type, 'user_message') NOT LIKE 'system_task%'
                        THEN json_extract(record_json, '$.metadata.walacor_audit.has_images')
                        ELSE NULL END) AS has_images,
                    MAX(CASE WHEN COALESCE(request_type, 'user_message') NOT LIKE 'system_task%'
                        THEN request_type
                        ELSE NULL END) AS request_type,
                    MAX(json_extract(record_json, '$.metadata.tool_strategy')) AS meta_tool_strategy,
                    MAX(json_extract(record_json, '$.metadata.tool_interaction_count')) AS meta_tool_count
                FROM wal_records
                WHERE session_id IS NOT NULL
                  AND event_type = 'execution'
                GROUP BY session_id"""

_SESSIONS_TOOL_JOIN_SUBQUERY = """
                SELECT
                    r.session_id AS session_id,
                    GROUP_CONCAT(DISTINCT te.tool_name) AS tool_names,
                    GROUP_CONCAT(DISTINCT
                        te.tool_name || ':' ||
                        COALESCE(te.tool_type, 'unknown')
                    ) AS tool_details
                FROM wal_records r
                JOIN wal_records te
                  ON te.parent_execution_id = r.execution_id
                 AND te.event_type = 'tool_call'
                WHERE r.session_id IS NOT NULL
                  AND r.event_type = 'execution'
                GROUP BY r.session_id"""

_SESSIONS_SORT_COLUMNS: dict[str, str] = {
    "last_activity": "s.last_activity",
    "record_count": "s.record_count",
    "model": "COALESCE(s.model, '')",
}


def _sessions_search_where(search: str | None) -> tuple[str, tuple]:
    if not search or not str(search).strip():
        return "", ()
    n = str(search).strip()
    clause = """WHERE (
            INSTR(LOWER(COALESCE(s.session_id, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(s.model, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(s.user, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(s.user_question, '')), LOWER(?)) > 0
        )"""
    return clause, (n, n, n, n)


_ATTEMPTS_SORT_COLUMNS: dict[str, str] = {
    "timestamp": "timestamp",
    "disposition": "disposition",
    "request_id": "COALESCE(request_id, '')",
    "user": "COALESCE(user, '')",
    "model_id": "COALESCE(model_id, '')",
    "path": "COALESCE(path, '')",
    "status_code": "status_code",
}


def _attempts_search_where(search: str | None) -> tuple[str, tuple]:
    if not search or not str(search).strip():
        return "", ()
    n = str(search).strip()
    clause = """WHERE (
            INSTR(LOWER(COALESCE(request_id, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(tenant_id, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(provider, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(model_id, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(path, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(disposition, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(user, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(execution_id, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(COALESCE(reason, '')), LOWER(?)) > 0 OR
            INSTR(LOWER(CAST(status_code AS TEXT)), LOWER(?)) > 0
        )"""
    return clause, (n, n, n, n, n, n, n, n, n, n)


def _metrics_timeline_labels(range_key: str) -> tuple[list[str], str, str]:
    """Build UTC bucket keys for the full window plus [t_low, t_high) bounds for SQL (ISO +00:00).

    Returns 60 one-minute buckets for 1h, 24 hourly for 24h, 168 for 7d, 720 for 30d.
    """
    now = datetime.now(timezone.utc)
    if range_key == "1h":
        end = now.replace(second=0, microsecond=0)
        start = end - timedelta(hours=1)
        step = timedelta(minutes=1)
        fmt = "%Y-%m-%dT%H:%M:00"
    elif range_key == "24h":
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=24)
        step = timedelta(hours=1)
        fmt = "%Y-%m-%dT%H:00:00"
    elif range_key == "7d":
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=7)
        step = timedelta(hours=1)
        fmt = "%Y-%m-%dT%H:00:00"
    elif range_key == "30d":
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=30)
        step = timedelta(hours=1)
        fmt = "%Y-%m-%dT%H:00:00"
    else:
        raise ValueError(range_key)

    labels: list[str] = []
    cur = start
    while cur < end:
        labels.append(cur.strftime(fmt))
        cur += step

    t_low = start.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
    t_high = end.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
    return labels, t_low, t_high


class LineageReader:
    """Read-only access to the WAL SQLite database for lineage queries.

    Opens with `?mode=ro` and `PRAGMA query_only=ON` so the write-path
    WALWriter is never blocked or corrupted by lineage reads.

    **Phase 1.1 — multi-PID WAL aggregation.** When the gateway runs with
    ``WALACOR_UVICORN_WORKERS>1``, each worker writes its own
    ``wal-<pid>.db`` (single-file SQLite multi-writer is unsafe). The
    reader aggregates across every ``wal*.db`` in the same directory
    using :func:`gateway.wal.path.iter_wal_db_paths` — readiness
    checks already do this via ``_exec_wal_ro_all``; lineage now mirrors
    the pattern. Single-worker mode (only ``wal.db`` present) is
    byte-identical to pre-Phase-1.1: one file → one query → one result.

    Queries that aggregate (``COUNT(*) GROUP BY t``, ``GROUP BY model``,
    etc.) are executed against each file independently and re-merged in
    Python so per-worker partitioned aggregates fold correctly.
    Pagination (``ORDER BY ... LIMIT/OFFSET``) is re-applied after the
    cross-file union, since each file's slice is meaningless on its own.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        # Map of db_path -> sqlite3.Connection (lazy). Treat ``self._path``
        # as the canonical "directory marker" — the parent directory is
        # what we actually iterate. Keeping ``self._path`` preserves the
        # legacy constructor signature and the readiness check that
        # spawns a session-scoped reader against a single file.
        self._conns: dict[str, sqlite3.Connection] = {}

    # ── per-file connection management ────────────────────────────────

    def _wal_dir(self) -> Path:
        """Directory the reader aggregates across (parent of ``self._path``)."""
        return Path(self._path).parent

    def _db_paths(self) -> list[str]:
        """Every WAL DB file the reader should query.

        Falls back to ``[self._path]`` when:
          * the helper returns nothing (directory empty / missing — e.g.
            in tests that pass a non-WAL path), OR
          * ``self._path`` is a single-file caller (the readiness
            integrity check spawns ``LineageReader(session_file)`` for
            per-session chain verification).

        Mirrors the pattern in ``readiness/checks/integrity.py:_wal_paths``.
        """
        from gateway.wal.path import iter_wal_db_paths
        paths = iter_wal_db_paths(self._wal_dir())
        if paths:
            return paths
        # No files discovered in dir scan — fall back to the explicit
        # path the caller handed us. Preserves the single-file FileNotFound
        # semantics so the existing test contract (raise on missing DB)
        # still holds in ``_open_ro``.
        return [self._path]

    def _open_ro(self, db_path: str) -> sqlite3.Connection:
        """Open / fetch the cached read-only connection for ``db_path``.

        Each per-PID file gets its own connection, cached for the
        lifetime of the reader. ``check_same_thread=False`` so the
        connection can be shared with the FastAPI thread pool — every
        query is read-only and SQLite serialises reads internally.
        """
        conn = self._conns.get(db_path)
        if conn is not None:
            return conn
        if not Path(db_path).exists():
            raise FileNotFoundError(f"WAL database not found: {db_path}")
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB
        conn.row_factory = sqlite3.Row
        self._conns[db_path] = conn
        return conn

    def _iter_conns(self) -> list[sqlite3.Connection]:
        """All read-only connections, opening on first use.

        Per-file failures (file vanished, schema mismatch on a half-
        initialised worker DB) are skipped with a warning rather than
        aborting the whole query — a partial aggregation across the
        surviving workers beats blanking the dashboard.
        """
        conns: list[sqlite3.Connection] = []
        for p in self._db_paths():
            try:
                conns.append(self._open_ro(p))
            except FileNotFoundError:
                # Only the first/canonical path is allowed to raise so
                # we keep the "missing wal.db" error path identical to
                # pre-Phase-1.1 behaviour. iter_wal_db_paths only ever
                # returns files it just stat'd, so this branch fires
                # when the explicit fallback path doesn't exist AND
                # the directory scan returned nothing.
                if len(self._db_paths()) == 1:
                    raise
                logger.warning("WAL file vanished mid-read: %s", p)
        return conns

    def _query_all(
        self,
        sql: str,
        params: tuple = (),
    ) -> list[sqlite3.Row]:
        """Run a read-only SELECT against every WAL file; return concatenated rows.

        Caller decides how to fold (sum, dedup, sort, paginate). Per-file
        errors are logged once and skipped (a partial result beats a
        blanket query failure when one file is briefly unavailable —
        mirrors ``readiness/checks/integrity.py:_exec_wal_ro_all``).
        """
        out: list[sqlite3.Row] = []
        for conn in self._iter_conns():
            try:
                out.extend(conn.execute(sql, params).fetchall())
            except sqlite3.Error:
                logger.warning(
                    "WAL read failed on a worker file (continuing with others)",
                    exc_info=True,
                )
                continue
        return out

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()

    def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        sort: str = "last_activity",
        order: str = "desc",
    ) -> list[dict]:
        """List distinct sessions with record count and latest timestamp.

        *search* filters by substring match on session_id, model, user, or user_question (case-insensitive).
        *sort* is one of: last_activity, record_count, model. *order* is asc or desc.
        """
        sort_key = sort if sort in _SESSIONS_SORT_COLUMNS else "last_activity"
        order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
        order_expr = _SESSIONS_SORT_COLUMNS[sort_key]
        where_sql, extra_params = _sessions_search_where(search)
        # Multi-PID: fetch the full session set from EVERY worker file
        # (no LIMIT/OFFSET in the SQL), re-merge in Python, then paginate.
        # A session_id only ever lives in one worker's file (the worker
        # that handled the first request of the session), so the union
        # is naturally distinct — no dedup needed. The sort/limit must
        # be re-applied after the union since each file's slice is
        # meaningless on its own.
        sql = f"""
            SELECT
                s.session_id,
                s.record_count,
                s.user_message_count,
                s.last_activity,
                s.model,
                s.user,
                s.user_question,
                s.has_rag_context,
                s.has_files,
                s.has_images,
                s.request_type,
                s.meta_tool_strategy,
                s.meta_tool_count,
                COALESCE(t.tool_names, '') AS tool_names,
                COALESCE(t.tool_details, '') AS tool_details
            FROM ({_SESSIONS_AGG_SUBQUERY}) s
            LEFT JOIN ({_SESSIONS_TOOL_JOIN_SUBQUERY}) t ON t.session_id = s.session_id
            {where_sql}
            """
        merged = [dict(r) for r in self._query_all(sql, extra_params)]
        # Re-apply sort + pagination across the unioned set.
        reverse = order_sql == "DESC"

        def _sort_key(d: dict):
            if sort_key == "last_activity":
                return d.get("last_activity") or ""
            if sort_key == "record_count":
                return d.get("record_count") or 0
            return (d.get("model") or "")

        merged.sort(key=_sort_key, reverse=reverse)
        rows = merged[offset : offset + limit]

        # Batched fallback: one IN-query for ALL sessions that need metadata fallback,
        # instead of one query per session (previously N+1 with up to `limit` roundtrips).
        fallback_ids = [
            r["session_id"] for r in rows
            if not r.get("tool_names")
            and r.get("meta_tool_count") and r.get("meta_tool_count") > 0
        ]
        fallback_map = self._batch_extract_tool_names_from_metadata(fallback_ids)
        # C6 read-side: walk previous_record_id per session and surface
        # chain_status. The dashboard renders this on every session row;
        # without it every session shows the default green chip regardless of
        # actual chain integrity. SQLite path mirrors what the Walacor reader
        # does in `_compute_chain_status_map`.
        session_ids = [r["session_id"] for r in rows]
        chain_status_map = self._compute_chain_status_map(session_ids)

        results = []
        for d in rows:
            sid = d["session_id"]
            if sid in fallback_map:
                d["tool_names"], d["tool_details"] = fallback_map[sid]
            d.pop("meta_tool_strategy", None)
            d.pop("meta_tool_count", None)
            d["chain_status"] = chain_status_map.get(sid, "verified")
            results.append(d)
        return results

    def _compute_chain_status_map(self, session_ids: list[str]) -> dict[str, str]:
        """Walk each session's previous_record_id chain in one batched query.

        Returns ``{session_id: "verified" | "warn"}``. Linkage is "verified"
        when every record's `previous_record_id` equals the predecessor's
        `record_id` (and the first is None). Otherwise "warn".

        Single SQLite query for N sessions — same shape as the Walacor
        reader's `_compute_chain_status_map` so the dashboard sees the same
        contract on both backends. Fail-open: a SQL error returns an empty
        map and `chain_status` defaults to "verified" on the row.
        """
        if not session_ids:
            return {}
        placeholders = ",".join("?" for _ in session_ids)
        try:
            # Multi-PID: a session lives in exactly one worker's file,
            # so the union across files is naturally per-session. We
            # still sort in Python (per-file SQL sort orders within each
            # file, but the cross-file concat isn't globally ordered).
            rows = self._query_all(
                f"""SELECT session_id, sequence_number,
                          json_extract(record_json, '$.record_id') AS record_id,
                          json_extract(record_json, '$.previous_record_id') AS previous_record_id
                   FROM wal_records
                   WHERE event_type = 'execution'
                     AND session_id IN ({placeholders})
                   ORDER BY session_id, sequence_number,
                            json_extract(record_json, '$.record_id')""",
                tuple(session_ids),
            )
        except Exception:
            logger.warning("chain_status SQL failed (defaulting to verified)", exc_info=True)
            return {}

        from collections import defaultdict
        by_session: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_session[row["session_id"]].append({
                "record_id": row["record_id"],
                "previous_record_id": row["previous_record_id"],
                "sequence_number": row["sequence_number"],
            })
        # Re-sort per session (the cross-file union from _query_all isn't
        # globally ordered; SQL ORDER BY only applies within each file).
        for sid, recs in by_session.items():
            recs.sort(key=lambda r: (r.get("sequence_number") or 0, r.get("record_id") or ""))

        status: dict[str, str] = {}
        for sid, recs in by_session.items():
            expected_prev = None
            verified = True
            for r in recs:
                if r["previous_record_id"] != expected_prev:
                    verified = False
                    break
                expected_prev = r["record_id"]
            status[sid] = "verified" if verified else "warn"
        return status

    def _batch_extract_tool_names_from_metadata(
        self, session_ids: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Fallback: extract tool names from execution metadata JSON for many sessions.

        Runs a single query with `session_id IN (…)` instead of one query per
        session. Used when `tool_call` rows are missing from the WAL (older data
        or `_write_tool_events` failed) but the execution metadata indicates tools
        ran. Returns `{session_id: (names_csv, details_csv)}` — sessions with no
        extractable data are omitted.
        """
        if not session_ids:
            return {}
        placeholders = ",".join("?" for _ in session_ids)
        # Multi-PID: union across worker files; a session lives in one file.
        rows_iter = self._query_all(
            f"""SELECT session_id,
                       json_extract(record_json, '$.metadata.tool_interactions') AS ti
                FROM wal_records
                WHERE event_type = 'execution'
                  AND session_id IN ({placeholders})
                  AND json_extract(record_json, '$.metadata.tool_interaction_count') > 0
                ORDER BY timestamp ASC""",
            tuple(session_ids),
        )
        # Aggregate across all execution records per session (a session may have
        # multiple tool-augmented turns).
        agg: dict[str, tuple[set, set]] = {}
        for row in rows_iter:
            sid = row["session_id"]
            raw = row["ti"]
            if not raw:
                continue
            try:
                interactions = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            names, details = agg.setdefault(sid, (set(), set()))
            for ti in interactions:
                name = ti.get("tool_name") or ti.get("name") or ""
                source = ti.get("source") or ti.get("tool_type") or "unknown"
                if name:
                    names.add(name)
                    details.add(f"{name}:{source}")
        return {
            sid: (",".join(sorted(names)), ",".join(sorted(details)))
            for sid, (names, details) in agg.items()
            if names
        }

    def count_sessions(self, search: str | None = None) -> int:
        """Count sessions matching the same filters as list_sessions (excluding limit/offset)."""
        where_sql, extra_params = _sessions_search_where(search)
        # Multi-PID: a session lives in exactly one worker's file, so the
        # per-file COUNT(*)s are partitioned across the deployment and
        # sum cleanly. (No re-distinct needed — see ``list_sessions``.)
        sql = f"""
            SELECT COUNT(*) AS c FROM (
                SELECT s.session_id
                FROM ({_SESSIONS_AGG_SUBQUERY}) s
                LEFT JOIN ({_SESSIONS_TOOL_JOIN_SUBQUERY}) t ON t.session_id = s.session_id
                {where_sql}
            )
            """
        return sum(int(r[0] or 0) for r in self._query_all(sql, extra_params))

    def get_session_timeline(self, session_id: str, limit: int = 500) -> list[dict]:
        """Return execution records for a session, ordered by sequence_number.

        *limit* caps the result set so a runaway session (thousands of executions)
        can't exhaust dashboard memory or wire bandwidth. The dashboard timeline
        view only renders a paged subset; 500 is well above the practical display
        budget. Callers that need the full history can pass a larger value.
        """
        # Multi-PID: a session's records live in a single worker's
        # file, but we still union and re-sort defensively (cheaper than
        # branching, and protects against the migration corner case
        # where a session straddles a worker restart with a fresh PID
        # file). Sort key matches the original SQL ORDER BY.
        rows = self._query_all(
            """
            SELECT execution_id, record_json, created_at
            FROM wal_records
            WHERE session_id = ?
              AND event_type = 'execution'
            ORDER BY sequence_number ASC,
                     created_at ASC
            LIMIT ?
            """,
            (session_id, int(limit)),
        )
        results = []
        for row in rows:
            record = json.loads(row["record_json"])
            record["_wal_created_at"] = row["created_at"]
            _normalize_record(record)
            results.append(record)
        # Re-sort across the union, then re-apply the limit. Records
        # without sequence_number sort last (None coerced to -1 swaps
        # them to the front under ASC — use a sentinel that pushes them
        # to the end the same way SQLite's NULLS LAST default does).
        results.sort(key=lambda r: (
            r.get("sequence_number") if r.get("sequence_number") is not None else float("inf"),
            r.get("_wal_created_at") or "",
        ))
        return results[: int(limit)]

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        """Return full execution record by execution_id."""
        # Multi-PID: execution_id is globally unique, but only one
        # worker has the row. ``_query_all`` returns at most one row in
        # practice; take the first.
        rows = self._query_all(
            "SELECT record_json FROM wal_records WHERE execution_id = ?",
            (execution_id,),
        )
        if not rows:
            return None
        record = json.loads(rows[0]["record_json"])
        _normalize_record(record)
        return record

    def get_tool_events(self, execution_id: str) -> list[dict]:
        """Return tool event records linked to an execution."""
        # Multi-PID: tool events for an execution all land in the same
        # worker that handled the execution (the request never crosses
        # workers mid-call). Union+sort defensively anyway.
        rows = self._query_all(
            """
            SELECT record_json, timestamp
            FROM wal_records
            WHERE parent_execution_id = ?
              AND event_type = 'tool_call'
            ORDER BY timestamp ASC
            """,
            (execution_id,),
        )
        events = [json.loads(r["record_json"]) for r in rows]
        events.sort(key=lambda e: e.get("timestamp") or "")
        return events

    def get_execution_trace(self, execution_id: str) -> dict[str, Any] | None:
        """Return execution + tool events + timings for waterfall trace view."""
        execution = self.get_execution(execution_id)
        if not execution:
            return None
        tool_events = self.get_tool_events(execution_id)
        return {
            "execution": execution,
            "tool_events": tool_events,
            "timings": execution.get("timings") or {},
        }

    def get_attempts(
        self,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        sort: str = "timestamp",
        order: str = "desc",
        disposition: str | None = None,
    ) -> dict:
        """Return attempt records and disposition stats (same *search* filter for list, stats, total).

        *search* matches substring across request_id, tenant_id, provider, model_id, path,
        disposition, user, execution_id, and status_code (as text), case-insensitive.
        *sort*: timestamp, disposition, request_id, user, model_id, path, status_code.
        *disposition*: exact-match filter on the ``disposition`` column (additive to *search*).
        """
        sort_key = sort if sort in _ATTEMPTS_SORT_COLUMNS else "timestamp"
        order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
        order_expr = _ATTEMPTS_SORT_COLUMNS[sort_key]
        where_sql, extra_params = _attempts_search_where(search)
        if disposition is not None:
            if where_sql:
                where_sql = f"{where_sql} AND disposition = ?"
            else:
                where_sql = "WHERE disposition = ?"
            extra_params = (*extra_params, disposition)
        base = f"FROM gateway_attempts {where_sql}"

        # Multi-PID: each worker has its own attempts table partition.
        # Fetch all rows from every file (no LIMIT/OFFSET in SQL),
        # re-sort + paginate across the union. Stats and total are
        # summed across files.
        items_rows = self._query_all(
            f"""
            SELECT request_id, timestamp, tenant_id, provider, model_id,
                   path, disposition, execution_id, status_code, user, reason
            {base}
            """,
            extra_params,
        )
        items_all = [dict(row) for row in items_rows]
        reverse = order_sql == "DESC"

        def _attempt_sort_key(d: dict):
            if sort_key == "timestamp":
                return d.get("timestamp") or ""
            if sort_key == "status_code":
                # NULLs sort low under ASC and high under DESC — match
                # SQLite's default by coercing to -1 (stable across the
                # union and identical to the original single-file
                # ordering for non-null rows).
                return d.get("status_code") if d.get("status_code") is not None else -1
            col = sort_key
            return d.get(col) or ""

        items_all.sort(key=_attempt_sort_key, reverse=reverse)
        items = items_all[offset : offset + limit]

        # Stats: per-file COUNT(*) GROUP BY disposition, then sum across
        # files keyed by disposition. (A worker that's never seen a
        # given disposition contributes 0 implicitly.)
        stats: dict[str, int] = {}
        for row in self._query_all(
            f"SELECT disposition, COUNT(*) AS count {base} GROUP BY disposition",
            extra_params,
        ):
            disp = row["disposition"]
            stats[disp] = stats.get(disp, 0) + int(row["count"] or 0)

        total = sum(
            int(r["total"] or 0)
            for r in self._query_all(f"SELECT COUNT(*) AS total {base}", extra_params)
        )

        return {"items": items, "stats": stats, "total": total}

    _RANGE_CONFIG: dict[str, tuple[str, str]] = {
        # range_key -> (lookback SQL interval, strftime bucket format)
        "1h":  ("-1 hour",  "%Y-%m-%dT%H:%M:00"),   # 1-minute buckets
        "24h": ("-24 hours", "%Y-%m-%dT%H:00:00"),    # 1-hour buckets (show mins as 00)
        "7d":  ("-7 days",  "%Y-%m-%dT%H:00:00"),     # 1-hour buckets
        "30d": ("-30 days", "%Y-%m-%dT%H:00:00"),     # 1-hour buckets (aggregated client-side if needed)
    }

    def get_metrics_history(self, range_key: str) -> dict:
        """Return time-bucketed attempt counts for charting.

        range_key: "1h" | "24h" | "7d" | "30d"
        Returns: {buckets: [{t, total, allowed, blocked}], range: str}

        Includes every bucket in the window (zeros where there was no traffic) so charts show
        the full last hour / day / week rather than only non-empty intervals.
        """
        if range_key not in self._RANGE_CONFIG:
            range_key = "1h"
        _, fmt = self._RANGE_CONFIG[range_key]
        labels, t_low, t_high = _metrics_timeline_labels(range_key)
        # Multi-PID: each worker partitions its own attempts by bucket;
        # the same bucket key may exist in multiple worker files and the
        # counts must be **summed**, not concatenated.
        rows = self._query_all(
            f"""
            SELECT
                strftime('{fmt}', timestamp) AS t,
                COUNT(*) AS total,
                SUM(CASE WHEN disposition IN ('forwarded', 'allowed') THEN 1 ELSE 0 END) AS allowed,
                SUM(CASE WHEN disposition NOT IN ('forwarded', 'allowed') THEN 1 ELSE 0 END) AS blocked
            FROM gateway_attempts
            WHERE timestamp >= ? AND timestamp < ?
            GROUP BY t
            ORDER BY t ASC
            """,
            (t_low, t_high),
        )
        by_t: dict[str, dict[str, int]] = {}
        for row in rows:
            key = row["t"]
            slot = by_t.setdefault(key, {"total": 0, "allowed": 0, "blocked": 0})
            slot["total"] += int(row["total"] or 0)
            slot["allowed"] += int(row["allowed"] or 0)
            slot["blocked"] += int(row["blocked"] or 0)
        buckets = []
        for t in labels:
            slot = by_t.get(t)
            if slot is None:
                buckets.append({"t": t, "total": 0, "allowed": 0, "blocked": 0})
            else:
                buckets.append({"t": t, **slot})
        return {"buckets": buckets, "range": range_key}

    def get_token_latency_history(self, range_key: str) -> dict:
        """Return time-bucketed token usage and latency aggregates for charting.

        Returns: {buckets: [{t, prompt_tokens, completion_tokens, total_tokens,
                             avg_latency_ms, max_latency_ms, request_count}], range: str}

        Uses the same UTC window as :meth:`get_metrics_history` (parameterized bounds) so
        JSON ``$.timestamp`` values (ISO-8601 from execution records) align with
        ``gateway_attempts`` charts. Emits a full timeline with zeros for empty buckets.
        """
        if range_key not in self._RANGE_CONFIG:
            range_key = "1h"
        _, fmt = self._RANGE_CONFIG[range_key]
        labels, t_low, t_high = _metrics_timeline_labels(range_key)
        # Multi-PID: per-worker buckets must be re-aggregated. SUM/MAX
        # are trivially mergeable; AVG must be reconstructed from
        # ``SUM(latency) / SUM(count)`` — we pull per-file sums via
        # ``avg * count`` so a worker with zero rows contributes nothing.
        rows = self._query_all(
            f"""
            SELECT
                strftime('{fmt}', timestamp) AS t,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(latency_ms), 0) AS sum_latency_ms,
                COALESCE(MAX(latency_ms), 0) AS max_latency_ms,
                COUNT(*) AS request_count
            FROM wal_records
            WHERE timestamp >= ? AND timestamp < ?
              AND event_type = 'execution'
            GROUP BY t
            HAVING t IS NOT NULL
            ORDER BY t ASC
            """,
            (t_low, t_high),
        )
        by_t: dict[str, dict[str, float]] = {}
        for row in rows:
            key = row["t"]
            slot = by_t.setdefault(key, {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "sum_latency_ms": 0.0,
                "max_latency_ms": 0.0,
                "request_count": 0,
            })
            slot["prompt_tokens"] += int(row["prompt_tokens"] or 0)
            slot["completion_tokens"] += int(row["completion_tokens"] or 0)
            slot["total_tokens"] += int(row["total_tokens"] or 0)
            slot["sum_latency_ms"] += float(row["sum_latency_ms"] or 0.0)
            slot["max_latency_ms"] = max(slot["max_latency_ms"], float(row["max_latency_ms"] or 0.0))
            slot["request_count"] += int(row["request_count"] or 0)
        buckets: list[dict[str, Any]] = []
        for t in labels:
            slot = by_t.get(t)
            if slot is None:
                buckets.append({
                    "t": t,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "avg_latency_ms": 0,
                    "max_latency_ms": 0,
                    "request_count": 0,
                })
            else:
                count = slot["request_count"]
                avg = (slot["sum_latency_ms"] / count) if count else 0.0
                buckets.append({
                    "t": t,
                    "prompt_tokens": slot["prompt_tokens"],
                    "completion_tokens": slot["completion_tokens"],
                    "total_tokens": slot["total_tokens"],
                    "avg_latency_ms": round(avg, 1),
                    "max_latency_ms": round(slot["max_latency_ms"], 1),
                    "request_count": count,
                })
        return {"buckets": buckets, "range": range_key}

    # ── Compliance query methods (Phase 24) ────────────────────────────────

    def get_compliance_summary(self, start: str, end: str) -> dict:
        """Aggregate stats for compliance report: total requests, pass/fail rates,
        model usage, content analysis summary, chain integrity."""
        # Multi-PID: sum scalar aggregates across worker files.
        total = allowed = denied = 0
        for row in self._query_all(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN disposition IN ('forwarded', 'allowed') THEN 1 ELSE 0 END) AS allowed,
                SUM(CASE WHEN disposition NOT IN ('forwarded', 'allowed') THEN 1 ELSE 0 END) AS denied
            FROM gateway_attempts
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start, end),
        ):
            total += int(row["total"] or 0)
            allowed += int(row["allowed"] or 0)
            denied += int(row["denied"] or 0)

        # Models used in period — union of per-worker DISTINCT sets.
        models_used = sorted({
            r["model_id"] for r in self._query_all(
                """
                SELECT DISTINCT model_id
                FROM wal_records
                WHERE timestamp >= ? AND timestamp < ?
                  AND event_type = 'execution'
                  AND model_id IS NOT NULL
                """,
                (start, end),
            )
            if r["model_id"] is not None
        })

        # Content-analysis coverage: percent of window executions whose
        # JSON payload actually carries analyzer output. The local WAL
        # stores the full record body in `record_json`; we probe two
        # shapes — top-level `content_analysis` or
        # `metadata.analyzer_decisions` — that the orchestrator writes.
        rows3 = self._query_all(
            """
            SELECT record_json FROM wal_records
             WHERE timestamp >= ? AND timestamp < ?
               AND event_type = 'execution'
            """,
            (start, end),
        )
        total_exec = 0
        analyzed = 0
        import json as _json
        for r in rows3:
            total_exec += 1
            try:
                rec = _json.loads(r["record_json"] or "{}")
            except Exception:
                continue
            if rec.get("content_analysis"):
                analyzed += 1
                continue
            meta = rec.get("metadata") or {}
            if isinstance(meta, dict) and (meta.get("analyzer_decisions") or meta.get("content_analysis")):
                analyzed += 1
        coverage_pct = round(analyzed / total_exec * 100, 1) if total_exec else 0.0

        return {
            "total_requests": total,
            "allowed": allowed,
            "denied": denied,
            "models_used": models_used,
            "total_executions": total_exec,
            "content_analysis_covered": analyzed,
            "content_analysis_coverage_pct": coverage_pct,
        }

    def get_execution_export(self, start: str, end: str, limit: int = 10000) -> list[dict]:
        """Full execution records for date range (JSON/CSV export)."""
        # Multi-PID: each worker contributes up to ``limit`` rows; union,
        # re-sort by timestamp, then truncate. Pulling the full per-file
        # cap and re-applying the limit cross-worker is the only safe
        # ordering — pulling ``limit / N`` per file could miss recent
        # rows on a hot worker.
        rows = self._query_all(
            """
            SELECT record_json, timestamp
            FROM wal_records
            WHERE timestamp >= ? AND timestamp < ?
              AND event_type = 'execution'
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (start, end, limit),
        )
        decoded = [(r["timestamp"] or "", json.loads(r["record_json"])) for r in rows]
        decoded.sort(key=lambda t: t[0])
        records = [rec for _, rec in decoded[:limit]]
        for r in records:
            _normalize_record(r)
        return records

    def get_attestation_summary(self, start: str, end: str) -> list[dict]:
        """Model attestation inventory with usage counts in period."""
        # Multi-PID: re-aggregate GROUP BY (model_id, provider) across
        # worker files (each worker only has its share of requests).
        rows = self._query_all(
            """
            SELECT
                model_id,
                provider,
                json_extract(record_json, '$.model_attestation_id') AS attestation_id,
                COUNT(*) AS request_count,
                SUM(COALESCE(total_tokens, 0)) AS total_tokens
            FROM wal_records
            WHERE timestamp >= ? AND timestamp < ?
              AND event_type = 'execution'
              AND model_id IS NOT NULL
            GROUP BY model_id, provider
            ORDER BY request_count DESC
            """,
            (start, end),
        )
        merged: dict[tuple, dict] = {}
        for row in rows:
            key = (row["model_id"], row["provider"])
            slot = merged.get(key)
            if slot is None:
                merged[key] = {
                    "model_id": row["model_id"],
                    "provider": row["provider"],
                    "attestation_id": row["attestation_id"],
                    "request_count": int(row["request_count"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                }
            else:
                slot["request_count"] += int(row["request_count"] or 0)
                slot["total_tokens"] += int(row["total_tokens"] or 0)
                # Keep any non-null attestation_id we see.
                if not slot["attestation_id"] and row["attestation_id"]:
                    slot["attestation_id"] = row["attestation_id"]
        result = list(merged.values())
        result.sort(key=lambda d: d["request_count"], reverse=True)
        return result

    def get_chain_verification_report(
        self, start: str, end: str, sample_limit: int = 50,
    ) -> list[dict]:
        """Verify chain integrity for a bounded sample of sessions in [start, end).

        Still works for ad-hoc one-offs (CLI debugging, custom scripts,
        the background ``ChainIntegrityWorker`` which calls this with a
        very large ``sample_limit`` to get a census). The dashboard's
        ``/v1/compliance/export`` endpoint NO LONGER calls this directly
        — it reads precomputed results from
        ``gateway.compliance.chain_store.ChainVerificationStore``,
        populated by the background worker. See
        ``gateway.compliance.chain_worker`` for the worker contract.
        """
        # Multi-PID: per-worker LIMITs miss top-N globally. Pull each
        # worker's top-sample_limit, union, re-sort by latest_ts, take
        # the global top sample_limit. Per-session aggregation is safe
        # (a session lives in one worker).
        rows = self._query_all(
            """
            SELECT session_id, MAX(timestamp) AS latest_ts
            FROM wal_records
            WHERE timestamp >= ? AND timestamp < ?
              AND session_id IS NOT NULL
              AND event_type = 'execution'
            GROUP BY session_id
            ORDER BY latest_ts DESC
            LIMIT ?
            """,
            (start, end, sample_limit),
        )
        unioned = [(r["latest_ts"] or "", r["session_id"]) for r in rows]
        unioned.sort(key=lambda t: t[0], reverse=True)
        session_ids = [sid for _, sid in unioned[:sample_limit]]
        return [self.verify_chain(sid) for sid in session_ids]

    def count_sessions_in_window(self, start: str, end: str) -> int:
        """Total distinct sessions with an execution in [start, end).

        Used by the compliance endpoint to disclose how many sessions
        actually fell in the window vs. how many were verified.
        """
        # Multi-PID: a session_id lives in exactly one worker file, so
        # per-file ``COUNT(DISTINCT session_id)`` partitions cleanly and
        # the sum is the global count. (No cross-file re-distinct.)
        return sum(
            int(r["n"] or 0)
            for r in self._query_all(
                """
                SELECT COUNT(DISTINCT session_id) AS n
                FROM wal_records
                WHERE timestamp >= ? AND timestamp < ?
                  AND session_id IS NOT NULL
                  AND event_type = 'execution'
                """,
                (start, end),
            )
        )

    def get_cost_summary(self, range_key: str = "24h", group_by: str = "model") -> dict:
        """Aggregate estimated costs by model or user over a time range."""
        interval_map = {"1h": "-1 hour", "24h": "-1 day", "7d": "-7 days", "30d": "-30 days"}
        interval = interval_map.get(range_key, "-1 day")

        if group_by == "user":
            group_col = "user"
            group_alias = "user"
        else:
            group_col = "model_id"
            group_alias = "model"

        sql = f"""
            SELECT
                {group_col} AS group_key,
                COUNT(*) AS request_count,
                SUM(COALESCE(prompt_tokens, 0)) AS total_prompt_tokens,
                SUM(COALESCE(completion_tokens, 0)) AS total_completion_tokens,
                SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                SUM(COALESCE(json_extract(record_json, '$.estimated_cost_usd'), 0.0)) AS total_cost_usd
            FROM wal_records
            WHERE timestamp > datetime('now', ?)
              AND event_type = 'execution'
            GROUP BY group_key
            ORDER BY total_cost_usd DESC
        """

        # Multi-PID: merge GROUP BY group_key across worker files; sum
        # counters, accumulate cost. The original per-file ORDER BY is
        # meaningless after the union, so we re-sort by cost at the end.
        merged: dict[Any, dict] = {}
        for row in self._query_all(sql, (interval,)):
            key = row["group_key"] or "unknown"
            slot = merged.get(key)
            if slot is None:
                merged[key] = {
                    group_alias: key,
                    "request_count": int(row["request_count"] or 0),
                    "prompt_tokens": int(row["total_prompt_tokens"] or 0),
                    "completion_tokens": int(row["total_completion_tokens"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                    "cost_usd": float(row["total_cost_usd"] or 0.0),
                }
            else:
                slot["request_count"] += int(row["request_count"] or 0)
                slot["prompt_tokens"] += int(row["total_prompt_tokens"] or 0)
                slot["completion_tokens"] += int(row["total_completion_tokens"] or 0)
                slot["total_tokens"] += int(row["total_tokens"] or 0)
                slot["cost_usd"] += float(row["total_cost_usd"] or 0.0)

        rows = sorted(merged.values(), key=lambda e: e["cost_usd"], reverse=True)
        grand_total = sum(e["cost_usd"] for e in rows)
        for e in rows:
            e["cost_usd"] = round(e["cost_usd"], 6)

        return {
            "range": range_key,
            "group_by": group_by,
            "entries": rows,
            "grand_total_usd": round(grand_total, 6),
        }

    def get_attachments(self, session_id: str) -> list[dict]:
        """Get all file_metadata entries from execution records in a session."""
        # Multi-PID: a session's records live in one file in practice,
        # but union defensively for the worker-restart edge case.
        rows = self._query_all(
            "SELECT record_json FROM wal_records WHERE session_id = ? AND event_type = 'execution'",
            (session_id,),
        )
        attachments = []
        for row in rows:
            record = json.loads(row["record_json"])
            for fm in record.get("file_metadata", []):
                fm["execution_id"] = record.get("execution_id", "")
                attachments.append(fm)
        return attachments

    def verify_chain(self, session_id: str) -> dict:
        """Verify chain integrity and authenticity for a session.

        Three independent checks per record:
          1. **Structural** — sequence_number is contiguous and
             previous_record_id links to the prior record's record_id.
          2. **Signature** — Ed25519 signature over the canonical
             (record_id | previous_record_id | sequence_number | execution_id | timestamp)
             string verifies against the loaded verify key.
          3. **Anchor** — walacor_block_id/trans_id/dh are all present
             (the envelope was sealed by the Walacor backend on ingest).

        The top-level ``valid`` is False if ANY structural check fails OR any
        present signature fails to verify. Missing signatures / missing anchors
        don't fail the chain — they're reported so callers can see coverage.
        """
        from gateway.crypto.signing import verify_record_signature, signing_key_available

        records = self.get_session_timeline(session_id)
        if not records:
            return _empty_verify_result(session_id)

        errors: list[str] = []
        # Seed from first record so legacy chains validate correctly.
        expected_prev_id: str | None = records[0].get("previous_record_id")
        per_record: list[dict] = []
        sig_valid = sig_invalid = sig_absent = sig_unverifiable = 0
        anchor_ok = anchor_missing = 0

        for i, rec in enumerate(records):
            seq = rec.get("sequence_number")
            rec_id = rec.get("record_id")
            prev_id = rec.get("previous_record_id")
            execution_id = rec.get("execution_id", "")
            structural_ok = True

            if seq is not None and seq != i:
                errors.append(
                    f"sequence gap at record {i}: expected {i}, got {seq} (execution_id={execution_id})"
                )
                structural_ok = False

            if prev_id != expected_prev_id:
                errors.append(
                    f"id pointer mismatch at sequence {i}: "
                    f"expected previous_record_id={expected_prev_id!r}, got {prev_id!r} (execution_id={execution_id})"
                )
                structural_ok = False

            expected_prev_id = rec_id

            sig_status = verify_record_signature(rec)
            if sig_status == "valid":
                sig_valid += 1
            elif sig_status == "invalid":
                sig_invalid += 1
                errors.append(
                    f"signature invalid at sequence {i} (execution_id={execution_id})"
                )
            elif sig_status == "unverifiable":
                sig_unverifiable += 1
            else:
                sig_absent += 1

            has_anchor = bool(rec.get("walacor_block_id") and rec.get("walacor_trans_id") and rec.get("walacor_dh"))
            if has_anchor:
                # SQLite reader does NOT do anchor round-trips (it only reads
                # the local WAL — no live Walacor connection). The strongest
                # claim it can make about an anchor is "present", not
                # "verified". This is the C2 fix on the SQLite path: the
                # session-level `valid` no longer treats "present without
                # round-trip" as a positive proof.
                anchor_ok += 1
                anchor_status = "present"
            else:
                anchor_missing += 1
                anchor_status = "absent"

            # Per-record validity (C6 read-side): true unless this row has a
            # structural break or an invalid signature.
            record_valid = structural_ok and sig_status != "invalid"

            per_record.append({
                "execution_id": execution_id,
                "sequence_number": seq,
                "record_id": rec_id,
                "structural_ok": structural_ok,
                "signature": sig_status,
                "anchor": anchor_status,
                "valid": record_valid,
                "walacor_block_id": rec.get("walacor_block_id"),
                "walacor_trans_id": rec.get("walacor_trans_id"),
                "walacor_dh": rec.get("walacor_dh"),
            })

        # C2: derive verification_level. SQLite has no live Walacor link, so
        # the strongest possible level is "structural" — anchor round-trips
        # are exclusively the WalacorLineageReader's job. Even with all
        # anchors "present", the SQLite reader cannot say "verified".
        #
        # The top-level `valid` keeps its legacy semantics on the SQLite path
        # (structural + signature-clean = True) so existing tests and the
        # dashboard's "verify chain" button on local-only deployments stay
        # green. `verification_level: structural` tells the dashboard to
        # render the yellow / "structural" badge rather than the green
        # "fully verified" one — that's the dashboard's job.
        if len(errors) > 0 or sig_invalid > 0:
            verification_level = "unverifiable"
        else:
            # Either structurally clean (anchors absent on every node — legacy
            # pre-anchor records) or structurally clean + every anchor field
            # present locally. Either way, no round-trip evidence is possible
            # without the Walacor reader.
            verification_level = "structural"

        return {
            "valid": len(errors) == 0,
            "verification_level": verification_level,
            "records_checked": len(records),
            "errors": errors,
            "session_id": session_id,
            "checks": {
                "structural": {"passed": sum(1 for r in per_record if r["structural_ok"]),
                               "failed": sum(1 for r in per_record if not r["structural_ok"])},
                "signatures": {"valid": sig_valid, "invalid": sig_invalid,
                               "absent": sig_absent, "unverifiable": sig_unverifiable,
                               "verify_key_loaded": signing_key_available()},
                "anchors":    {"verified": 0, "present": anchor_ok, "absent": anchor_missing,
                               "mismatched": 0, "unverifiable": 0,
                               "independent_roundtrip": False},
            },
            "records": per_record,
            # Back-compat: old clients read walacor_attestation[].
            "walacor_attestation": [
                {
                    "record_id": r["record_id"],
                    "walacor_block_id": r["walacor_block_id"],
                    "walacor_trans_id": r["walacor_trans_id"],
                    "walacor_dh": r["walacor_dh"],
                }
                for r in per_record
            ],
        }

    def get_ab_test_results(self, test_name: str) -> dict:
        """Aggregate execution stats by model variant for a given A/B test.

        Groups rows by model_id for all execution records that carry the
        matching ``ab_variant`` metadata field, and returns per-variant
        request counts, average latency, and total token usage so callers
        can compare quality/cost/latency across variants.
        """
        # Multi-PID: re-aggregate GROUP BY model_id across worker files.
        # AVG values must be reconstructed from SUM/COUNT, since two
        # per-file averages can't be combined directly. Pull SUMs only;
        # derive avgs in Python.
        rows = self._query_all(
            """
            SELECT
                model_id                                                   AS model_id,
                json_extract(record_json, '$.metadata.ab_variant')         AS ab_variant,
                json_extract(record_json, '$.metadata.ab_original_model')  AS original_model,
                COUNT(*)                                                   AS request_count,
                SUM(COALESCE(latency_ms, 0))                               AS sum_latency_ms,
                SUM(COALESCE(total_tokens, 0))                             AS total_tokens
            FROM wal_records
            WHERE event_type = 'execution'
              AND json_extract(record_json, '$.metadata.ab_variant') = ?
            GROUP BY model_id
            """,
            (test_name,),
        )
        merged: dict[Any, dict] = {}
        for row in rows:
            mid = row["model_id"]
            slot = merged.get(mid)
            if slot is None:
                merged[mid] = {
                    "model_id": mid,
                    "ab_variant": row["ab_variant"],
                    "original_model": row["original_model"],
                    "request_count": int(row["request_count"] or 0),
                    "sum_latency_ms": float(row["sum_latency_ms"] or 0.0),
                    "total_tokens": int(row["total_tokens"] or 0),
                }
            else:
                slot["request_count"] += int(row["request_count"] or 0)
                slot["sum_latency_ms"] += float(row["sum_latency_ms"] or 0.0)
                slot["total_tokens"] += int(row["total_tokens"] or 0)

        variants = []
        for slot in sorted(merged.values(), key=lambda d: d["request_count"], reverse=True):
            count = slot["request_count"]
            avg_lat = (slot["sum_latency_ms"] / count) if count else None
            avg_tok = (slot["total_tokens"] / count) if count else None
            variants.append({
                "model_id": slot["model_id"],
                "ab_variant": slot["ab_variant"],
                "original_model": slot["original_model"],
                "request_count": count,
                "avg_latency_ms": round(avg_lat, 1) if avg_lat is not None else None,
                "total_tokens": slot["total_tokens"],
                "avg_tokens": round(avg_tok, 1) if avg_tok is not None else None,
            })

        return {"test_name": test_name, "variants": variants, "total_requests": sum(v["request_count"] for v in variants)}
