"""WalacorLineageReader: server-side bucket-aggregation for metrics/token history.

These tests pin the contract that ``get_metrics_history`` and
``get_token_latency_history`` push ``$group`` into Walacor — i.e. the
returned canonical buckets are populated from pre-bucketed rows, not from
one-row-per-attempt scans.  Catches a regression that would re-introduce the
unbounded ``$project``-then-paginate-in-Python pattern.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.lineage.walacor_reader import WalacorLineageReader


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_reader() -> WalacorLineageReader:
    client = MagicMock()
    client.query_complex = AsyncMock(return_value=[])
    reader = WalacorLineageReader.__new__(WalacorLineageReader)
    reader._client = client
    reader._exec_etid = "9000001"
    reader._tool_etid = "9000003"
    reader._att_etid = "9000002"
    reader._attempt_etid = "9000002"  # backcompat with existing fixture name
    reader.logger = MagicMock()
    return reader


def _pipeline_pushed(client: MagicMock) -> list[dict]:
    """Return the last pipeline passed to query_complex."""
    assert client.query_complex.await_count >= 1
    _etid, pipeline = client.query_complex.await_args.args
    return pipeline


def _has_group_stage(pipeline: list[dict]) -> bool:
    return any("$group" in stage for stage in pipeline)


@pytest.mark.anyio
async def test_get_metrics_history_pushes_group_to_walacor() -> None:
    reader = _make_reader()
    # Hourly bucket prefix (substr_len=13 for 24h) — pretend two buckets came back.
    now = datetime.now(timezone.utc)
    h1 = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H")
    h2 = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
    reader._client.query_complex.return_value = [
        {"_id": {"bucket": h1, "disposition": "allowed"}, "count": 3},
        {"_id": {"bucket": h1, "disposition": "blocked"}, "count": 1},
        {"_id": {"bucket": h2, "disposition": "forwarded"}, "count": 5},
    ]
    out = await reader.get_metrics_history("24h")

    # Assert pipeline pushed $group server-side.
    pipeline = _pipeline_pushed(reader._client)
    assert _has_group_stage(pipeline), f"expected $group in pipeline, got {pipeline}"
    group = next(s["$group"] for s in pipeline if "$group" in s)
    # Bucket key must be a $substr (server-side prefix), not a Python-side strftime.
    assert "$substr" in str(group["_id"])

    # Canonical shape.
    assert out["range"] == "24h"
    assert len(out["buckets"]) == 24
    assert all({"t", "total", "allowed", "blocked"} <= b.keys() for b in out["buckets"])

    # Counts from the mocked $group rows landed in the right buckets.
    h1_label = h1 + ":00:00"
    h2_label = h2 + ":00:00"
    by_t = {b["t"]: b for b in out["buckets"]}
    assert by_t[h1_label]["total"] == 4
    assert by_t[h1_label]["allowed"] == 3
    assert by_t[h1_label]["blocked"] == 1
    assert by_t[h2_label]["total"] == 5
    assert by_t[h2_label]["allowed"] == 5
    assert by_t[h2_label]["blocked"] == 0


@pytest.mark.anyio
async def test_get_token_latency_history_pushes_group_to_walacor() -> None:
    reader = _make_reader()
    now = datetime.now(timezone.utc)
    h1 = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
    reader._client.query_complex.return_value = [
        {
            "_id": h1,
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
            "avg_latency_ms": 42.5,
            "max_latency_ms": 90.0,
            "request_count": 7,
        },
    ]
    out = await reader.get_token_latency_history("24h")

    pipeline = _pipeline_pushed(reader._client)
    assert _has_group_stage(pipeline)
    group = next(s["$group"] for s in pipeline if "$group" in s)
    # Latency is summarized server-side: $avg / $max, not raw list shipped back.
    assert group.get("avg_latency_ms") == {"$avg": "$latency_ms"}
    assert group.get("max_latency_ms") == {"$max": "$latency_ms"}
    assert group.get("request_count") == {"$sum": 1}
    # Bucket key pushed via $substr.
    assert "$substr" in str(group["_id"])

    by_t = {b["t"]: b for b in out["buckets"]}
    label = h1 + ":00:00"
    assert by_t[label]["prompt_tokens"] == 100
    assert by_t[label]["completion_tokens"] == 200
    assert by_t[label]["total_tokens"] == 300
    assert by_t[label]["avg_latency_ms"] == 42.5
    assert by_t[label]["max_latency_ms"] == 90.0
    assert by_t[label]["request_count"] == 7


@pytest.mark.anyio
async def test_count_sessions_in_window_uses_distinct_group() -> None:
    reader = _make_reader()
    reader._client.query_complex.return_value = [{"n": 6290}]
    n = await reader.count_sessions_in_window("2026-05-01", "2026-05-20")
    assert n == 6290
    pipeline = _pipeline_pushed(reader._client)
    # Distinct sessions via $group then $count.
    assert any("$group" in s and s["$group"].get("_id") == "$session_id" for s in pipeline)
    assert any("$count" in s for s in pipeline)
