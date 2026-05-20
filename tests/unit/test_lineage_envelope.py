"""Regression tests for /v1/lineage/envelope — Walacor timeout guard.

Pins the 8s `_LINEAGE_READ_TIMEOUT_S` bound around the direct
``walacor_client.query_complex`` call. Before this guard a stalled Walacor
read pinned the coroutine for the httpx 30s default, wedging the dashboard
read pool. The guard fails fast with 504 — the same shape used by the
compliance export endpoint.

See CLAUDE.md "Failure modes & guards" for the broader rationale on
chokepoint-style guards over discipline.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _envelope_request(execution_id: str) -> Request:
    scope = {
        "type": "http", "method": "GET",
        "path": f"/v1/lineage/envelope/{execution_id}",
        "query_string": b"",
        "headers": [],
        "path_params": {"execution_id": execution_id},
    }
    return Request(scope)


@pytest.mark.anyio
async def test_lineage_envelope_504_on_walacor_timeout(monkeypatch):
    """A slow walacor_client.query_complex must trip the 8s timeout and
    return 504 fast — not wait out the httpx 30s default."""
    from gateway.lineage import api as lineage_api

    # Shrink the bound so the test runs in ~0.2s, not ~8s. The bound is a
    # module-level constant; monkeypatching it is the documented test seam.
    monkeypatch.setattr(lineage_api, "_LINEAGE_READ_TIMEOUT_S", 0.2)

    # Local reader resolves the execution (so we reach the Walacor branch).
    mock_reader = MagicMock()
    mock_reader.get_execution.return_value = {
        "execution_id": "exec-slow",
        "walacor_block_id": None,
        "walacor_trans_id": None,
        "walacor_dh": None,
        "record_hash": None,
    }

    async def _slow_query(*_args, **_kwargs):
        await asyncio.sleep(5.0)  # would hang for 5s if not bounded
        return []

    mock_walacor = MagicMock()
    mock_walacor.query_complex = AsyncMock(side_effect=_slow_query)

    mock_ctx = MagicMock()
    mock_ctx.lineage_reader = mock_reader
    mock_ctx.walacor_client = mock_walacor

    with patch("gateway.lineage.api.get_pipeline_context", return_value=mock_ctx):
        start = time.monotonic()
        response = await lineage_api.lineage_envelope(_envelope_request("exec-slow"))
        elapsed = time.monotonic() - start

    assert response.status_code == 504, response.body
    body = json.loads(response.body)
    assert body["envelope"] is None
    assert "timed out" in body["error"].lower()
    # Allow a generous ceiling; the point is it does NOT wait 5s.
    assert elapsed < 2.0, f"envelope timeout did not fire fast enough: {elapsed:.2f}s"


@pytest.mark.anyio
async def test_lineage_envelope_happy_path_unaffected(monkeypatch):
    """A fast query_complex still returns 200 with the envelope payload —
    the timeout guard must not break the success path."""
    from gateway.lineage import api as lineage_api

    mock_reader = MagicMock()
    mock_reader.get_execution.return_value = {
        "execution_id": "exec-fast",
        "walacor_block_id": "blk-1",
        "walacor_trans_id": "tx-1",
        "walacor_dh": "dh-1",
        "record_hash": "rh-1",
    }
    mock_walacor = MagicMock()
    mock_walacor.query_complex = AsyncMock(return_value=[{
        "execution_id": "exec-fast",
        "BlockId": "blk-1", "TransId": "tx-1", "DH": "dh-1",
    }])

    mock_ctx = MagicMock()
    mock_ctx.lineage_reader = mock_reader
    mock_ctx.walacor_client = mock_walacor

    with patch("gateway.lineage.api.get_pipeline_context", return_value=mock_ctx):
        response = await lineage_api.lineage_envelope(_envelope_request("exec-fast"))

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["envelope"]["BlockId"] == "blk-1"
    assert body["match"]["all_ok"] is True
