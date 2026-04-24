"""Unit tests for individual readiness checks (green-path + red-path per check)."""

from __future__ import annotations

import asyncio
import types

import pytest


def _make_ctx(**kwargs):
    defaults = dict(wal_writer=None, walacor_client=None)
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _run(coro):
    return asyncio.run(coro)


# ─── SEC-01 ───────────────────────────────────────────────────────────────────

def test_sec01_green(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: types.SimpleNamespace(api_keys_list=["real-key-abc123"]),
    )
    from gateway.readiness.checks.security import _Sec01ApiKeyEnforced
    result = _run(_Sec01ApiKeyEnforced().run(_make_ctx()))
    assert result.status == "green"


def test_sec01_amber_no_keys(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: types.SimpleNamespace(api_keys_list=[]),
    )
    from gateway.readiness.checks.security import _Sec01ApiKeyEnforced
    result = _run(_Sec01ApiKeyEnforced().run(_make_ctx()))
    assert result.status == "amber"
    assert "No API keys" in result.detail


def test_sec01_amber_auto_generated(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: types.SimpleNamespace(api_keys_list=["wgk-abc123"], wal_path=str(tmp_path)),
    )
    from gateway.readiness.checks.security import _Sec01ApiKeyEnforced
    result = _run(_Sec01ApiKeyEnforced().run(_make_ctx()))
    assert result.status == "amber"
    # Phase 5: SEC-01 reports bootstrap_key_stable in evidence.
    assert "bootstrap_key_stable" in result.evidence


def test_sec01_amber_auto_generated_key_stable(monkeypatch, tmp_path):
    """When a persisted bootstrap key file exists, SEC-01 reports bootstrap_key_stable=true."""
    from gateway.auth.bootstrap_key import ensure_bootstrap_key
    key, _ = ensure_bootstrap_key(str(tmp_path))
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: types.SimpleNamespace(api_keys_list=[key], wal_path=str(tmp_path)),
    )
    from gateway.readiness.checks.security import _Sec01ApiKeyEnforced
    result = _run(_Sec01ApiKeyEnforced().run(_make_ctx()))
    assert result.status == "amber"
    assert result.evidence["bootstrap_key_stable"] is True
    assert "recommend moving to a secret store" in result.detail


# ─── SEC-02 ───────────────────────────────────────────────────────────────────

def test_sec02_green_no_keys(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: types.SimpleNamespace(api_keys_list=[], lineage_auth_required=True),
    )
    from gateway.readiness.checks.security import _Sec02LineageAuthActive
    result = _run(_Sec02LineageAuthActive().run(_make_ctx()))
    assert result.status == "green"
    assert "not applicable" in result.detail


def test_sec02_red_auth_disabled(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: types.SimpleNamespace(
            api_keys_list=["key1"], lineage_auth_required=False
        ),
    )
    from gateway.readiness.checks.security import _Sec02LineageAuthActive
    result = _run(_Sec02LineageAuthActive().run(_make_ctx()))
    assert result.status == "red"
    assert "lineage_auth_required" in result.detail


def test_sec02_green_probe_returns_401(monkeypatch):
    """When keys are set and lineage_auth_required=True, a probe that returns 401 is green."""
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: types.SimpleNamespace(
            api_keys_list=["key1"], lineage_auth_required=True
        ),
    )

    class _FakeResp:
        status_code = 401

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        def get(self, url):
            return _FakeResp()

    import unittest.mock as mock
    with mock.patch("gateway.readiness.checks.security.TestClient", _FakeClient, create=True):
        from gateway.readiness.checks.security import _Sec02LineageAuthActive
        result = _run(_Sec02LineageAuthActive().run(_make_ctx()))
    assert result.status == "green"


# ─── INT-01 ───────────────────────────────────────────────────────────────────

def test_int01_green(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.integrity.signing_key_available", lambda: True
    )
    from gateway.readiness.checks.integrity import _Int01SigningKeyLoaded
    result = _run(_Int01SigningKeyLoaded().run(_make_ctx()))
    assert result.status == "green"


