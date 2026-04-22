"""Feature-coherence batch: FEA-01…FEA-07 green+red path tests."""

from __future__ import annotations

import asyncio
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


def _ctx(**kw):
    d = dict(wal_writer=None, walacor_client=None, tool_registry=None,
             model_registry=None, intelligence_db=None)
    d.update(kw)
    return types.SimpleNamespace(**d)


def _settings(**kw):
    defaults = dict(
        llama_guard_enabled=False, llama_guard_ollama_url="", provider_ollama_url="",
        web_search_enabled=False, tool_aware_enabled=True,
        presidio_pii_enabled=False, prompt_guard_enabled=False,
        otel_enabled=False, otel_endpoint="",
        uvicorn_workers=1, redis_url="",
        intelligence_enabled=False,
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


# ─── FEA-01 ───────────────────────────────────────────────────────────────────

def test_fea01_green_disabled(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.features.get_settings", lambda: _settings())
    from gateway.readiness.checks.features import _Fea01LlamaGuard
    assert _run(_Fea01LlamaGuard().run(_ctx())).status == "green"


def test_fea01_red_ollama_unreachable(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(llama_guard_enabled=True,
                          llama_guard_ollama_url="http://nonexistent.invalid"),
    )

    async def _fake_ping(url, timeout=3.0):
        return False
    monkeypatch.setattr("gateway.readiness.checks.features._ping_ollama", _fake_ping)
    from gateway.readiness.checks.features import _Fea01LlamaGuard
    assert _run(_Fea01LlamaGuard().run(_ctx())).status == "red"


# ─── FEA-02 ───────────────────────────────────────────────────────────────────

def test_fea02_green_disabled(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.features.get_settings", lambda: _settings())
    from gateway.readiness.checks.features import _Fea02WebSearch
    assert _run(_Fea02WebSearch().run(_ctx())).status == "green"


def test_fea02_red_missing_tool(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(web_search_enabled=True, tool_aware_enabled=True),
    )
    from gateway.readiness.checks.features import _Fea02WebSearch
    # tool_registry is None → missing tool
    assert _run(_Fea02WebSearch().run(_ctx())).status == "red"


def test_fea02_green_properly_wired(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(web_search_enabled=True, tool_aware_enabled=True),
    )
    registry = types.SimpleNamespace(get_tool_schema=lambda name: {"type": "object"} if name == "web_search" else None)
    from gateway.readiness.checks.features import _Fea02WebSearch
    assert _run(_Fea02WebSearch().run(_ctx(tool_registry=registry))).status == "green"


# ─── FEA-03 ───────────────────────────────────────────────────────────────────

def test_fea03_green_disabled(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.features.get_settings", lambda: _settings())
    from gateway.readiness.checks.features import _Fea03Presidio
    assert _run(_Fea03Presidio().run(_ctx())).status == "green"


def test_fea03_red_import_fails(monkeypatch):
    """Force ImportError deterministically to prove the red path is reachable."""
    import builtins
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(presidio_pii_enabled=True),
    )
    real_import = builtins.__import__

    def _blocking_import(name, *a, **kw):
        if name == "presidio_analyzer":
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    from gateway.readiness.checks.features import _Fea03Presidio
    r = _run(_Fea03Presidio().run(_ctx()))
    assert r.status == "red"
    assert "blocked for test" in r.detail


# ─── FEA-04 ───────────────────────────────────────────────────────────────────

def test_fea04_green_disabled(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.features.get_settings", lambda: _settings())
    from gateway.readiness.checks.features import _Fea04PromptGuard
    assert _run(_Fea04PromptGuard().run(_ctx())).status == "green"


def test_fea04_red_import_fails(monkeypatch):
    """Force ImportError deterministically."""
    import builtins
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(prompt_guard_enabled=True),
    )
    real_import = builtins.__import__

    def _blocking_import(name, *a, **kw):
        if name == "transformers":
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    from gateway.readiness.checks.features import _Fea04PromptGuard
    r = _run(_Fea04PromptGuard().run(_ctx()))
    assert r.status == "red"
    assert "blocked for test" in r.detail


# ─── FEA-05 ───────────────────────────────────────────────────────────────────

def test_fea05_green_disabled(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.features.get_settings", lambda: _settings())
    from gateway.readiness.checks.features import _Fea05OTel
    assert _run(_Fea05OTel().run(_ctx())).status == "green"


def test_fea05_red_missing_endpoint(monkeypatch):
    """When OTel is enabled but endpoint is empty, returns red."""
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(otel_enabled=True, otel_endpoint=""),
    )
    from gateway.readiness.checks.features import _Fea05OTel
    r = _run(_Fea05OTel().run(_ctx()))
    assert r.status == "red"


# ─── FEA-06 ───────────────────────────────────────────────────────────────────

def test_fea06_green(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.features.get_settings", lambda: _settings())
    from gateway.readiness.checks.features import _Fea06WorkerRedis
    assert _run(_Fea06WorkerRedis().run(_ctx())).status == "green"


def test_fea06_red_workers_no_redis(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(uvicorn_workers=4, redis_url=""),
    )
    from gateway.readiness.checks.features import _Fea06WorkerRedis
    assert _run(_Fea06WorkerRedis().run(_ctx())).status == "red"


# ─── FEA-07 ───────────────────────────────────────────────────────────────────

def test_fea07_green_disabled(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.features.get_settings", lambda: _settings())
    from gateway.readiness.checks.features import _Fea07Intelligence
    assert _run(_Fea07Intelligence().run(_ctx())).status == "green"


def test_fea07_red_enabled_without_deps(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(intelligence_enabled=True),
    )
    from gateway.readiness.checks.features import _Fea07Intelligence
    assert _run(_Fea07Intelligence().run(_ctx())).status == "red"


def test_fea07_green_enabled_with_deps(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.features.get_settings",
        lambda: _settings(intelligence_enabled=True),
    )
    from gateway.readiness.checks.features import _Fea07Intelligence
    assert _run(_Fea07Intelligence().run(
        _ctx(model_registry=object(), intelligence_db=object())
    )).status == "green"
