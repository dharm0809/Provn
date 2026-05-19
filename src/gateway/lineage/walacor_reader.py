"""Walacor-backed lineage reader — replaces SQLite LineageReader.

Queries execution records, attempts, and tool events via Walacor's
/api/query/getcomplex endpoint using MongoDB-style aggregation pipelines.
All methods are async (Walacor API is HTTP-based).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from gateway.lineage._normalize import normalize_record as _normalize_record

if TYPE_CHECKING:
    from gateway.walacor.client import WalacorClient

logger = logging.getLogger(__name__)


def _has_content_analysis(raw: dict) -> bool:
    """True when a raw execution record carries content-analysis output.

    Analyzer verdicts may live at the top level (``content_analysis``) or
    inside ``metadata_json`` (``analyzer_decisions``) depending on write
    path. Both count as "the analyzer actually ran".
    """
    if raw.get("content_analysis"):
        return True
    meta = raw.get("metadata_json")
    if not meta:
        return False
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, ValueError):
            return False
    if isinstance(meta, dict):
        return bool(meta.get("analyzer_decisions") or meta.get("content_analysis"))
    return False


def _deserialize_record(r: dict) -> dict:
    """Convert Walacor storage format back to gateway record format.

    - metadata_json (string) → metadata (dict)
    - Strips Walacor envelope-internal fields that leak into operator views.

    C9: the strip-list now includes ``CreatedAt``, ``UpdatedAt``, ``EId``. These
    are Walacor bookkeeping fields that no dashboard JSX reads; they only
    clutter the JSON returned to the audit dashboard and made record diffs
    noisy. ``EId`` is intentionally re-attached as the leading-underscore
    ``_walacor_eid`` by the caller for the envelope drawer; we strip it here
    so it doesn't appear at top level.

    C10: ``metadata._internal`` is PRESERVED, not promoted. Internal classifier
    flags (``_intent``, ``_translated_from_openai``, ``schema_mapper_*``) live
    inside that nested key. Operators with ``?debug=true`` can still see it;
    the simple session view ignores it. The alternative (strip outright) was
    rejected because auditors should be able to inspect classifier reasoning
    if needed — just not by default.
    """
    # Parse metadata_json back to metadata dict
    mj = r.pop("metadata_json", None)
    if mj and isinstance(mj, str):
        try:
            r["metadata"] = json.loads(mj)
        except (json.JSONDecodeError, ValueError):
            r["metadata"] = {}
    elif mj and isinstance(mj, dict):
        r["metadata"] = mj
    # Promote file_metadata from metadata to top level for dashboard
    meta = r.get("metadata")
    if isinstance(meta, dict):
        fm = meta.pop("file_metadata", None)
        if fm:
            r["file_metadata"] = fm
        # Promote chain fields if stored in metadata (older records)
        for chain_key in ("sequence_number", "record_hash", "previous_record_hash"):
            if r.get(chain_key) is None and meta.get(chain_key) is not None:
                r[chain_key] = meta[chain_key]
        # NB: deliberately do NOT pop `_internal` — keeping classifier
        # reasoning available behind the operator's debug filter.
    # Strip Walacor internal fields that leak into query results
    for k in (
        "_id", "ORGId", "UID", "IsDeleted", "SV", "LastModifiedBy",
        "CreatedAt", "UpdatedAt", "EId",
    ):
        r.pop(k, None)
    return r


class WalacorLineageReader:
    """Async read interface for lineage data stored in Walacor."""

    def __init__(
        self,
        client: WalacorClient,
        executions_etid: int = 9000011,
        attempts_etid: int = 9000012,
        tool_events_etid: int = 9000013,
    ) -> None:
        self._client = client
        self._exec_etid = executions_etid
        self._att_etid = attempts_etid
        self._tool_etid = tool_events_etid

    # ── Sessions ──────────────────────────────────────────────────────────

    async def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        sort: str = "last_activity",
        order: str = "desc",
    ) -> list[dict]:
        sort_dir = 1 if str(order).lower() == "asc" else -1
        sort_field = {
            "last_activity": "last_activity",
            "record_count": "record_count",
            "model": "model",
        }.get(sort, "last_activity")

        pipeline: list[dict[str, Any]] = [
            {"$match": {"session_id": {"$ne": None}}},
            {"$group": {
                "_id": "$session_id",
                "record_count": {"$sum": 1},
                "last_activity": {"$max": "$timestamp"},
                "model": {"$last": "$model_id"},
                "user": {"$last": "$user"},
                "metadata_json": {"$last": "$metadata_json"},
            }},
            {"$sort": {sort_field: sort_dir}},
        ]

        if search and search.strip():
            safe_search = re.escape(search.strip())  # Prevent regex injection
            pipeline.insert(1, {"$match": {
                "$or": [
                    {"session_id": {"$regex": safe_search, "$options": "i"}},
                    {"model_id": {"$regex": safe_search, "$options": "i"}},
                    {"user": {"$regex": safe_search, "$options": "i"}},
                ]
            }})

        pipeline.extend([{"$skip": offset}, {"$limit": limit}])
        rows = await self._client.query_complex(self._exec_etid, pipeline)

        # Extract session IDs for tool event + user-record metadata lookup
        session_ids = [r.get("_id") or r.get("session_id") for r in rows if r.get("_id") or r.get("session_id")]
        tool_map = await self._get_session_tool_indicators(session_ids) if session_ids else {}
        # For sessions where $last metadata is a system task, fetch user-record metadata
        user_meta_map = await self._get_user_record_metadata(session_ids) if session_ids else {}
        # C6: compute per-session chain_status by walking previous_record_id
        # linkage. The dashboard reads this field on every session row
        # (`Sessions.jsx:58, 124, 205`); pre-fix code never populated it so
        # all sessions defaulted to "verified", which made the integrity
        # badge decorative.
        chain_status_map = await self._compute_chain_status_map(session_ids) if session_ids else {}

        results = []
        for r in rows:
            sid = r.get("_id") or r.get("session_id")
            meta = self._parse_session_metadata(r.get("metadata_json"))
            # If last record was system task, use user-record metadata instead
            if not meta.get("user_question") and sid in user_meta_map:
                meta.update(user_meta_map[sid])
            tools = tool_map.get(sid, {})
            # Fallback: extract tool info from execution metadata when tool events query returned empty
            if not tools.get("tool_names") and meta.get("tool_names"):
                tools = {"tool_names": meta["tool_names"], "tool_details": meta.get("tool_details", "")}
            results.append({
                "session_id": sid,
                "record_count": r.get("record_count", 0),
                "user_message_count": meta.get("user_message_count", r.get("record_count", 0)),
                "last_activity": r.get("last_activity"),
                "model": r.get("model"),
                "user": r.get("user"),
                "user_question": meta.get("user_question"),
                "has_rag_context": meta.get("has_rag_context"),
                "has_files": meta.get("has_files"),
                "has_images": meta.get("has_images"),
                "request_type": meta.get("request_type"),
                "tool_names": tools.get("tool_names", ""),
                "tool_details": tools.get("tool_details", ""),
                # "verified" | "warn" — derived from `previous_record_id`
                # walk in `_compute_chain_status_map`.
                "chain_status": chain_status_map.get(sid, "verified"),
            })
        return results

    async def _compute_chain_status_map(self, session_ids: list[str]) -> dict[str, str]:
        """Walk each session's records and decide ``chain_status``.

        Returns ``{session_id: "verified" | "warn"}``. A session is "verified"
        when every record's ``previous_record_id`` equals the immediately
        preceding record's ``record_id`` (and the first record's
        ``previous_record_id`` is None). Otherwise "warn".

        Implementation: one batched query for all records in the supplied
        sessions, then group-and-walk in Python. Avoids running N verify_chain
        calls against Walacor when N can easily reach 50 sessions per page.

        Fail-open: a query error returns an empty map so the caller falls back
        to "verified" — keeps the dashboard usable on Walacor flakiness.
        """
        if not session_ids:
            return {}
        try:
            rows = await self._client.query_complex(
                self._exec_etid,
                [
                    {"$match": {"session_id": {"$in": session_ids}}},
                    {"$project": {
                        "session_id": 1,
                        "sequence_number": 1,
                        "record_id": 1,
                        "previous_record_id": 1,
                    }},
                ],
            )
        except Exception:
            logger.warning("chain_status query failed (defaulting to verified)", exc_info=True)
            return {}

        # Group by session_id and sort within session by sequence_number then
        # record_id (UUIDv7 is time-ordered). Matches `get_session_timeline`'s
        # ordering so the walk reflects what the dashboard will render.
        from collections import defaultdict
        by_session: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            sid = r.get("session_id")
            if sid:
                by_session[sid].append(r)

        status: dict[str, str] = {}
        for sid, recs in by_session.items():
            recs.sort(key=lambda r: (
                r.get("sequence_number") if r.get("sequence_number") is not None else 1 << 31,
                r.get("record_id") or "",
            ))
            expected_prev: str | None = None
            verified = True
            for r in recs:
                prev = r.get("previous_record_id")
                if prev != expected_prev:
                    verified = False
                    break
                expected_prev = r.get("record_id")
            status[sid] = "verified" if verified else "warn"
        return status

    @staticmethod
    def _parse_session_metadata(metadata_json: str | dict | None) -> dict:
        """Extract indicator fields from the last record's metadata_json."""
        if not metadata_json:
            return {}
        meta = metadata_json
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, ValueError):
                return {}
        if not isinstance(meta, dict):
            return {}

        audit = meta.get("walacor_audit", {})
        request_type = meta.get("request_type") or ""
        # If the last record is a system task, don't use its question/flags
        # for the session display (the real user question is in an earlier record).
        # But ALWAYS preserve the actual request_type so system tasks are identifiable.
        is_system = request_type.startswith("system_task")

        # Extract tool info from metadata.tool_interactions (always present in execution records)
        tool_names = ""
        tool_details = ""
        tool_interactions = meta.get("tool_interactions", [])
        if tool_interactions and isinstance(tool_interactions, list):
            names = set()
            details = set()
            for ti in tool_interactions:
                if isinstance(ti, dict):
                    name = ti.get("tool_name") or ti.get("name") or ""
                    source = ti.get("source") or ti.get("tool_type") or "unknown"
                    if name:
                        names.add(name)
                        details.add(f"{name}:{source}")
            tool_names = ",".join(sorted(names))
            tool_details = ",".join(sorted(details))

        return {
            "user_question": None if is_system else (audit.get("user_question") or None),
            "has_rag_context": False if is_system else audit.get("has_rag_context", False),
            "has_files": False if is_system else (audit.get("has_files", False) or audit.get("file_count", 0) > 0),
            "has_images": False if is_system else audit.get("has_images", False),
            "request_type": request_type,  # Always preserve — don't mask system_task
            "user_message_count": audit.get("conversation_turns", 0) or 0,
            "tool_names": tool_names,
            "tool_details": tool_details,
        }

    async def _get_user_record_metadata(self, session_ids: list[str]) -> dict[str, dict]:
        """For sessions where $last record is a system task, fetch the last user record's metadata."""
        if not session_ids:
            return {}
        pipeline: list[dict[str, Any]] = [
            {"$match": {"session_id": {"$in": session_ids}}},
            {"$project": {"session_id": 1, "metadata_json": 1}},
            {"$sort": {"timestamp": -1}},
        ]
        try:
            rows = await self._client.query_complex(self._exec_etid, pipeline)
        except Exception:
            logger.warning("User-record metadata query failed (non-fatal)", exc_info=True)
            return {}

        # Group by session: use last user question, but OR-merge boolean flags
        result: dict[str, dict] = {}
        for r in rows:
            sid = r.get("session_id")
            if not sid:
                continue
            meta = self._parse_session_metadata(r.get("metadata_json"))
            if not meta.get("user_question"):
                continue
            if sid not in result:
                result[sid] = meta
            else:
                # Merge: keep latest question, OR boolean flags
                for flag in ("has_rag_context", "has_files", "has_images"):
                    if meta.get(flag):
                        result[sid][flag] = True
        return result

    async def _get_session_tool_indicators(self, session_ids: list[str]) -> dict[str, dict]:
        """Query tool events for a batch of sessions. Returns {session_id: {tool_names, tool_details}}.

        Uses a simple $match + $project to fetch raw tool events, then aggregates
        in Python. This avoids relying on advanced MongoDB operators ($addToSet,
        $concat, $ifNull) that Walacor's getcomplex may not fully support.
        """
        if not session_ids:
            return {}
        pipeline: list[dict[str, Any]] = [
            {"$match": {"session_id": {"$in": session_ids}}},
            {"$project": {
                "session_id": 1,
                "tool_name": 1,
                "tool_source": 1,
                "tool_type": 1,
            }},
        ]
        try:
            rows = await self._client.query_complex(self._tool_etid, pipeline)
        except Exception:
            logger.warning("Tool event indicator query failed (falling back to metadata)", exc_info=True)
            return {}

        # Aggregate in Python: collect unique tool names and sources per session
        from collections import defaultdict
        session_tools: dict[str, dict[str, set]] = defaultdict(lambda: {"names": set(), "details": set()})
        for r in rows:
            sid = r.get("session_id")
            name = r.get("tool_name")
            if not sid or not name:
                continue
            source = r.get("tool_source") or r.get("tool_type") or "unknown"
            session_tools[sid]["names"].add(name)
            session_tools[sid]["details"].add(f"{name}:{source}")

        return {
            sid: {
                "tool_names": ",".join(sorted(data["names"])),
                "tool_details": ",".join(sorted(data["details"])),
            }
            for sid, data in session_tools.items()
        }

    async def count_sessions(self, search: str | None = None) -> int:
        pipeline: list[dict[str, Any]] = [
            {"$match": {"session_id": {"$ne": None}}},
            {"$group": {"_id": "$session_id"}},
            {"$count": "total"},
        ]
        if search and search.strip():
            safe_search = re.escape(search.strip())
            pipeline.insert(1, {"$match": {
                "$or": [
                    {"session_id": {"$regex": safe_search, "$options": "i"}},
                    {"model_id": {"$regex": safe_search, "$options": "i"}},
                    {"user": {"$regex": safe_search, "$options": "i"}},
                ]
            }})
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        return rows[0]["total"] if rows else 0

    # ── Session timeline ──────────────────────────────────────────────────

    async def get_session_timeline(self, session_id: str, limit: int = 500) -> list[dict]:
        # C11: sort by ``(sequence_number, record_id)`` — record_id is UUIDv7
        # so its lexicographic order matches creation time, giving a stable
        # tiebreaker without depending on the envelope ``$lookup`` having
        # already attached ``CreatedAt``. The previous sort included
        # ``CreatedAt`` here but the field isn't on the execution record before
        # the lookup runs, so records that share a sequence_number could come
        # back in undefined order.
        pipeline: list[dict] = [
            {"$match": {"session_id": session_id}},
            {"$sort": {"sequence_number": 1, "record_id": 1}},
        ]
        try:
            capped = max(1, int(limit))
        except (TypeError, ValueError):
            capped = 500
        pipeline.append({"$limit": capped})
        pipeline.append({"$lookup": {
            "from": "envelopes",
            "localField": "EId",
            "foreignField": "EId",
            "as": "env",
        }})
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        results = []
        for r in rows:
            # Capture EId BEFORE deserialization — _deserialize_record now
            # strips it from the top-level record (C9) so the dashboard sees
            # a clean view. We still want it available as `_walacor_eid` for
            # the envelope drawer and the chain anchor round-trip.
            eid = r.get("EId")
            _deserialize_record(r)
            r["_walacor_eid"] = eid
            _normalize_record(r)
            results.append(r)
        return results

    # ── Execution detail ──────────────────────────────────────────────────

    async def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        pipeline = [
            {"$match": {"execution_id": execution_id}},
            {"$limit": 1},
            {"$lookup": {
                "from": "envelopes",
                "localField": "EId",
                "foreignField": "EId",
                "as": "env",
            }},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        if not rows:
            return None
        r = rows[0]
        eid = r.get("EId")  # capture before _deserialize_record strips it (C9)
        _deserialize_record(r)
        r["_walacor_eid"] = eid
        _normalize_record(r)
        return r

    async def get_tool_events(self, execution_id: str) -> list[dict]:
        pipeline = [
            {"$match": {"execution_id": execution_id}},
            {"$sort": {"timestamp": 1}},
        ]
        rows = await self._client.query_complex(self._tool_etid, pipeline)
        for r in rows:
            # Reverse the field mapping from write time (tool_source → source)
            if "tool_source" in r and "source" not in r:
                r["source"] = r.pop("tool_source")
            # Deserialise JSON string fields
            for key in ("input_data", "sources", "content_analysis"):
                val = r.get(key)
                if isinstance(val, str):
                    try:
                        r[key] = json.loads(val)
                    except (json.JSONDecodeError, ValueError):
                        pass
        return rows

    async def get_execution_trace(self, execution_id: str) -> dict[str, Any] | None:
        execution = await self.get_execution(execution_id)
        if not execution:
            return None
        tool_events = await self.get_tool_events(execution_id)
        timings = execution.get("timings") or {}
        if isinstance(timings, str):
            try:
                timings = json.loads(timings)
            except (json.JSONDecodeError, ValueError):
                timings = {}
        # Timings are not a top-level Walacor schema field — they're stored in
        # metadata_json. Extract them from there when the top-level field is absent.
        if not timings:
            raw_meta = execution.get("metadata_json")
            if raw_meta:
                try:
                    meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                    timings = meta.get("timings") or {}
                except (json.JSONDecodeError, ValueError, AttributeError):
                    pass
        return {
            "execution": execution,
            "tool_events": tool_events,
            "timings": timings,
        }

    # ── Attempts ──────────────────────────────────────────────────────────

    async def get_attempts(
        self,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        sort: str = "timestamp",
        order: str = "desc",
        disposition: str | None = None,
    ) -> dict:
        sort_dir = 1 if str(order).lower() == "asc" else -1
        sort_field = sort if sort in (
            "timestamp", "disposition", "request_id", "user",
            "model_id", "path", "status_code",
        ) else "timestamp"

        match_stage: dict[str, Any] = {}
        if search and search.strip():
            safe_search = re.escape(search.strip())
            match_stage = {"$or": [
                {"request_id": {"$regex": safe_search, "$options": "i"}},
                {"tenant_id": {"$regex": safe_search, "$options": "i"}},
                {"provider": {"$regex": safe_search, "$options": "i"}},
                {"model_id": {"$regex": safe_search, "$options": "i"}},
                {"disposition": {"$regex": safe_search, "$options": "i"}},
                {"user": {"$regex": safe_search, "$options": "i"}},
                {"reason": {"$regex": safe_search, "$options": "i"}},
            ]}
        if disposition is not None:
            # Additive exact-match filter; wrap with $and so it composes with the $or search.
            if match_stage:
                match_stage = {"$and": [match_stage, {"disposition": disposition}]}
            else:
                match_stage = {"disposition": disposition}

        # Items query
        items_pipeline: list[dict[str, Any]] = []
        if match_stage:
            items_pipeline.append({"$match": match_stage})
        items_pipeline.extend([
            {"$sort": {sort_field: sort_dir}},
            {"$skip": offset},
            {"$limit": limit},
            {"$project": {
                "request_id": 1, "timestamp": 1, "tenant_id": 1,
                "provider": 1, "model_id": 1, "path": 1,
                "disposition": 1, "execution_id": 1, "status_code": 1, "user": 1, "reason": 1,
            }},
        ])
        items = await self._client.query_complex(self._att_etid, items_pipeline)

        # Stats query
        stats_pipeline: list[dict[str, Any]] = []
        if match_stage:
            stats_pipeline.append({"$match": match_stage})
        stats_pipeline.append({"$group": {"_id": "$disposition", "count": {"$sum": 1}}})
        stats_rows = await self._client.query_complex(self._att_etid, stats_pipeline)
        stats = {r["_id"]: r["count"] for r in stats_rows if r.get("_id")}

        # Total count
        count_pipeline: list[dict[str, Any]] = []
        if match_stage:
            count_pipeline.append({"$match": match_stage})
        count_pipeline.append({"$count": "total"})
        count_rows = await self._client.query_complex(self._att_etid, count_pipeline)
        total = count_rows[0]["total"] if count_rows else 0

        return {"items": items, "stats": stats, "total": total}

    # ── Metrics history ───────────────────────────────────────────────────

    async def get_metrics_history(self, range_key: str) -> dict:
        """Time-bucketed attempt counts for throughput chart."""
        cfg = {"1h": (1, 60, "%Y-%m-%dT%H:%M:00"), "24h": (24, 24, "%Y-%m-%dT%H:00:00"),
               "7d": (168, 168, "%Y-%m-%dT%H:00:00"), "30d": (720, 720, "%Y-%m-%dT%H:00:00")}
        hours, num_buckets, fmt = cfg.get(range_key, cfg["1h"])
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=hours)).isoformat()

        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$project": {"timestamp": 1, "disposition": 1}},
        ]
        rows = await self._client.query_complex(self._att_etid, pipeline)

        # Build time buckets in Python
        step = timedelta(hours=hours) / num_buckets
        start = now - timedelta(hours=hours)
        labels = [(start + step * i).strftime(fmt) for i in range(num_buckets)]
        by_t: dict[str, dict] = {t: {"t": t, "total": 0, "allowed": 0, "blocked": 0} for t in labels}

        for r in rows:
            ts = r.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                key = dt.strftime(fmt)
            except (ValueError, TypeError):
                continue
            bucket = by_t.get(key)
            if bucket:
                bucket["total"] += 1
                if r.get("disposition") in ("allowed", "forwarded"):
                    bucket["allowed"] += 1
                else:
                    bucket["blocked"] += 1

        return {"buckets": [by_t[t] for t in labels], "range": range_key}

    # ── Token / latency history ───────────────────────────────────────────

    async def get_token_latency_history(self, range_key: str) -> dict:
        """Time-bucketed token usage and latency for charts."""
        cfg = {"1h": (1, 60, "%Y-%m-%dT%H:%M:00"), "24h": (24, 24, "%Y-%m-%dT%H:00:00"),
               "7d": (168, 168, "%Y-%m-%dT%H:00:00"), "30d": (720, 720, "%Y-%m-%dT%H:00:00")}
        hours, num_buckets, fmt = cfg.get(range_key, cfg["1h"])
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=hours)).isoformat()

        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$project": {"timestamp": 1, "prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 1, "latency_ms": 1}},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)

        step = timedelta(hours=hours) / num_buckets
        start = now - timedelta(hours=hours)
        labels = [(start + step * i).strftime(fmt) for i in range(num_buckets)]
        by_t: dict[str, dict] = {}
        for t in labels:
            by_t[t] = {"t": t, "prompt_tokens": 0, "completion_tokens": 0,
                       "total_tokens": 0, "latencies": [], "request_count": 0}

        for r in rows:
            ts = r.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                key = dt.strftime(fmt)
            except (ValueError, TypeError):
                continue
            bucket = by_t.get(key)
            if bucket:
                bucket["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
                bucket["completion_tokens"] += r.get("completion_tokens", 0) or 0
                bucket["total_tokens"] += r.get("total_tokens", 0) or 0
                lat = r.get("latency_ms")
                if lat:
                    bucket["latencies"].append(lat)
                bucket["request_count"] += 1

        buckets = []
        for t in labels:
            b = by_t[t]
            lats = b.pop("latencies")
            b["avg_latency_ms"] = round(sum(lats) / len(lats), 1) if lats else 0
            b["max_latency_ms"] = round(max(lats), 1) if lats else 0
            buckets.append(b)

        return {"buckets": buckets, "range": range_key}

    # ── Chain verification ────────────────────────────────────────────────

    async def verify_chain(self, session_id: str) -> dict:
        """Verify chain integrity and authenticity for a session.

        Runs four independent checks per record:
          1. Structural linkage (sequence + previous_record_id pointer).
          2. Ed25519 signature verification over the canonical ID string.
          3. Anchor presence (walacor_block_id/trans_id/dh all non-null).
          4. **Independent Walacor round-trip** — re-query the envelope by
             EId on the envelope collection and confirm BlockId/TransId/DH
             match what the record fetch returned. Defeats any in-query
             tampering at the initial ``$lookup`` stage.

        C2: ``verification_level`` distinguishes three outcomes:

            * ``"verified"`` — structural integrity holds AND every anchor
              completed an independent Walacor round-trip with matching
              BlockId/TransId/DH. Strongest guarantee.
            * ``"structural"`` — structural integrity holds, anchor fields are
              present on every record, but at least one round-trip didn't
              actually compare against the envelope collection (e.g. no
              ``_walacor_eid`` for that record). Useful for offline / partial
              audits. Does NOT prove the seal is still intact at Walacor.
            * ``"unverifiable"`` — at least one anchor round-trip raised a
              transport error, OR records have anchor fields missing, OR a
              signature check failed, OR structural errors exist.

        Per-record ``valid`` (C6 read-side): each record carries a boolean so
        the dashboard's per-row badge reflects that single record's status,
        not the aggregate session verdict.

        The session-level ``valid`` field is True only when
        ``verification_level == "verified"``.
        """
        from gateway.crypto.signing import verify_record_signature, signing_key_available
        from gateway.lineage.reader import _empty_verify_result

        records = await self.get_session_timeline(session_id)
        if not records:
            return _empty_verify_result(session_id)

        errors: list[str] = []
        expected_prev_id: str | None = records[0].get("previous_record_id")
        per_record: list[dict] = []
        sig_valid = sig_invalid = sig_absent = sig_unverifiable = 0
        anchor_verified = anchor_present_only = anchor_missing = anchor_mismatched = anchor_unverifiable = 0
        roundtrips_attempted = False

        for i, r in enumerate(records):
            seq = r.get("sequence_number")
            if seq is None:
                seq = i  # Walacor records written pre-chain may lack sequence_number
            rec_id = r.get("record_id")
            prev_id = r.get("previous_record_id")
            execution_id = r.get("execution_id", "")
            structural_ok = True

            if seq != i:
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

            sig_status = verify_record_signature(r)
            if sig_status == "valid":
                sig_valid += 1
            elif sig_status == "invalid":
                sig_invalid += 1
                errors.append(f"signature invalid at sequence {i} (execution_id={execution_id})")
            elif sig_status == "unverifiable":
                sig_unverifiable += 1
            else:
                sig_absent += 1

            block_id = r.get("walacor_block_id")
            trans_id = r.get("walacor_trans_id")
            dh = r.get("walacor_dh")
            eid = r.get("_walacor_eid") or r.get("EId")
            has_anchor = bool(block_id and trans_id and dh)

            anchor_status = "absent"
            if has_anchor and eid:
                roundtrips_attempted = True
                try:
                    fresh = await self._client.query_complex(
                        self._exec_etid,
                        [
                            {"$match": {"EId": eid}},
                            {"$limit": 1},
                            {"$lookup": {
                                "from": "envelopes",
                                "localField": "EId",
                                "foreignField": "EId",
                                "as": "env",
                            }},
                            {"$project": {"_id": 0, "env": 1, "EId": 1}},
                        ],
                    )
                    env = (fresh[0].get("env") or [{}])[0] if fresh else {}
                    fresh_block = env.get("BlockId")
                    fresh_trans = env.get("TransId")
                    fresh_dh = env.get("DH")
                    if (fresh_block, fresh_trans, fresh_dh) == (block_id, trans_id, dh):
                        anchor_verified += 1
                        anchor_status = "verified"
                    else:
                        anchor_mismatched += 1
                        anchor_status = "mismatched"
                        errors.append(
                            f"walacor anchor mismatch at sequence {i}: "
                            f"initial=(block={block_id}, trans={trans_id}, dh={dh}) "
                            f"roundtrip=(block={fresh_block}, trans={fresh_trans}, dh={fresh_dh})"
                        )
                except Exception as exc:
                    # Transport error during round-trip: we cannot confirm the
                    # anchor either way. Mark "unverifiable" rather than
                    # inflating "present" — a network partition must NOT silently
                    # turn into valid:true. Callers can distinguish "anchor
                    # confirmed missing" (anchor_missing) from "anchor
                    # confirmation failed" (anchor_unverifiable).
                    logger.warning("anchor round-trip failed for EId=%s: %s", eid, exc)
                    anchor_unverifiable += 1
                    anchor_status = "unverifiable"
            elif has_anchor:
                # C2: anchor fields exist on the record body but we don't have
                # an EId to round-trip them. This is "present" (we can't
                # tamper-check) NOT "verified" (we proved it matches). The
                # pre-fix code lumped both buckets under `anchor_ok`, so the
                # session-level `valid` accepted these as verified.
                anchor_present_only += 1
                anchor_status = "present"
            else:
                anchor_missing += 1

            # Per-record validity: passes when structural is OK, signature is
            # not invalid (absent/unverifiable don't fail the row), and anchor
            # didn't round-trip-mismatch.
            record_valid = (
                structural_ok
                and sig_status != "invalid"
                and anchor_status != "mismatched"
            )

            per_record.append({
                "execution_id": execution_id,
                "sequence_number": seq,
                "record_id": rec_id,
                "structural_ok": structural_ok,
                "signature": sig_status,
                "anchor": anchor_status,
                # C6 read-side: dashboard renders `r.valid !== false` for the
                # per-row tick. Make it explicit.
                "valid": record_valid,
                "walacor_block_id": block_id,
                "walacor_trans_id": trans_id,
                "walacor_dh": dh,
            })

        # Derive verification_level (C2):
        #   * "verified"     — every anchor round-tripped with a match AND no
        #                      structural / signature errors.
        #   * "structural"   — structural integrity intact, no anchor
        #                      mismatches OR round-trip failures, but at
        #                      least one record was not independently
        #                      round-tripped (anchor present on body without
        #                      EId, or anchor absent on legacy records).
        #                      Strong claim about LINKAGE only — not about
        #                      whether the Walacor seal still holds today.
        #   * "unverifiable" — anything else (transport failures during
        #                      round-trip, mismatched anchor fields,
        #                      structural breaks, bad signatures).
        signatures_clean = sig_invalid == 0
        structural_clean = len(errors) == 0
        no_anchor_failures = (
            anchor_mismatched == 0
            and anchor_unverifiable == 0
        )
        if (
            structural_clean
            and signatures_clean
            and no_anchor_failures
            and anchor_missing == 0
            and anchor_present_only == 0
        ):
            verification_level = "verified"
        elif (
            structural_clean
            and signatures_clean
            and no_anchor_failures
        ):
            # Chain linkage intact and no anchor evidence is contradictory.
            # Some records have anchors that weren't round-tripped (or no
            # anchor at all — legacy records). We can vouch for the chain
            # structure but not for "the seal is still intact at Walacor".
            verification_level = "structural"
        else:
            verification_level = "unverifiable"

        return {
            # Top-level `valid` only when the strongest level holds. A network
            # partition that makes the round-trip fail, OR an anchor present
            # on the record body but not independently verified, must NOT
            # produce valid:true (the original C2 bug).
            "valid": verification_level == "verified",
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
                "anchors":    {
                    # "verified" = round-tripped & matched
                    # "present"  = on the body, not round-tripped
                    "verified": anchor_verified,
                    "present": anchor_present_only,
                    "absent": anchor_missing,
                    "mismatched": anchor_mismatched,
                    "unverifiable": anchor_unverifiable,
                    "independent_roundtrip": roundtrips_attempted,
                },
            },
            "records": per_record,
            # Back-compat for older clients.
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

    # ── Compliance queries ────────────────────────────────────────────────

    async def get_compliance_summary(self, start: str, end: str) -> dict:
        # Attempts stats
        att_pipeline = [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": "$disposition", "count": {"$sum": 1}}},
        ]
        att_rows = await self._client.query_complex(self._att_etid, att_pipeline)
        stats = {r["_id"]: r["count"] for r in att_rows if r.get("_id")}
        total = sum(stats.values())
        allowed = stats.get("allowed", 0) + stats.get("forwarded", 0)

        # Models used
        model_pipeline = [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}, "model_id": {"$ne": None}}},
            {"$group": {"_id": "$model_id"}},
        ]
        model_rows = await self._client.query_complex(self._exec_etid, model_pipeline)
        models_used = [r["_id"] for r in model_rows if r.get("_id")]

        # Content-analysis coverage: percent of executions in the window
        # whose metadata carries a content_analysis block. This measures
        # whether analyzers actually RAN on traffic, which is what the
        # compliance score needs — not just whether analyzers were
        # configured at boot time.
        ca_pipeline = [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}}},
            {"$project": {"metadata_json": 1, "content_analysis": 1}},
        ]
        ca_rows = await self._client.query_complex(self._exec_etid, ca_pipeline)
        total_exec = len(ca_rows)
        analyzed = 0
        for r in ca_rows:
            if _has_content_analysis(r):
                analyzed += 1
        coverage_pct = round(analyzed / total_exec * 100, 1) if total_exec else 0.0

        return {
            "total_requests": total,
            "allowed": allowed,
            "denied": total - allowed,
            "models_used": models_used,
            "total_executions": total_exec,
            "content_analysis_coverage_pct": coverage_pct,
            "content_analysis_covered": analyzed,
        }

    async def get_execution_export(self, start: str, end: str, limit: int = 10000) -> list[dict]:
        pipeline = [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}}},
            {"$sort": {"timestamp": 1}},
            {"$limit": limit},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        return [_deserialize_record(r) for r in rows]

    async def get_attestation_summary(self, start: str, end: str) -> list[dict]:
        pipeline = [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}, "model_id": {"$ne": None}}},
            {"$group": {
                "_id": {"model_id": "$model_id", "provider": "$provider"},
                "attestation_id": {"$last": "$model_attestation_id"},
                "request_count": {"$sum": 1},
                "total_tokens": {"$sum": "$total_tokens"},
            }},
            {"$sort": {"request_count": -1}},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        return [
            {
                "model_id": (r.get("_id") or {}).get("model_id", "unknown"),
                "provider": (r.get("_id") or {}).get("provider", "unknown"),
                "attestation_id": r.get("attestation_id"),
                "request_count": r.get("request_count", 0),
                "total_tokens": r.get("total_tokens", 0),
            }
            for r in rows
        ]

    async def get_chain_verification_report(self, start: str, end: str) -> list[dict]:
        pipeline = [
            {"$match": {"timestamp": {"$gte": start, "$lt": end}, "session_id": {"$ne": None}}},
            {"$group": {"_id": "$session_id"}},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        results = []
        for r in rows:
            sid = r.get("_id")
            if sid:
                results.append(await self.verify_chain(sid))
        return results

    async def get_cost_summary(self, range_key: str = "24h", group_by: str = "model") -> dict:
        interval_map = {"1h": "-1 hour", "24h": "-1 day", "7d": "-7 days", "30d": "-30 days"}
        hours_map = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}
        hours = hours_map.get(range_key, 24)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        group_field = "$user" if group_by == "user" else "$model_id"
        group_alias = "user" if group_by == "user" else "model"

        pipeline = [
            {"$match": {"timestamp": {"$gte": cutoff}}},
            {"$group": {
                "_id": group_field,
                "request_count": {"$sum": 1},
                "total_prompt_tokens": {"$sum": "$prompt_tokens"},
                "total_completion_tokens": {"$sum": "$completion_tokens"},
                "total_tokens": {"$sum": "$total_tokens"},
                "total_cost_usd": {"$sum": "$estimated_cost_usd"},
            }},
            {"$sort": {"total_cost_usd": -1}},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)

        entries = []
        grand_total = 0.0
        for r in rows:
            cost = r.get("total_cost_usd") or 0.0
            entries.append({
                group_alias: r.get("_id") or "unknown",
                "request_count": r.get("request_count", 0),
                "prompt_tokens": r.get("total_prompt_tokens", 0),
                "completion_tokens": r.get("total_completion_tokens", 0),
                "total_tokens": r.get("total_tokens", 0),
                "cost_usd": round(cost, 6),
            })
            grand_total += cost

        return {
            "range": range_key,
            "group_by": group_by,
            "entries": entries,
            "grand_total_usd": round(grand_total, 6),
        }

    async def get_attachments(self, session_id: str) -> list[dict]:
        pipeline = [
            {"$match": {"session_id": session_id}},
            {"$project": {"execution_id": 1, "metadata_json": 1}},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        attachments = []
        for r in rows:
            mj = r.get("metadata_json")
            meta = json.loads(mj) if mj and isinstance(mj, str) else {}
            fm = meta.get("file_metadata", [])
            for f in fm:
                f["execution_id"] = r.get("execution_id", "")
                attachments.append(f)
        return attachments

    async def get_ab_test_results(self, test_name: str) -> dict:
        pipeline = [
            {"$match": {"metadata.ab_variant": test_name}},
            {"$group": {
                "_id": "$model_id",
                "ab_variant": {"$last": "$metadata.ab_variant"},
                "original_model": {"$last": "$metadata.ab_original_model"},
                "request_count": {"$sum": 1},
                "avg_latency_ms": {"$avg": "$latency_ms"},
                "total_tokens": {"$sum": "$total_tokens"},
                "avg_tokens": {"$avg": "$total_tokens"},
            }},
            {"$sort": {"request_count": -1}},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        variants = []
        for r in rows:
            variants.append({
                "model_id": r.get("_id"),
                "ab_variant": r.get("ab_variant"),
                "original_model": r.get("original_model"),
                "request_count": r.get("request_count", 0),
                "avg_latency_ms": round(r["avg_latency_ms"], 1) if r.get("avg_latency_ms") else None,
                "total_tokens": r.get("total_tokens", 0),
                "avg_tokens": round(r["avg_tokens"], 1) if r.get("avg_tokens") else None,
            })
        return {
            "test_name": test_name,
            "variants": variants,
            "total_requests": sum(v["request_count"] for v in variants),
        }

    def close(self) -> None:
        """No-op — WalacorClient lifecycle is managed by main.py."""
        pass
