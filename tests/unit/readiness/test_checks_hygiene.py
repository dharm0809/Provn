"""Hygiene batch: HYG-01…HYG-03 green+red path tests."""

from __future__ import annotations

import asyncio
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    return types.SimpleNamespace()


def _settings(**kw):
    defaults = dict(log_level="INFO", gateway_host="127.0.0.1", rate_limit_enabled=True)
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


# ─── HYG-01 ───────────────────────────────────────────────────────────────────

def test_hyg01_green_info(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.hygiene.get_settings", lambda: _settings())
    from gateway.readiness.checks.hygiene import _Hyg01LogLevel
    assert _run(_Hyg01LogLevel().run(_ctx())).status == "green"


def test_hyg01_amber_debug_on_prod(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.hygiene.get_settings",
        lambda: _settings(log_level="DEBUG", gateway_host="10.0.1.5"),
    )
    from gateway.readiness.checks.hygiene import _Hyg01LogLevel
    assert _run(_Hyg01LogLevel().run(_ctx())).status == "amber"


# ─── HYG-02 ───────────────────────────────────────────────────────────────────

def test_hyg02_green(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.hygiene.get_settings", lambda: _settings())
    from gateway.readiness.checks.hygiene import _Hyg02RateLimiting
    assert _run(_Hyg02RateLimiting().run(_ctx())).status == "green"


def test_hyg02_amber(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.hygiene.get_settings",
        lambda: _settings(rate_limit_enabled=False),
    )
    from gateway.readiness.checks.hygiene import _Hyg02RateLimiting
    assert _run(_Hyg02RateLimiting().run(_ctx())).status == "amber"


# ─── HYG-03 ───────────────────────────────────────────────────────────────────

def test_hyg03_green_not_colocated(monkeypatch):
    """On dev machines the OpenWebUI volume won't exist → green (not applicable)."""
    import unittest.mock as mock
    with mock.patch("gateway.readiness.checks.hygiene.Path.exists", return_value=False):
        from gateway.readiness.checks.hygiene import _Hyg03OpenWebUISecretPersistence
        assert _run(_Hyg03OpenWebUISecretPersistence().run(_ctx())).status == "green"


def test_hyg03_green_with_persisted_key(tmp_path, monkeypatch):
    """Simulate co-located OpenWebUI with a persisted secret key file."""
    key_file = tmp_path / ".webui_secret_key"
    key_file.write_text("a" * 64)  # plausible size

    monkeypatch.setattr(
        "gateway.readiness.checks.hygiene._OPENWEBUI_VOLUME_PATHS",
        (str(key_file),),
    )
    from gateway.readiness.checks.hygiene import _Hyg03OpenWebUISecretPersistence
    r = _run(_Hyg03OpenWebUISecretPersistence().run(_ctx()))
    assert r.status == "green"


def test_hyg03_amber_tiny_key(tmp_path, monkeypatch):
    """Suspiciously small key → amber."""
    key_file = tmp_path / ".webui_secret_key"
    key_file.write_text("x")

    monkeypatch.setattr(
        "gateway.readiness.checks.hygiene._OPENWEBUI_VOLUME_PATHS",
        (str(key_file),),
    )
    from gateway.readiness.checks.hygiene import _Hyg03OpenWebUISecretPersistence
    r = _run(_Hyg03OpenWebUISecretPersistence().run(_ctx()))
    assert r.status == "amber"
