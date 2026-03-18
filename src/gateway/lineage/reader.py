"""Read-only SQLite reader for the WAL database. Separate connection from WALWriter."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from gateway.core import compute_sha3_512_string

from gateway.pipeline.session_chain import GENESIS_HASH

logger = logging.getLogger(__name__)


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

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List distinct sessions with record count and latest timestamp."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
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
                s.request_type,
                COALESCE(t.tool_names, '') AS tool_names,
                COALESCE(t.tool_details, '') AS tool_details
            FROM (
                SELECT
                    json_extract(record_json, '$.session_id') AS session_id,
                    COUNT(*) AS record_count,
                    SUM(CASE WHEN COALESCE(
                        json_extract(record_json, '$.metadata.request_type'), 'user_message'
                    ) = 'user_message' THEN 1 ELSE 0 END) AS user_message_count,
                    MAX(json_extract(record_json, '$.timestamp')) AS last_activity,
                    COALESCE(json_extract(record_json, '$.model_id'),
                             json_extract(record_json, '$.model_attestation_id')) AS model,
                    json_extract(record_json, '$.user') AS user,
                    json_extract(record_json, '$.metadata.walacor_audit.user_question') AS user_question,
                    json_extract(record_json, '$.metadata.walacor_audit.has_rag_context') AS has_rag_context,
                    json_extract(record_json, '$.metadata.walacor_audit.has_files') AS has_files,
                    json_extract(record_json, '$.metadata.request_type') AS request_type
                FROM wal_records
                WHERE json_extract(record_json, '$.session_id') IS NOT NULL
                  AND json_extract(record_json, '$.event_type') IS NULL
                GROUP BY session_id
            ) s
            LEFT JOIN (
                SELECT
                    json_extract(r.record_json, '$.session_id') AS session_id,
                    GROUP_CONCAT(DISTINCT json_extract(te.record_json, '$.tool_name')) AS tool_names,
                    GROUP_CONCAT(DISTINCT
                        json_extract(te.record_json, '$.tool_name') || ':' ||
                        COALESCE(json_extract(te.record_json, '$.source'), 'unknown')
                    ) AS tool_details
                FROM wal_records r
                JOIN wal_records te
                  ON json_extract(te.record_json, '$.execution_id') = r.execution_id
                 AND json_extract(te.record_json, '$.event_type') = 'tool_call'
                WHERE json_extract(r.record_json, '$.session_id') IS NOT NULL
                  AND json_extract(r.record_json, '$.event_type') IS NULL
                GROUP BY json_extract(r.record_json, '$.session_id')
            ) t ON t.session_id = s.session_id
            ORDER BY s.last_activity DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(row) for row in cur.fetchall()]

    def count_sessions(self) -> int:
        """Count total distinct sessions in WAL."""
        conn = self._ensure_conn()
        cur = conn.execute(
            "SELECT COUNT(DISTINCT json_extract(record_json, '$.session_id')) FROM wal_records "
            "WHERE json_extract(record_json, '$.session_id') IS NOT NULL "
            "AND json_extract(record_json, '$.event_type') IS NULL"
        )
        return cur.fetchone()[0] or 0

    def get_session_timeline(self, session_id: str) -> list[dict]:
        """Return all execution records for a session, ordered by sequence_number."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            SELECT execution_id, record_json, created_at
            FROM wal_records
            WHERE json_extract(record_json, '$.session_id') = ?
              AND json_extract(record_json, '$.event_type') IS NULL
            ORDER BY json_extract(record_json, '$.sequence_number') ASC,
                     created_at ASC
            """,
            (session_id,),
        )
        results = []
        for row in cur.fetchall():
            record = json.loads(row["record_json"])
            record["_wal_created_at"] = row["created_at"]
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
        return json.loads(row["record_json"])

    def get_tool_events(self, execution_id: str) -> list[dict]:
        """Return tool event records linked to an execution."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            SELECT record_json
            FROM wal_records
            WHERE json_extract(record_json, '$.execution_id') = ?
              AND json_extract(record_json, '$.event_type') = 'tool_call'
            ORDER BY json_extract(record_json, '$.timestamp') ASC
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

    def get_attempts(self, limit: int = 100, offset: int = 0) -> dict:
        """Return recent attempt records and disposition stats."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            SELECT request_id, timestamp, tenant_id, provider, model_id,
                   path, disposition, execution_id, status_code, user
            FROM gateway_attempts
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        items = [dict(row) for row in cur.fetchall()]

        cur2 = conn.execute(
            "SELECT disposition, COUNT(*) AS count FROM gateway_attempts GROUP BY disposition"
        )
        stats = {row["disposition"]: row["count"] for row in cur2.fetchall()}

        total_cur = conn.execute("SELECT COUNT(*) AS total FROM gateway_attempts")
        total = total_cur.fetchone()["total"]

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
        """
        if range_key not in self._RANGE_CONFIG:
            range_key = "1h"
        lookback, fmt = self._RANGE_CONFIG[range_key]
        conn = self._ensure_conn()
        cur = conn.execute(
            f"""
            SELECT
                strftime('{fmt}', timestamp) AS t,
                COUNT(*) AS total,
                SUM(CASE WHEN disposition IN ('forwarded', 'allowed') THEN 1 ELSE 0 END) AS allowed,
                SUM(CASE WHEN disposition NOT IN ('forwarded', 'allowed') THEN 1 ELSE 0 END) AS blocked
            FROM gateway_attempts
            WHERE timestamp >= datetime('now', '{lookback}')
            GROUP BY t
            ORDER BY t ASC
            """,
        )
        buckets = [{"t": row["t"], "total": row["total"], "allowed": row["allowed"], "blocked": row["blocked"]} for row in cur.fetchall()]
        return {"buckets": buckets, "range": range_key}

    def get_token_latency_history(self, range_key: str) -> dict:
        """Return time-bucketed token usage and latency aggregates for charting.

        Returns: {buckets: [{t, prompt_tokens, completion_tokens, total_tokens,
                             avg_latency_ms, max_latency_ms, request_count}], range: str}
        """
        if range_key not in self._RANGE_CONFIG:
            range_key = "1h"
        lookback, fmt = self._RANGE_CONFIG[range_key]
        conn = self._ensure_conn()
        cur = conn.execute(
            f"""
            SELECT
                strftime('{fmt}', json_extract(record_json, '$.timestamp')) AS t,
                COALESCE(SUM(json_extract(record_json, '$.prompt_tokens')), 0) AS prompt_tokens,
                COALESCE(SUM(json_extract(record_json, '$.completion_tokens')), 0) AS completion_tokens,
                COALESCE(SUM(json_extract(record_json, '$.total_tokens')), 0) AS total_tokens,
                COALESCE(AVG(json_extract(record_json, '$.latency_ms')), 0) AS avg_latency_ms,
                COALESCE(MAX(json_extract(record_json, '$.latency_ms')), 0) AS max_latency_ms,
                COUNT(*) AS request_count
            FROM wal_records
            WHERE json_extract(record_json, '$.timestamp') >= datetime('now', '{lookback}')
              AND (json_extract(record_json, '$.event_type') IS NULL
                   OR json_extract(record_json, '$.event_type') = '')
            GROUP BY t
            ORDER BY t ASC
            """,
        )
        buckets = []
        for row in cur.fetchall():
            buckets.append({
                "t": row["t"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "total_tokens": row["total_tokens"],
                "avg_latency_ms": round(row["avg_latency_ms"], 1),
                "max_latency_ms": round(row["max_latency_ms"], 1),
                "request_count": row["request_count"],
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
            SELECT DISTINCT json_extract(record_json, '$.model_id') AS model_id
            FROM wal_records
            WHERE json_extract(record_json, '$.timestamp') >= ?
              AND json_extract(record_json, '$.timestamp') < ?
              AND json_extract(record_json, '$.event_type') IS NULL
              AND json_extract(record_json, '$.model_id') IS NOT NULL
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
            WHERE json_extract(record_json, '$.timestamp') >= ?
              AND json_extract(record_json, '$.timestamp') < ?
              AND json_extract(record_json, '$.event_type') IS NULL
            ORDER BY json_extract(record_json, '$.timestamp') ASC
            LIMIT ?
            """,
            (start, end, limit),
        )
        return [json.loads(row["record_json"]) for row in cur.fetchall()]

    def get_attestation_summary(self, start: str, end: str) -> list[dict]:
        """Model attestation inventory with usage counts in period."""
        conn = self._ensure_conn()
        cur = conn.execute(
            """
            SELECT
                json_extract(record_json, '$.model_id') AS model_id,
                json_extract(record_json, '$.provider') AS provider,
                json_extract(record_json, '$.model_attestation_id') AS attestation_id,
                COUNT(*) AS request_count,
                SUM(COALESCE(json_extract(record_json, '$.total_tokens'), 0)) AS total_tokens
            FROM wal_records
            WHERE json_extract(record_json, '$.timestamp') >= ?
              AND json_extract(record_json, '$.timestamp') < ?
              AND json_extract(record_json, '$.event_type') IS NULL
              AND json_extract(record_json, '$.model_id') IS NOT NULL
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
            SELECT DISTINCT json_extract(record_json, '$.session_id') AS session_id
            FROM wal_records
            WHERE json_extract(record_json, '$.timestamp') >= ?
              AND json_extract(record_json, '$.timestamp') < ?
              AND json_extract(record_json, '$.session_id') IS NOT NULL
              AND json_extract(record_json, '$.event_type') IS NULL
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
            group_col = "json_extract(record_json, '$.user')"
            group_alias = "user"
        else:
            group_col = (
                "COALESCE(json_extract(record_json, '$.model_id'), "
                "json_extract(record_json, '$.model_attestation_id'))"
            )
            group_alias = "model"

        sql = f"""
            SELECT
                {group_col} AS group_key,
                COUNT(*) AS request_count,
                SUM(COALESCE(json_extract(record_json, '$.prompt_tokens'), 0)) AS total_prompt_tokens,
                SUM(COALESCE(json_extract(record_json, '$.completion_tokens'), 0)) AS total_completion_tokens,
                SUM(COALESCE(json_extract(record_json, '$.total_tokens'), 0)) AS total_tokens,
                SUM(COALESCE(json_extract(record_json, '$.estimated_cost_usd'), 0.0)) AS total_cost_usd
            FROM wal_records
            WHERE json_extract(record_json, '$.timestamp') > datetime('now', ?)
              AND (json_extract(record_json, '$.event_type') IS NULL
                   OR json_extract(record_json, '$.event_type') = '')
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
            "SELECT record_json FROM wal_records WHERE json_extract(record_json, '$.session_id') = ?",
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
        """Verify Merkle chain integrity for a session.

        Recomputes record_hash for each record and checks previous_record_hash linkage.
        Returns {valid: bool, record_count: int, errors: list[str]}.
        """
        records = self.get_session_timeline(session_id)
        if not records:
            return {"valid": True, "record_count": 0, "errors": [], "session_id": session_id}

        errors: list[str] = []
        prev_hash = GENESIS_HASH

        for i, rec in enumerate(records):
            seq = rec.get("sequence_number")
            rec_hash = rec.get("record_hash")
            rec_prev = rec.get("previous_record_hash")
            execution_id = rec.get("execution_id", "")

            # Check sequence_number ordering
            if seq is not None and seq != i:
                errors.append(
                    f"Record {i}: expected sequence_number={i}, got {seq} (execution_id={execution_id})"
                )

            # Check previous_record_hash linkage
            if rec_prev is not None and rec_prev != prev_hash:
                errors.append(
                    f"Record {i}: previous_record_hash mismatch "
                    f"(expected={prev_hash[:16]}..., got={rec_prev[:16]}..., execution_id={execution_id})"
                )

            # Recompute record_hash
            if rec_hash is not None:
                computed = compute_sha3_512_string("|".join([
                    execution_id,
                    str(rec.get("policy_version", "")),
                    str(rec.get("policy_result", "")),
                    str(rec.get("previous_record_hash", "")),
                    str(seq if seq is not None else ""),
                    str(rec.get("timestamp", "")),
                ]))
                if computed != rec_hash:
                    errors.append(
                        f"Record {i}: record_hash mismatch "
                        f"(computed={computed[:16]}..., stored={rec_hash[:16]}..., execution_id={execution_id})"
                    )
                prev_hash = rec_hash
            else:
                # Record has no chain fields (e.g. unchained legacy record)
                errors.append(
                    f"Record {i}: missing record_hash (execution_id={execution_id})"
                )

        return {
            "valid": len(errors) == 0,
            "record_count": len(records),
            "errors": errors,
            "session_id": session_id,
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
                json_extract(record, '$.model_id')                   AS model_id,
                json_extract(record, '$.metadata.ab_variant')        AS ab_variant,
                json_extract(record, '$.metadata.ab_original_model') AS original_model,
                COUNT(*)                                             AS request_count,
                AVG(json_extract(record, '$.latency_ms'))            AS avg_latency_ms,
                SUM(json_extract(record, '$.total_tokens'))          AS total_tokens,
                AVG(json_extract(record, '$.total_tokens'))          AS avg_tokens
            FROM gateway_executions
            WHERE json_extract(record, '$.metadata.ab_variant') = ?
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
