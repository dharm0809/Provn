"""Tests for the readiness runner: concurrency, timeout, exception, cache, endpoint auth."""

from __future__ import annotations

import asyncio
import types

import pytest

anyio_backend = pytest.fixture(params=["asyncio"])(lambda request: request.param)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_ctx(**kw):
    d = dict(wal_writer=None, walacor_client=None)
    d.update(kw)
    return types.SimpleNamespace(**d)


def _make_check(check_id, status="green", *, sleep_s=0.0, raise_exc=None):
    from gateway.readiness.protocol import Category, CheckResult, Severity

    class _Check:
        id = check_id
        name = f"Test {check_id}"
        category = Category.security
        severity = Severity.warn

        async def run(self, ctx):
            if sleep_s:
                await asyncio.sleep(sleep_s)
            if raise_exc:
                raise raise_exc
            return CheckResult(status=status, detail="test detail")

    return _Check()


# ─── Runner tests ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_run_all_concurrent(monkeypatch):
    """All checks run concurrently — total time ≈ slowest individual check."""
    import gateway.readiness.runner as runner_mod
    import gateway.readiness.registry as reg_mod

    original = reg_mod._REGISTERED[:]
    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.extend([
        _make_check("T-01", sleep_s=0.05),
        _make_check("T-02", sleep_s=0.05),
        _make_check("T-03", sleep_s=0.05),
    ])
    runner_mod._cache = None

    import time
    t0 = time.monotonic()
    report = await runner_mod.run_all(_make_ctx(), fresh=True)
    elapsed = time.monotonic() - t0

    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.extend(original)

    assert elapsed < 0.3, f"Checks should run concurrently (took {elapsed:.2f}s)"
    assert report.summary["green"] == 3


@pytest.mark.anyio
async def test_run_all_timeout_becomes_amber(monkeypatch):
    import gateway.readiness.runner as runner_mod
    import gateway.readiness.registry as reg_mod

    original = reg_mod._REGISTERED[:]
    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.append(_make_check("T-SLOW", sleep_s=999))
    runner_mod._cache = None

    monkeypatch.setattr(runner_mod, "_CHECK_TIMEOUT_S", 0.05)

    report = await runner_mod.run_all(_make_ctx(), fresh=True)

    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.extend(original)
    monkeypatch.undo()

    check = report.checks[0]
    assert check["status"] == "amber"
    assert "timed out" in check["detail"]


@pytest.mark.anyio
async def test_run_all_exception_becomes_amber():
    import gateway.readiness.runner as runner_mod
    import gateway.readiness.registry as reg_mod

    original = reg_mod._REGISTERED[:]
    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.append(_make_check("T-ERR", raise_exc=RuntimeError("boom")))
    runner_mod._cache = None

    report = await runner_mod.run_all(_make_ctx(), fresh=True)

    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.extend(original)

    check = report.checks[0]
    assert check["status"] == "amber"
    assert "boom" in check["detail"]


@pytest.mark.anyio
async def test_cache_ttl():
    """Second call within TTL returns cached result without re-running checks."""
    import gateway.readiness.runner as runner_mod
    import gateway.readiness.registry as reg_mod

    call_count = 0

    class _CountingCheck:
        id = "T-COUNT"
        name = "counting"
        from gateway.readiness.protocol import Category, Severity
        category = Category.security
        severity = Severity.warn

        async def run(self, ctx):
            nonlocal call_count
            call_count += 1
            from gateway.readiness.protocol import CheckResult
            return CheckResult(status="green", detail="ok")

    original = reg_mod._REGISTERED[:]
    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.append(_CountingCheck())
    runner_mod._cache = None
    runner_mod._cache_lock_obj = None

    await runner_mod.run_all(_make_ctx(), fresh=True)
    await runner_mod.run_all(_make_ctx())  # should hit cache
    assert call_count == 1

    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.extend(original)
    runner_mod._cache = None


@pytest.mark.anyio
async def test_cache_bypass_with_fresh():
    import gateway.readiness.runner as runner_mod
    import gateway.readiness.registry as reg_mod

    call_count = 0

    class _CountingCheck:
        id = "T-FRESH"
        name = "fresh"
        from gateway.readiness.protocol import Category, Severity
        category = Category.security
        severity = Severity.warn

        async def run(self, ctx):
            nonlocal call_count
            call_count += 1
            from gateway.readiness.protocol import CheckResult
            return CheckResult(status="green", detail="ok")

    original = reg_mod._REGISTERED[:]
    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.append(_CountingCheck())
    runner_mod._cache = None
    runner_mod._cache_lock_obj = None

    await runner_mod.run_all(_make_ctx(), fresh=True)
    await runner_mod.run_all(_make_ctx(), fresh=True)
    assert call_count == 2

    reg_mod._REGISTERED.clear()
    reg_mod._REGISTERED.extend(original)
    runner_mod._cache = None


# ─── Rollup rules ─────────────────────────────────────────────────────────────

def test_rollup_unready():
    from gateway.readiness.runner import _rollup
    checks = [
        {"status": "red", "severity": "sec"},
        {"status": "green", "severity": "ops"},
    ]
    assert _rollup(checks) == "unready"


def test_rollup_degraded():
    from gateway.readiness.runner import _rollup
    checks = [
        {"status": "red", "severity": "ops"},
        {"status": "green", "severity": "sec"},
    ]
    assert _rollup(checks) == "degraded"


def test_rollup_ready():
    from gateway.readiness.runner import _rollup
    checks = [
        {"status": "green", "severity": "sec"},
        {"status": "green", "severity": "ops"},
    ]
    assert _rollup(checks) == "ready"


def test_rollup_ready_with_only_warn_amber():
    """Per §4.5: 'only warn amber' checks keep rollup=ready."""
    from gateway.readiness.runner import _rollup
    checks = [
        {"status": "green", "severity": "sec"},
        {"status": "amber", "severity": "warn"},  # a warn-severity amber alone
        {"status": "green", "severity": "int"},
    ]
    assert _rollup(checks) == "ready"


def test_rollup_degraded_with_ops_amber():
    """An ops-severity amber (non-warn) must demote to degraded."""
    from gateway.readiness.runner import _rollup
    checks = [
        {"status": "green", "severity": "sec"},
        {"status": "amber", "severity": "ops"},
    ]
    assert _rollup(checks) == "degraded"


def test_rollup_degraded_with_warn_red():
    """Even though warn ambers are tolerated, a warn-severity RED must surface."""
    from gateway.readiness.runner import _rollup
    checks = [
        {"status": "red", "severity": "warn"},
    ]
    assert _rollup(checks) == "degraded"