def test_int01_red(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.integrity.signing_key_available", lambda: False
    )
    from gateway.readiness.checks.integrity import _Int01SigningKeyLoaded
    result = _run(_Int01SigningKeyLoaded().run(_make_ctx()))
    assert result.status == "red"


# ─── INT-02 ───────────────────────────────────────────────────────────────────

def test_int02_amber_no_writer():
    from gateway.readiness.checks.integrity import _Int02SigningActive
    result = _run(_Int02SigningActive().run(_make_ctx()))
    assert result.status == "amber"


def test_int02_green(tmp_path):
    import json
    import sqlite3

    db = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE wal_records (execution_id TEXT, record_json TEXT, event_type TEXT, request_type TEXT)")
    for i in range(50):
        conn.execute(
            "INSERT INTO wal_records VALUES (?, ?, 'execution', NULL)",
            (str(i), json.dumps({"record_signature": f"sig{i}"})),
        )
    conn.commit()
    conn.close()

    from gateway.readiness.checks.integrity import _Int02SigningActive
    result = _run(_Int02SigningActive().run(_make_ctx(wal_writer=types.SimpleNamespace(_path=str(db)))))
    assert result.status == "green"
    assert result.evidence["signed"] == 50


def test_int02_red_unsigned(tmp_path):
    import json
    import sqlite3

    db = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE wal_records (execution_id TEXT, record_json TEXT, event_type TEXT, request_type TEXT)")
    for i in range(50):
        conn.execute(
            "INSERT INTO wal_records VALUES (?, ?, 'execution', NULL)",
            (str(i), json.dumps({"record_signature": None})),
        )
    conn.commit()
    conn.close()

    from gateway.readiness.checks.integrity import _Int02SigningActive
    result = _run(_Int02SigningActive().run(_make_ctx(wal_writer=types.SimpleNamespace(_path=str(db)))))
    assert result.status == "red"
    assert result.evidence["signed"] == 0


# ─── PER-01 ───────────────────────────────────────────────────────────────────

def test_per01_green(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.persistence.get_settings",
        lambda: types.SimpleNamespace(wal_path=str(tmp_path)),
    )
    from gateway.readiness.checks.persistence import _Per01WalWritable
    result = _run(_Per01WalWritable().run(_make_ctx()))
    assert result.status == "green"


def test_per01_red(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.persistence.get_settings",
        lambda: types.SimpleNamespace(wal_path="/nonexistent/xyz"),
    )
    import unittest.mock as mock
    with mock.patch(
        "gateway.readiness.checks.persistence.Path.mkdir",
        side_effect=PermissionError("denied"),
    ):
        from gateway.readiness.checks.persistence import _Per01WalWritable
        result = _run(_Per01WalWritable().run(_make_ctx()))
    assert result.status == "red"


# ─── DEP-01 ───────────────────────────────────────────────────────────────────

def test_dep01_amber_no_walacor():
    from gateway.readiness.checks.dependencies import _Dep01WalacorAuth
    result = _run(_Dep01WalacorAuth().run(_make_ctx()))
    assert result.status == "amber"
    assert "not configured" in result.detail


def test_dep01_green():
    async def _fake_start():
        pass

    ctx = _make_ctx(walacor_client=types.SimpleNamespace(start=_fake_start))
    from gateway.readiness.checks.dependencies import _Dep01WalacorAuth
    result = _run(_Dep01WalacorAuth().run(ctx))
    assert result.status == "green"


def test_dep01_red_auth_failure():
    async def _bad_start():
        raise ConnectionError("refused")

    ctx = _make_ctx(walacor_client=types.SimpleNamespace(start=_bad_start))
    from gateway.readiness.checks.dependencies import _Dep01WalacorAuth
    result = _run(_Dep01WalacorAuth().run(ctx))
    assert result.status == "red"
    assert "refused" in result.detail
