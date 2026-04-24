"""Unit tests for DefaultResourceMonitor.snapshot()."""
from __future__ import annotations

import time

from gateway.adaptive.resource_monitor import DefaultResourceMonitor


def _make_monitor() -> DefaultResourceMonitor:
    # wal_path only used by check(); snapshot() doesn't care.
    return DefaultResourceMonitor(wal_path="/tmp")


def test_snapshot_empty():
    m = _make_monitor()
    snap = m.snapshot()
    assert snap == {"providers": {}}


def test_snapshot_records_provider_state():
    m = _make_monitor()
    m.record_provider_result("ollama", success=True)
    m.record_provider_result("ollama", success=False, error="HTTP 503")
    snap = m.snapshot()
    assert "ollama" in snap["providers"]
    p = snap["providers"]["ollama"]
    assert set(p.keys()) == {"error_rate_60s", "cooldown_until", "last_error"}
    assert p["last_error"] == "HTTP 503"


def test_snapshot_cooldown_until_iso8601_when_cooling_down():
    m = _make_monitor()
    # Force cooldown: >50% failures with at least min_samples in 60s window.
    for _ in range(5):
        m.record_provider_result("ollama", success=False, error="boom")
    snap = m.snapshot()
    p = snap["providers"]["ollama"]
    assert p["cooldown_until"] is not None
    # iso8601_utc emits UTC offset suffix.
    assert p["cooldown_until"].endswith("+00:00")
    assert p["last_error"] == "boom"


def test_snapshot_cooldown_until_none_when_healthy():
    m = _make_monitor()
    for _ in range(5):
        m.record_provider_result("ollama", success=True)
    snap = m.snapshot()
    p = snap["providers"]["ollama"]
    assert p["cooldown_until"] is None
    assert p["last_error"] is None


def test_snapshot_multi_provider():
    m = _make_monitor()
    m.record_provider_result("ollama", success=True)
    m.record_provider_result("openai", success=False, error="429")
    snap = m.snapshot()
    assert set(snap["providers"].keys()) == {"ollama", "openai"}
    assert snap["providers"]["openai"]["last_error"] == "429"
    assert snap["providers"]["ollama"]["last_error"] is None


def test_record_provider_result_without_error_kwarg():
    # Must work without the optional error kwarg (keyword-only, default None).
    m = _make_monitor()
    m.record_provider_result("ollama", success=True)
    m.record_provider_result("ollama", success=False)
    snap = m.snapshot()
    assert snap["providers"]["ollama"]["last_error"] is None
