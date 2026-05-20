"""Compliance API reads chain integrity from the worker's SQLite store.

Pins the contract:
  - When the store is populated, ``_compute_shared_report`` returns the
    store's sessions and marks ``sampled=False`` (real census).
  - When the store is empty (fresh deploy, worker hasn't ticked yet),
    ``pending=True`` and ``sessions_verified=0`` so the dashboard can
    render an honest empty state.
  - The store reader path is independent of the old
    ``reader.get_chain_verification_report`` — even if that reader
    returns an empty list, a populated store still surfaces the census.
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _reset_state():
    from gateway.compliance import api as compliance_api
    from gateway.pipeline import context as ctx_mod
    from gateway.pipeline.context import get_pipeline_context
    # Restore the import in case a prior test left a `patch()` leak —
    # concurrent `with patch("gateway.compliance.api.get_pipeline_context")`
    # blocks (test_compliance_api.py::test_concurrent_...) can stack and
    # unstack out of order, leaving the api module pointing at a stale
    # MagicMock. Force it back to the real function before each test.
    compliance_api.get_pipeline_context = ctx_mod.get_pipeline_context
    compliance_api._REPORT_CACHE.clear()
    compliance_api._REPORT_INFLIGHT.clear()
    ctx = get_pipeline_context()
    saved = getattr(ctx, "chain_verification_store", None)
    ctx.chain_verification_store = None
    yield
    ctx.chain_verification_store = saved
    compliance_api._REPORT_CACHE.clear()
    compliance_api._REPORT_INFLIGHT.clear()


def _stub_reader():
    """Minimal reader stub for ``_compute_shared_report``."""
    reader = types.SimpleNamespace()

    async def _summary(start, end):
        return {"total_executions": 42}

    async def _execs(start, end, limit=1000):
        return [{"execution_id": f"e{i}"} for i in range(3)]

    async def _atts(start, end):
        return []

    async def _chain(start, end, sample_limit=50):
        # On the new code path this is NOT called by the dashboard
        # request handler — but we still stub it because the request
        # handler's gather() includes the other reader calls.
        return []

    async def _count(start, end):
        return 100

    reader.get_compliance_summary = _summary
    reader.get_execution_export = _execs
    reader.get_attestation_summary = _atts
    reader.get_chain_verification_report = _chain
    reader.count_sessions_in_window = _count
    return reader


@pytest.mark.anyio
async def test_compliance_export_reads_from_store_when_populated(tmp_path):
    """Populated store → census surfaces in chain_integrity (not pending)."""
    from gateway.compliance.api import _compute_shared_report
    from gateway.compliance.chain_store import ChainVerificationStore
    from gateway.pipeline.context import get_pipeline_context

    store = ChainVerificationStore(str(tmp_path / "chain.db"))
    store.upsert_many([
        {"session_id": "s1", "valid": True, "verification_level": "ok",
         "errors": [], "records_checked": 4},
        {"session_id": "s2", "valid": False, "verification_level": "warn",
         "errors": ["sig:missing"], "records_checked": 2},
    ])
    store.set_meta("last_tick_at", "2026-05-19T12:00:00+00:00")

    ctx = get_pipeline_context()
    ctx.chain_verification_store = store

    out = await _compute_shared_report(_stub_reader(), "2026-05-12", "2026-05-19")
    ci = out["chain_integrity"]
    assert ci["sessions_verified"] == 2
    assert ci["sampled"] is False
    assert ci["pending"] is False
    assert ci["all_valid"] is False  # s2 was invalid
    assert {s["session_id"] for s in ci["sessions"]} == {"s1", "s2"}
    assert ci["last_verification_at"] == "2026-05-19T12:00:00+00:00"
    assert ci["total_sessions_in_window"] == 100  # from count_sessions_in_window


@pytest.mark.anyio
async def test_compliance_export_pending_when_store_empty(tmp_path):
    """Empty store → pending=True, sessions_verified=0, still surfaces total."""
    from gateway.compliance.api import _compute_shared_report
    from gateway.compliance.chain_store import ChainVerificationStore
    from gateway.pipeline.context import get_pipeline_context

    store = ChainVerificationStore(str(tmp_path / "chain.db"))
    ctx = get_pipeline_context()
    ctx.chain_verification_store = store

    out = await _compute_shared_report(_stub_reader(), "2026-05-12", "2026-05-19")
    ci = out["chain_integrity"]
    assert ci["pending"] is True
    assert ci["sessions_verified"] == 0
    assert ci["sessions"] == []
    assert ci["sampled"] is False
    # Window total is still honest — the dashboard can render
    # "0 of N pending" instead of implying nothing exists at all.
    assert ci["total_sessions_in_window"] == 100


@pytest.mark.anyio
async def test_compliance_export_pending_when_store_not_wired():
    """No store on ctx (transparent-proxy / lineage disabled) → pending=True."""
    from gateway.compliance.api import _compute_shared_report
    from gateway.pipeline.context import get_pipeline_context

    ctx = get_pipeline_context()
    ctx.chain_verification_store = None

    out = await _compute_shared_report(_stub_reader(), "2026-05-12", "2026-05-19")
    ci = out["chain_integrity"]
    assert ci["pending"] is True
    assert ci["sessions_verified"] == 0
    assert ci["sampled"] is False
