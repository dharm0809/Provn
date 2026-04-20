"""Read-only SQLite reader for the WAL database. Separate connection from WALWriter."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gateway.core import compute_sha3_512_string

from gateway.lineage._normalize import normalize_record as _normalize_record
from gateway.pipeline.session_chain import GENESIS_HASH

logger = logging.getLogger(__name__)

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
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: sqlite3.Connection | None = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if not Path(self._path).exists():
                raise FileNotFoundError(f"WAL database not found: {self._path}")
            uri = f"file:{self._path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA mmap_size=268435456")  # 256MB
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

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
            ORDER BY {order_expr} {order_sql}
            LIMIT ? OFFSET ?
            """
        conn = self._ensure_conn()
        cur = conn.execute(sql, (*extra_params, limit, offset))
        rows = [dict(row) for row in cur.fetchall()]

        # Batched fallback: one IN-query for ALL sessions that need metadata fallback,
        # instead of one query per session (previously N+1 with up to `limit` roundtrips).
        fallback_ids = [
            r["session_id"] for r in rows
            if not r.get("tool_names")
            and r.get("meta_tool_count") and r.get("meta_tool_count") > 0
        ]
        fallback_map = self._batch_extract_tool_names_from_metadata(fallback_ids)

        results = []
        for d in rows:
            if d["session_id"] in fallback_map:
                d["tool_names"], d["tool_details"] = fallback_map[d["session_id"]]
            d.pop("meta_tool_strategy", None)
            d.pop("meta_tool_count", None)
            results.append(d)
        return results

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
        conn = self._ensure_conn()
        placeholders = ",".join("?" for _ in session_ids)
        cur = conn.execute(
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
        for row in cur.fetchall():
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
        sql = f"""
            SELECT COUNT(*) AS c FROM (
                SELECT s.session_id
                FROM ({_SESSIONS_AGG_SUBQUERY}) s
                LEFT JOIN ({_SESSIONS_TOOL_JOIN_SUBQUERY}) t ON t.session_id = s.session_id
                {where_sql}
            )
            """
        conn = self._ensure_conn()
        cur = conn.execute(sql, extra_params)
        return int(cur.fetchone()[0] or 0)

    def get_session_timeline(self, session_id: str, limit: int = 500) -> list[dict]:
        """Return execution records for a session, ordered by sequence_number.

        *limit* caps the result set so a runaway session (thousands of executions)
        can't exhaust dashboard memory or wire bandwidth. The dashboard timeline
        view only renders a paged subset; 500 is well above the practical display
        budget. Callers that need the full history can pass a larger value.
        """
        conn = self._ensure_conn()
        cur = conn.execute(
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
        for row in cur.fetchall():
            record = json.loads(row["record_json"])
            record["_wal_created_at"] = row["created_at"]
            _normalize_record(record)
            results.append(record)
        return results

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        """Return full execution record by execution_id."""
        conn = self._ensure_conn()
        cur = conn.execute(
            "SELECT record_json FROM wal_records WHERE execution_id = ?",
            (execution_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        _normalize_record(record)
        return record

    def get_tool_events(self, execution_id: str) -> list[dict]:
        """Return tool event records linked to an execution."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            SELECT record_json
            FROM wal_records
            WHERE parent_execution_id = ?
              AND event_type = 'tool_call'
            ORDER BY timestamp ASC
            """,
            (execution_id,),
        )
        return [json.loads(row["record_json"]) for row in cur.fetchall()]

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
    ) -> dict:
        """Return attempt records and disposition stats (same *search* filter for list, stats, total).

        *search* matches substring across request_id, tenant_id, provider, model_id, path,
        disposition, user, execution_id, and status_code (as text), case-insensitive.
        *sort*: timestamp, disposition, request_id, user, model_id, path, status_code.
        """
        sort_key = sort if sort in _ATTEMPTS_SORT_COLUMNS else "timestamp"
        order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
        order_expr = _ATTEMPTS_SORT_COLUMNS[sort_key]
        where_sql, extra_params = _attempts_search_where(search)
        base = f"FROM gateway_attempts {where_sql}"

        conn = self._ensure_conn()
        cur = conn.execute(
            f"""
            SELECT request_id, timestamp, tenant_id, provider, model_id,
                   path, disposition, execution_id, status_code, user, reason
            {base}
            ORDER BY {order_expr} {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*extra_params, limit, offset),
        )
        items = [dict(row) for row in cur.fetchall()]

        cur2 = conn.execute(
            f"SELECT disposition, COUNT(*) AS count {base} GROUP BY disposition",
            extra_params,
        )
        stats = {row["disposition"]: row["count"] for row in cur2.fetchall()}

        total_cur = conn.execute(f"SELECT COUNT(*) AS total {base}", extra_params)
        total = int(total_cur.fetchone()["total"] or 0)

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
        conn = self._ensure_conn()
        cur = conn.execute(
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
        by_t = {row["t"]: row for row in cur.fetchall()}
        buckets = []
        for t in labels:
            row = by_t.get(t)
            if row is None:
                buckets.append({"t": t, "total": 0, "allowed": 0, "blocked": 0})
            else:
                buckets.append({
                    "t": t,
                    "total": row["total"],
                    "allowed": row["allowed"] or 0,
                    "blocked": row["blocked"] or 0,
                })
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
        conn = self._ensure_conn()
        cur = conn.execute(
            f"""
            SELECT
                strftime('{fmt}', timestamp) AS t,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
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
        by_t = {row["t"]: row for row in cur.fetchall()}
        buckets: list[dict[str, Any]] = []
        for t in labels:
            row = by_t.get(t)
            if row is None:
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
                buckets.append({
                    "t": t,
                    "prompt_tokens": row["prompt_tokens"] or 0,
                    "completion_tokens": row["completion_tokens"] or 0,
                    "total_tokens": row["total_tokens"] or 0,
                    "avg_latency_ms": round(row["avg_latency_ms"] or 0, 1),
                    "max_latency_ms": round(row["max_latency_ms"] or 0, 1),
                    "request_count": row["request_count"] or 0,
                })
        return {"buckets": buckets, "range": range_key}

    # ── Compliance query methods (Phase 24) ────────────────────────────────

    def get_compliance_summary(self, start: str, end: str) -> dict:
        """Aggregate stats for compliance report: total requests, pass/fail rates,
        model usage, content analysis summary, chain integrity."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN disposition IN ('forwarded', 'allowed') THEN 1 ELSE 0 END) AS allowed,
                SUM(CASE WHEN disposition NOT IN ('forwarded', 'allowed') THEN 1 ELSE 0 END) AS denied
            FROM gateway_attempts
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start, end),
        )
        row = cur.fetchone()
        total = row["total"] or 0
        allowed = row["allowed"] or 0
        denied = row["denied"] or 0

        # Models used in period
        cur2 = conn.execute(
            """
            SELECT DISTINCT model_id
            FROM wal_records
            WHERE timestamp >= ? AND timestamp < ?
              AND event_type = 'execution'
              AND model_id IS NOT NULL
            """,
            (start, end),
        )
        models_used = [r["model_id"] for r in cur2.fetchall()]

        return {
            "total_requests": total,
            "allowed": allowed,
            "denied": denied,
            "models_used": models_used,
        }

    def get_execution_export(self, start: str, end: str, limit: int = 10000) -> list[dict]:
        """Full execution records for date range (JSON/CSV export)."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            SELECT record_json
            FROM wal_records
            WHERE timestamp >= ? AND timestamp < ?
              AND event_type = 'execution'
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (start, end, limit),
        )
        records = [json.loads(row["record_json"]) for row in cur.fetchall()]
        for r in records:
            _normalize_record(r)
        return records

    def get_attestation_summary(self, start: str, end: str) -> list[dict]:
        """Model attestation inventory with usage counts in period."""
        conn = self._ensure_conn()
        cur = conn.execute(
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
        return [dict(row) for row in cur.fetchall()]

    def get_chain_verification_report(self, start: str, end: str) -> list[dict]:
        """Run verify_chain for all sessions active in period, return results."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            SELECT DISTINCT session_id
            FROM wal_records
            WHERE timestamp >= ? AND timestamp < ?
              AND session_id IS NOT NULL
              AND event_type = 'execution'
            """,
            (start, end),
        )
        session_ids = [row["session_id"] for row in cur.fetchall()]
        return [self.verify_chain(sid) for sid in session_ids]

    def get_cost_summary(self, range_key: str = "24h", group_by: str = "model") -> dict:
        """Aggregate estimated costs by model or user over a time range."""
        conn = self._ensure_conn()

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

        cur = conn.execute(sql, (interval,))
        rows = []
        grand_total = 0.0
        for row in cur.fetchall():
            entry = {
                group_alias: row["group_key"] or "unknown",
                "request_count": row["request_count"],
                "prompt_tokens": row["total_prompt_tokens"],
                "completion_tokens": row["total_completion_tokens"],
                "total_tokens": row["total_tokens"],
                "cost_usd": round(row["total_cost_usd"], 6),
            }
            grand_total += row["total_cost_usd"]
            rows.append(entry)

        return {
            "range": range_key,
            "group_by": group_by,
            "entries": rows,
            "grand_total_usd": round(grand_total, 6),
        }

    def get_attachments(self, session_id: str) -> list[dict]:
        """Get all file_metadata entries from execution records in a session."""
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT record_json FROM wal_records WHERE session_id = ? AND event_type = 'execution'",
            (session_id,),
        ).fetchall()
        attachments = []
        for (record_json,) in rows:
            record = json.loads(record_json)
            for fm in record.get("file_metadata", []):
                fm["execution_id"] = record.get("execution_id", "")
                attachments.append(fm)
        return attachments

    def verify_chain(self, session_id: str) -> dict:
        """Verify chain integrity for a session by walking record_id pointers.

        Checks: sequence numbers are contiguous, each record's previous_record_id
        matches the prior record's record_id. Returns Walacor attestation fields
        per record so callers can surface blockchain provenance.
        """
        records = self.get_session_timeline(session_id)
        if not records:
            return {
                "valid": True,
                "records_checked": 0,
                "errors": [],
                "session_id": session_id,
                "walacor_attestation": [],
            }

        errors: list[str] = []
        # Seed from first record so legacy chains (previous_record_id = "legacy:000...")
        # validate correctly alongside new chains (previous_record_id = None for seq 0).
        expected_prev_id: str | None = records[0].get("previous_record_id") if records else None

        for i, rec in enumerate(records):
            seq = rec.get("sequence_number")
            rec_id = rec.get("record_id")
            prev_id = rec.get("previous_record_id")
            execution_id = rec.get("execution_id", "")

            if seq is not None and seq != i:
                errors.append(
                    f"sequence gap at record {i}: expected {i}, got {seq} (execution_id={execution_id})"
                )

            if prev_id != expected_prev_id:
                errors.append(
                    f"id pointer mismatch at sequence {i}: "
                    f"expected previous_record_id={expected_prev_id!r}, got {prev_id!r} (execution_id={execution_id})"
                )

            expected_prev_id = rec_id

        return {
            "valid": len(errors) == 0,
            "records_checked": len(records),
            "errors": errors,
            "session_id": session_id,
            "walacor_attestation": [
                {
                    "record_id": r.get("record_id"),
                    "walacor_block_id": r.get("walacor_block_id"),
                    "walacor_trans_id": r.get("walacor_trans_id"),
                    "walacor_dh": r.get("walacor_dh"),
                }
                for r in records
            ],
        }

    def get_ab_test_results(self, test_name: str) -> dict:
        """Aggregate execution stats by model variant for a given A/B test.

        Groups rows by model_id for all execution records that carry the
        matching ``ab_variant`` metadata field, and returns per-variant
        request counts, average latency, and total token usage so callers
        can compare quality/cost/latency across variants.
        """
        conn = self._ensure_conn()
        rows = conn.execute(
            """
            SELECT
                model_id                                                   AS model_id,
                json_extract(record_json, '$.metadata.ab_variant')         AS ab_variant,
                json_extract(record_json, '$.metadata.ab_original_model')  AS original_model,
                COUNT(*)                                                   AS request_count,
                AVG(latency_ms)                                            AS avg_latency_ms,
                SUM(total_tokens)                                          AS total_tokens,
                AVG(total_tokens)                                          AS avg_tokens
            FROM wal_records
            WHERE event_type = 'execution'
              AND json_extract(record_json, '$.metadata.ab_variant') = ?
            GROUP BY model_id
            ORDER BY request_count DESC
            """,
            (test_name,),
        ).fetchall()

        variants = []
        for row in rows:
            variants.append({
                "model_id": row[0],
                "ab_variant": row[1],
                "original_model": row[2],
                "request_count": row[3],
                "avg_latency_ms": round(row[4], 1) if row[4] is not None else None,
                "total_tokens": row[5],
                "avg_tokens": round(row[6], 1) if row[6] is not None else None,
            })

        return {"test_name": test_name, "variants": variants, "total_requests": sum(v["request_count"] for v in variants)}
