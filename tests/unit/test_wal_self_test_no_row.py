"""Regression test for fix A6: self-test must not persist a WAL row.

The old startup self-test wrote a synthetic execution_id="self-test-startup"
row into the real WAL and marked it delivered locally. Walacor never saw
the row, so it sat in the WAL forever (one persistent self-test row per
deployment, unbounded across restarts in some old configurations).

The fix validates the schema against an in-memory SQLite connection so
no row ever lands in the real WAL file.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from gateway.config import get_settings
from gateway.main import _self_test
from gateway.pipeline.context import get_pipeline_context
from gateway.wal.writer import WALWriter


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_self_test_does_not_write_to_real_wal(tmp_path, monkeypatch):
    """A6: after _self_test runs, the real WAL file must have zero rows.

    We pre-create the wal_records table (via the WALWriter's normal
    schema bootstrap) so the test can SELECT COUNT(*). _self_test must
    NOT add a row through that schema — the in-memory smoke validation
    is invisible to the real file.
    """
    db_path = str(tmp_path / "wal.db")
    wal = WALWriter(db_path)
    # Force the schema onto the real file so the COUNT(*) below has a
    # table to query against. We do NOT insert any rows.
    wal._ensure_conn()

    ctx = get_pipeline_context()
    original_wal = ctx.wal_writer
    ctx.wal_writer = wal

    # Minimal settings to satisfy _self_test:
    monkeypatch.setenv("WALACOR_CONTROL_PLANE_URL", "")
    monkeypatch.setenv("WALACOR_PROVIDER_OLLAMA_URL", "")
    monkeypatch.setenv("WALACOR_SKIP_GOVERNANCE", "true")
    get_settings.cache_clear()

    try:
        await _self_test()
    finally:
        ctx.wal_writer = original_wal
        wal.close()
        get_settings.cache_clear()

    # Open the real DB directly; expect ZERO rows.
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM wal_records").fetchone()[0]
    finally:
        conn.close()

    assert count == 0, (
        "Self-test must not persist a real WAL row — schema validation "
        "should happen against an in-memory connection."
    )
