"""Background pre-computation worker for compliance reports.

Pins the contract: the worker pre-warms the api._REPORT_CACHE for the
dashboard's default RangePicker windows on every tick, so the first
dashboard load lands on a cache hit instead of a ~5 s cold compute.
"""
from __future__ import annotations

import asyncio
import types

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _reset_state():
    from gateway.compliance import api as compliance_api
    compliance_api._REPORT_CACHE.clear()
    compliance_api._REPORT_INFLIGHT.clear()
    yield
    compliance_api._REPORT_CACHE.clear()
    compliance_api._REPORT_INFLIGHT.clear()


def _stub_reader():
    """Async-compatible reader stub matching WalacorLineageReader's shape."""
    reader = types.SimpleNamespace()

    async def _summary(start, end):
        return {"total_requests": 10, "allowed": 9, "denied": 1,
                "models_used": ["x"], "total_executions": 10,
                "content_analysis_coverage_pct": 90.0}

    async def _execs(start, end, limit=1000):
        return [{"execution_id": "e1", "session_id": "s1"}]

    async def _atts(start, end):
        return [{"model_id": "x", "provider": "p", "attestation_id": "a"}]

    async def _chain(start, end):
        return [{"session_id": "s1", "valid": True, "verification_level": "verified"}]

    reader.get_compliance_summary = _summary
    reader.get_execution_export = _execs
    reader.get_attestation_summary = _atts
    reader.get_chain_verification_report = _chain
    return reader


@pytest.mark.anyio
async def test_worker_warms_cache_for_default_windows_on_first_tick():
    """After one tick, every default RangePicker window has a cache entry."""
    from gateway.compliance import api as compliance_api
    from gateway.compliance.precompute import CompliancePrecomputeWorker, _PREWARM_WINDOWS, _today_window

    worker = CompliancePrecomputeWorker(_stub_reader(), tick_interval_s=60.0)
    # Call _tick_once directly — we don't want to wait on the run() loop's
    # sleep just to test the pre-warm behavior.
    await worker._tick_once()

    for _, days_back in _PREWARM_WINDOWS:
        key = _today_window(days_back)
        assert key in compliance_api._REPORT_CACHE, (
            f"expected window {key} in cache after one tick; "
            f"have {list(compliance_api._REPORT_CACHE.keys())}"
        )
    assert worker.health["last_tick_ok"] is True


@pytest.mark.anyio
async def test_worker_logs_warning_on_reader_failure_does_not_raise():
    """A reader that raises must not kill the worker — fail-open."""
    from gateway.compliance.precompute import CompliancePrecomputeWorker

    bad_reader = types.SimpleNamespace()
    async def _boom(*args, **kw):
        raise RuntimeError("walacor unreachable")
    bad_reader.get_compliance_summary = _boom
    bad_reader.get_execution_export = _boom
    bad_reader.get_attestation_summary = _boom
    bad_reader.get_chain_verification_report = _boom

    worker = CompliancePrecomputeWorker(bad_reader, tick_interval_s=60.0)
    # Must not raise.
    await worker._tick_once()
    assert worker.health["last_tick_ok"] is False
    assert "walacor unreachable" in (worker.health["last_error"] or "")


@pytest.mark.anyio
async def test_subsequent_request_hits_warmed_cache():
    """End-to-end: tick warms cache; calling the request handler with
    one of the pre-warmed windows must read from cache without invoking
    the reader again."""
    from gateway.compliance.precompute import CompliancePrecomputeWorker, _today_window

    reader = _stub_reader()
    call_count = {"n": 0}
    real_summary = reader.get_compliance_summary
    async def _counted_summary(start, end):
        call_count["n"] += 1
        return await real_summary(start, end)
    reader.get_compliance_summary = _counted_summary

    worker = CompliancePrecomputeWorker(reader, tick_interval_s=60.0)
    await worker._tick_once()
    n_after_warm = call_count["n"]
    assert n_after_warm >= 1, "worker should have called the reader at least once"

    # Now request the same window through _load_shared_report — should
    # hit the cache, NOT re-call the reader.
    from gateway.compliance.api import _load_shared_report
    start, end = _today_window(7)
    await _load_shared_report(reader, start, end)
    assert call_count["n"] == n_after_warm, (
        f"expected cache hit (call_count unchanged at {n_after_warm}) "
        f"but reader was called again — total now {call_count['n']}"
    )
