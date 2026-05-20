"""Dependency batch: DEP-02…DEP-05 green+red path tests."""

from __future__ import annotations

import asyncio
import types

import pytest


def _run(coro):
    return asyncio.run(coro)


def _ctx(**kw):
    d = dict(wal_writer=None, walacor_client=None, redis_client=None)
    d.update(kw)
    return types.SimpleNamespace(**d)


def _settings(*, fields_set: set[str] | None = None, **kw):
    defaults = dict(
        walacor_executions_etid=123,
        llama_guard_enabled=False,
        provider_ollama_url="",
        llama_guard_ollama_url="",
        redis_url="",
        openai_api_key="",
        anthropic_api_key="",
    )
    defaults.update(kw)
    s = types.SimpleNamespace(**defaults)
    # Mimic pydantic v2's `model_fields_set` — the set of fields explicitly
    # provided via env/config (as opposed to defaulting). DEP-03 reads this
    # to distinguish "operator opted in" from "default value present."
    # Tests that don't pass `fields_set` get the empty set (= pure defaults),
    # which is the right shape for the "not configured" branch.
    s.model_fields_set = fields_set or set()
    return s


# ─── DEP-02 ───────────────────────────────────────────────────────────────────

def test_dep02_green(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.dependencies.get_settings", lambda: _settings())

    async def _query(etid, pipe):
        return [{"execution_id": "x"}]
    client = types.SimpleNamespace(query_complex=_query)
    from gateway.readiness.checks.dependencies import _Dep02WalacorQuery
    assert _run(_Dep02WalacorQuery().run(_ctx(walacor_client=client))).status == "green"


def test_dep02_red(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.dependencies.get_settings", lambda: _settings())

    async def _query(etid, pipe):
        raise ConnectionError("refused")
    client = types.SimpleNamespace(query_complex=_query)
    from gateway.readiness.checks.dependencies import _Dep02WalacorQuery
    assert _run(_Dep02WalacorQuery().run(_ctx(walacor_client=client))).status == "red"


# ─── DEP-03 ───────────────────────────────────────────────────────────────────

def test_dep03_green_when_operator_did_not_opt_in(monkeypatch):
    """Default config (no env overrides) → check skips with green.

    The pre-fix logic treated the non-empty `provider_ollama_url` default
    `http://localhost:11434` as "operator wants Ollama" and probed it,
    firing red on any deployment that doesn't ship Ollama. The new logic
    keys on whether the operator explicitly opted in via env/config.
    """
    monkeypatch.setattr(
        "gateway.readiness.checks.dependencies.get_settings",
        lambda: _settings(
            llama_guard_enabled=True,
            provider_ollama_url="http://localhost:11434",
            fields_set=set(),  # neither field explicitly set
        ),
    )
    from gateway.readiness.checks.dependencies import _Dep03OllamaReachable
    result = _run(_Dep03OllamaReachable().run(_ctx()))
    assert result.status == "green"
    assert "not configured" in result.detail.lower()


def test_dep03_red_unreachable_when_operator_opted_in(monkeypatch):
    """Operator explicitly opted in → probe runs → red on unreachable."""
    monkeypatch.setattr(
        "gateway.readiness.checks.dependencies.get_settings",
        lambda: _settings(
            provider_ollama_url="http://nonexistent.invalid:11434",
            fields_set={"provider_ollama_url"},
        ),
    )
    from gateway.readiness.checks.dependencies import _Dep03OllamaReachable
    assert _run(_Dep03OllamaReachable().run(_ctx())).status == "red"


def test_dep03_explicit_llama_guard_enable_runs_probe(monkeypatch):
    """Setting llama_guard_enabled via env is also a real opt-in."""
    monkeypatch.setattr(
        "gateway.readiness.checks.dependencies.get_settings",
        lambda: _settings(
            llama_guard_enabled=True,
            provider_ollama_url="http://nonexistent.invalid:11434",
            fields_set={"llama_guard_enabled"},
        ),
    )
    from gateway.readiness.checks.dependencies import _Dep03OllamaReachable
    # Should attempt the probe (and fail since the host is invalid).
    assert _run(_Dep03OllamaReachable().run(_ctx())).status == "red"


# ─── DEP-04 ───────────────────────────────────────────────────────────────────

def test_dep04_green_no_redis(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.dependencies.get_settings", lambda: _settings())
    from gateway.readiness.checks.dependencies import _Dep04RedisReachable
    assert _run(_Dep04RedisReachable().run(_ctx())).status == "green"


def test_dep04_red_ping_fails(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.dependencies.get_settings",
        lambda: _settings(redis_url="redis://localhost:6379"),
    )

    class _R:
        async def ping(self):
            raise ConnectionError("refused")
    from gateway.readiness.checks.dependencies import _Dep04RedisReachable
    assert _run(_Dep04RedisReachable().run(_ctx(redis_client=_R()))).status == "red"


def test_dep04_green_ping_ok(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.dependencies.get_settings",
        lambda: _settings(redis_url="redis://localhost:6379"),
    )

    class _R:
        async def ping(self):
            return True
    from gateway.readiness.checks.dependencies import _Dep04RedisReachable
    assert _run(_Dep04RedisReachable().run(_ctx(redis_client=_R()))).status == "green"


# ─── DEP-05 ───────────────────────────────────────────────────────────────────

def test_dep05_green_no_keys(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.dependencies.get_settings", lambda: _settings())
    from gateway.readiness.checks.dependencies import _Dep05ProviderKeysPresent
    assert _run(_Dep05ProviderKeysPresent().run(_ctx())).status == "green"


def test_dep05_amber_bad_shape(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.dependencies.get_settings",
        lambda: _settings(openai_api_key="not-an-openai-key"),
    )
    from gateway.readiness.checks.dependencies import _Dep05ProviderKeysPresent
    assert _run(_Dep05ProviderKeysPresent().run(_ctx())).status == "amber"
