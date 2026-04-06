"""Walacor-backed lineage reader — replaces SQLite LineageReader.

Queries execution records, attempts, and tool events via Walacor's
/api/query/getcomplex endpoint using MongoDB-style aggregation pipelines.
All methods are async (Walacor API is HTTP-based).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.walacor.client import WalacorClient

logger = logging.getLogger(__name__)


def _deserialize_record(r: dict) -> dict:
    """Convert Walacor storage format back to gateway record format.

    - metadata_json (string) → metadata (dict)
    - Strips Walacor internal fields (_id, ORGId, UID, IsDeleted, SV, etc.)
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
    # Strip Walacor internal fields that leak into query results
    for k in ("_id", "ORGId", "UID", "IsDeleted", "SV", "LastModifiedBy"):
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
            pipeline.insert(1, {"$match": {
                "$or": [
                    {"session_id": {"$regex": search, "$options": "i"}},
                    {"model_id": {"$regex": search, "$options": "i"}},
                    {"user": {"$regex": search, "$options": "i"}},
                ]
            }})

        pipeline.extend([{"$skip": offset}, {"$limit": limit}])
        rows = await self._client.query_complex(self._exec_etid, pipeline)

        # Extract session IDs for tool event lookup
        session_ids = [r.get("_id") or r.get("session_id") for r in rows if r.get("_id") or r.get("session_id")]
        tool_map = await self._get_session_tool_indicators(session_ids) if session_ids else {}

        results = []
        for r in rows:
            sid = r.get("_id") or r.get("session_id")
            meta = self._parse_session_metadata(r.get("metadata_json"))
            tools = tool_map.get(sid, {})
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
            })
        return results

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
        # (the real user question is in an earlier record)
        is_system = request_type.startswith("system_task")
        return {
            "user_question": None if is_system else (audit.get("user_question") or None),
            "has_rag_context": False if is_system else audit.get("has_rag_context", False),
            "has_files": False if is_system else (audit.get("has_files", False) or audit.get("file_count", 0) > 0),
            "has_images": False if is_system else audit.get("has_images", False),
            "request_type": "user_message" if is_system else request_type,
            "user_message_count": audit.get("conversation_turns", 0) or 0,
        }

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
            logger.debug("Tool event indicator query failed", exc_info=True)
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
            pipeline.insert(1, {"$match": {
                "$or": [
                    {"session_id": {"$regex": search, "$options": "i"}},
                    {"model_id": {"$regex": search, "$options": "i"}},
                    {"user": {"$regex": search, "$options": "i"}},
                ]
            }})
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        return rows[0]["total"] if rows else 0

    # ── Session timeline ──────────────────────────────────────────────────

    async def get_session_timeline(self, session_id: str) -> list[dict]:
        pipeline = [
            {"$match": {"session_id": session_id}},
            {"$sort": {"sequence_number": 1, "CreatedAt": 1}},
            {"$lookup": {
                "from": "envelopes",
                "localField": "EId",
                "foreignField": "EId",
                "as": "env",
            }},
        ]
        rows = await self._client.query_complex(self._exec_etid, pipeline)
        results = []
        for r in rows:
            _deserialize_record(r)
            r["_walacor_eid"] = r.get("EId")
            env = r.pop("env", [])
            if env:
                r["_envelope"] = {
                    "block_id": env[0].get("BlockId"),
                    "trans_id": env[0].get("TransId"),
                    "data_hash": env[0].get("DH"),
                    "block_level": env[0].get("BL"),
                    "created_at": env[0].get("CreatedAt"),
                }
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
        _deserialize_record(r)
        r["_walacor_eid"] = r.get("EId")
        env = r.pop("env", [])
        if env:
            r["_envelope"] = {
                "block_id": env[0].get("BlockId"),
                "trans_id": env[0].get("TransId"),
                "data_hash": env[0].get("DH"),
                "block_level": env[0].get("BL"),
                "block_index": env[0].get("BlockIndexId"),
                "created_at": env[0].get("CreatedAt"),
            }
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
    ) -> dict:
        sort_dir = 1 if str(order).lower() == "asc" else -1
        sort_field = sort if sort in (
            "timestamp", "disposition", "request_id", "user",
            "model_id", "path", "status_code",
        ) else "timestamp"

        match_stage: dict[str, Any] = {}
        if search and search.strip():
            match_stage = {"$or": [
                {"request_id": {"$regex": search, "$options": "i"}},
                {"tenant_id": {"$regex": search, "$options": "i"}},
                {"provider": {"$regex": search, "$options": "i"}},
                {"model_id": {"$regex": search, "$options": "i"}},
                {"disposition": {"$regex": search, "$options": "i"}},
                {"user": {"$regex": search, "$options": "i"}},
            ]}

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
                "disposition": 1, "execution_id": 1, "status_code": 1, "user": 1,
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
        """Verify Merkle chain integrity for a session."""
        from gateway.core import compute_sha3_512_string
        from gateway.pipeline.session_chain import GENESIS_HASH

        records = await self.get_session_timeline(session_id)
        errors: list[str] = []
        prev_hash = GENESIS_HASH

        for i, r in enumerate(records):
            seq = r.get("sequence_number", i)
            stored_prev = r.get("previous_record_hash", "")
            stored_hash = r.get("record_hash", "")

            if stored_prev != prev_hash:
                errors.append(
                    f"Record seq={seq}: previous_record_hash mismatch "
                    f"(expected {prev_hash[:16]}…, got {stored_prev[:16]}…)"
                )

            canonical = "|".join([
                r.get("execution_id", ""),
                str(r.get("policy_version", "")),
                r.get("policy_result", ""),
                stored_prev,
                str(seq),
                r.get("timestamp", ""),
            ])
            computed = compute_sha3_512_string(canonical)
            if computed != stored_hash:
                errors.append(
                    f"Record seq={seq}: record_hash mismatch "
                    f"(computed {computed[:16]}…, got {stored_hash[:16]}…)"
                )
            prev_hash = stored_hash

        return {
            "valid": len(errors) == 0,
            "record_count": len(records),
            "errors": errors,
            "session_id": session_id,
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

        return {
            "total_requests": total,
            "allowed": allowed,
            "denied": total - allowed,
            "models_used": models_used,
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
                "model_id": r["_id"]["model_id"],
                "provider": r["_id"]["provider"],
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
