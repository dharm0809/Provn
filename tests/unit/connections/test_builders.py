"""Per-tile builder tests: empty-state + one threshold flip per tile."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pytest

from gateway.connections import builder as B
from gateway.util.time import iso8601_utc


anyio_backend = pytest.fixture(params=["asyncio"])(lambda request: request.param)


@dataclass
class FakeCtx:
    resource_monitor: Any = None
    walacor_client: Any = None
    wal_writer: Any = None
    content_analyzers: list = field(default_factory=list)
    capability_registry: Any = None
    sync_client: Any = None
    control_store: Any = None
    policy_cache: Any = None
    attestation_cache: Any = None
    local_sync_task: Any = None
    sync_loop_task: Any = None
    intelligence_worker: Any = None
    intelligence_db: Any = None
    lineage_reader: Any = None


# ── providers ─────────────────────────────────────────────────────────


class _RM:
    def __init__(self, providers):
        self._providers = providers

    def snapshot(self):
        return {"providers": self._providers}


def test_providers_empty_disabled():
    tile = B.build_providers_tile(FakeCtx())
    assert tile["status"] == "unknown"
    assert tile["id"] == "providers"


def test_providers_red_on_cooldown():
    rm = _RM({"ollama": {"error_rate_60s": 0.5, "cooldown_until": iso8601_utc(time.time() + 30), "last_error": "boom"}})
    tile = B.build_providers_tile(FakeCtx(resource_monitor=rm))
    assert tile["status"] == "red"


def test_providers_amber_on_high_rate():
    rm = _RM({"p": {"error_rate_60s": 0.25, "cooldown_until": None, "last_error": None}})
    tile = B.build_providers_tile(FakeCtx(resource_monitor=rm))
    assert tile["status"] == "amber"


# ── walacor_delivery ─────────────────────────────────────────────────


class _WC:
    def __init__(self, snap, log=None):
        self._snap = snap
        self._delivery_log = log or deque()

    def delivery_snapshot(self):
        return self._snap


class _WAL:
    def __init__(self, pending=0):
        self._pending = pending

    def pending_count(self):
        return self._pending


def test_walacor_empty_disabled():
    tile = B.build_walacor_delivery_tile(FakeCtx())
    assert tile["status"] == "unknown"


def test_walacor_red_on_low_success():
    snap = {
        "success_rate_60s": 0.2,
        "last_failure": {"ts": iso8601_utc(time.time()), "op": "write", "detail": "500"},
        "last_success_ts": None,
        "time_since_last_success_s": None,
    }
    tile = B.build_walacor_delivery_tile(FakeCtx(walacor_client=_WC(snap), wal_writer=_WAL(3)))
    assert tile["status"] == "red"
    assert tile["detail"]["pending_writes"] == 3


def test_walacor_amber_on_partial_failures():
    snap = {
        "success_rate_60s": 0.8,
        "last_failure": {"ts": iso8601_utc(time.time()), "op": "write", "detail": "slow"},
        "last_success_ts": iso8601_utc(time.time()),
        "time_since_last_success_s": 1.0,
    }
    tile = B.build_walacor_delivery_tile(FakeCtx(walacor_client=_WC(snap)))
    assert tile["status"] == "amber"


# ── analyzers ────────────────────────────────────────────────────────


class _Analyzer:
    def __init__(self, fail_opens=0, enabled=True, aid="fake"):
        self.analyzer_id = aid
        self.enabled = enabled
        self._count = fail_opens
        now = time.time()
        self._fail_open_log = deque((now, "reason") for _ in range(fail_opens))

    def fail_open_snapshot(self):
        last = self._fail_open_log[-1] if self._fail_open_log else None
        return {
            "fail_opens_60s": self._count,
            "last_fail_open": (
                {"ts": iso8601_utc(last[0]), "reason": last[1]} if last else None
            ),
        }


def test_analyzers_empty_green():
    tile = B.build_analyzers_tile(FakeCtx(content_analyzers=[_Analyzer(0)]))
    assert tile["status"] == "green"


def test_analyzers_red_on_many_fail_opens():
    tile = B.build_analyzers_tile(FakeCtx(content_analyzers=[_Analyzer(5)]))
    assert tile["status"] == "red"


def test_analyzers_amber_on_single_fail_open():
    tile = B.build_analyzers_tile(FakeCtx(content_analyzers=[_Analyzer(1)]))
    assert tile["status"] == "amber"


# ── tool_loop ─────────────────────────────────────────────────────────


def _clear_tool_log():
    from gateway.pipeline.tool_executor import _tool_exception_log
    _tool_exception_log.clear()


def test_tool_loop_empty_green():
    _clear_tool_log()
    tile = B.build_tool_loop_tile(FakeCtx())
    assert tile["status"] == "green"


def test_tool_loop_amber_on_recent_exception():
    _clear_tool_log()
    from gateway.pipeline.tool_executor import record_tool_exception
    record_tool_exception(tool="web_search", error="timeout")
    tile = B.build_tool_loop_tile(FakeCtx())
    assert tile["status"] == "amber"
    _clear_tool_log()


# ── model_capabilities ───────────────────────────────────────────────


class _CapReg:
    def __init__(self, caps):
        self._caps = caps

    def all_capabilities(self):
        return self._caps


def test_model_capabilities_empty_green():
    tile = B.build_model_capabilities_tile(FakeCtx(capability_registry=_CapReg({})))
    assert tile["status"] == "green"


def test_model_capabilities_amber_on_auto_disabled():
    tile = B.build_model_capabilities_tile(
        FakeCtx(capability_registry=_CapReg({
            "m1": {"supports_tools": False, "auto_disabled": True, "since": None},
        }))
    )
    assert tile["status"] == "amber"
    assert tile["detail"]["auto_disabled_count"] == 1


# ── control_plane ─────────────────────────────────────────────────────


from datetime import datetime, timezone


class _PC:
    def __init__(self, version=1, last_sync=None, stale=False, policies=None):
        self.version = version
        self.last_sync = last_sync
        self.is_stale = stale
        self._pols = policies or []

    def get_policies(self):
        return self._pols


class _FakeTask:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


def test_control_plane_empty_disabled_green():
    # no sync_client, no control_store, no policy_cache → mode=disabled, green
    tile = B.build_control_plane_tile(FakeCtx())
    assert tile["status"] == "green"
    assert tile["detail"]["mode"] == "disabled"


def test_control_plane_red_on_dead_sync_task():
    pc = _PC(last_sync=datetime.now(timezone.utc), stale=True)
    tile = B.build_control_plane_tile(
        FakeCtx(control_store=object(), policy_cache=pc, local_sync_task=_FakeTask(done=True))
    )
    assert tile["status"] == "red"


# ── auth ──────────────────────────────────────────────────────────────


def _fake_settings(monkeypatch, *, api_keys=None, control_plane=True):
    """Install a dummy Settings object for the auth-tile test matrix."""
    class _S:
        api_keys_list = list(api_keys or [])
        control_plane_enabled = control_plane
        wal_path = "/tmp/wal-tests"
        auth_mode = "api_key"
        jwt_secret = ""
        jwt_jwks_url = ""
    monkeypatch.setattr("gateway.config.get_settings", lambda: _S())


def test_auth_empty_green(monkeypatch):
    """No admin keys + persisted bootstrap key → green."""
    _fake_settings(monkeypatch)
    monkeypatch.setattr(
        "gateway.auth.bootstrap_key.bootstrap_key_stable", lambda _p: True
    )
    tile = B.build_auth_tile(FakeCtx())
    assert tile["status"] == "green"


def test_auth_amber_on_unstable_bootstrap(monkeypatch):
    """No admin keys + bootstrap persistence broken → amber with clear subline."""
    _fake_settings(monkeypatch)
    monkeypatch.setattr(
        "gateway.auth.bootstrap_key.bootstrap_key_stable", lambda _p: False
    )
    tile = B.build_auth_tile(FakeCtx())
    assert tile["status"] == "amber"
    assert "persistence failed" in tile["subline"]


def test_auth_green_when_admin_keys_configured(monkeypatch):
    """Admin keys configured → bootstrap key is not applicable → green.

    Previously this tile went amber forever on any deployment that set
    WALACOR_GATEWAY_API_KEYS because the bootstrap-key file is intentionally
    never written in that mode.
    """
    _fake_settings(monkeypatch, api_keys=["dharm-key-2026"])
    tile = B.build_auth_tile(FakeCtx())
    assert tile["status"] == "green"
    assert tile["detail"]["bootstrap_key_stable"] is None
    assert tile["detail"]["admin_api_keys_configured"] is True


# ── readiness ─────────────────────────────────────────────────────────


class _ReadinessReport:
    def __init__(self, status, checks):
        self.status = status
        self.checks = checks
        self.generated_at = "2026-04-23T00:00:00Z"


@pytest.mark.anyio
async def test_readiness_green(monkeypatch, anyio_backend):
    async def fake_run_all(ctx):
        return _ReadinessReport("ready", [])
    monkeypatch.setattr("gateway.readiness.runner.run_all", fake_run_all)
    tile = await B.build_readiness_tile(FakeCtx())
    assert tile["status"] == "green"


@pytest.mark.anyio
async def test_readiness_red_on_unready(monkeypatch, anyio_backend):
    async def fake_run_all(ctx):
        return _ReadinessReport(
            "unready",
            [{"id": "SEC-01", "status": "red", "detail": "bad"}],
        )
    monkeypatch.setattr("gateway.readiness.runner.run_all", fake_run_all)
    tile = await B.build_readiness_tile(FakeCtx())
    assert tile["status"] == "red"
    assert tile["detail"]["reds"] == [{"check_id": "SEC-01", "detail": "bad"}]


# ── streaming ─────────────────────────────────────────────────────────


def _clear_stream_log():
    from gateway.pipeline.forwarder import _stream_interruption_log
    _stream_interruption_log.clear()


def test_streaming_empty_green():
    _clear_stream_log()
    tile = B.build_streaming_tile(FakeCtx())
    assert tile["status"] == "green"


def test_streaming_amber_on_recent_interruption():
    _clear_stream_log()
    from gateway.pipeline.forwarder import record_stream_interruption
    record_stream_interruption(provider="ollama", detail="closed")
    tile = B.build_streaming_tile(FakeCtx())
    assert tile["status"] == "amber"
    _clear_stream_log()


# ── intelligence_worker ───────────────────────────────────────────────


class _Worker:
    def __init__(self, running=True, queue_depth=0, oldest=0.0, last_error=None):
        self._running = running
        self._qd = queue_depth
        self._oldest = oldest
        self._last_error = last_error  # (ts, detail) tuple or None

    def snapshot(self):
        last = None
        if self._last_error is not None:
            ts, detail = self._last_error
            last = {"ts": iso8601_utc(ts), "detail": detail}
        return {
            "running": self._running,
            "queue_depth": self._qd,
            "oldest_job_age_s": self._oldest,
            "last_error": last,
        }


@pytest.mark.anyio
async def test_intelligence_worker_empty_disabled(anyio_backend):
    tile = await B.build_intelligence_worker_tile(FakeCtx())
    assert tile["status"] == "unknown"


@pytest.mark.anyio
async def test_intelligence_worker_red_when_not_running(anyio_backend):
    tile = await B.build_intelligence_worker_tile(FakeCtx(intelligence_worker=_Worker(running=False)))
    assert tile["status"] == "red"


@pytest.mark.anyio
async def test_intelligence_worker_amber_on_queue_backup(anyio_backend):
    tile = await B.build_intelligence_worker_tile(FakeCtx(intelligence_worker=_Worker(queue_depth=150)))
    assert tile["status"] == "amber"


# ── rollup ────────────────────────────────────────────────────────────


def test_rollup_red_dominates():
    tiles = [{"status": "green"}, {"status": "amber"}, {"status": "red"}]
    assert B.compute_rollup(tiles) == "red"


def test_rollup_amber_over_green():
    tiles = [{"status": "green"}, {"status": "amber"}, {"status": "unknown"}]
    assert B.compute_rollup(tiles) == "amber"


def test_rollup_unknown_does_not_contribute():
    tiles = [{"status": "green"}, {"status": "unknown"}]
    assert B.compute_rollup(tiles) == "green"
