"""Integration test: spin up a deliberately broken gateway and assert the expected red/amber items.

This test would have caught all four past incidents. It must remain a permanent part of the suite.

Broken config used:
  - No API keys (SEC-01 amber)
  - lineage_auth_required=False, but no keys → SEC-02 green (no keys = not applicable)
  - Signing key NOT loaded (INT-01 red)
  - No WAL records → INT-02 amber (no data)
  - WAL directory exists and writable → PER-01 green
  - No walacor client → DEP-01 amber
"""

from __future__ import annotations

import asyncio
import os
import types

import pytest


anyio_backend = pytest.fixture(params=["asyncio"])(lambda request: request.param)


@pytest.mark.anyio
async def test_broken_config_produces_expected_check_statuses(tmp_path, monkeypatch):
    """Deliberately broken gateway surfaces expected red/amber items."""
    # Signing key: not available
    monkeypatch.setattr(
        "gateway.readiness.checks.integrity.signing_key_available", lambda: False
    )

    # WAL writer with empty DB
    import sqlite3
    import json

    db = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE wal_records (execution_id TEXT, record_json TEXT, event_type TEXT)"
    )
    conn.commit()
    conn.close()
    wal_writer = types.SimpleNamespace(_path=str(db))

    # No API keys configured
    monkeypatch.setattr(
        "gateway.readiness.checks.security.get_settings",
        lambda: types.SimpleNamespace(api_keys_list=[], lineage_auth_required=False),
    )

    # WAL path = tmp_path (writable)
    monkeypatch.setattr(
        "gateway.readiness.checks.persistence.get_settings",
        lambda: types.SimpleNamespace(wal_path=str(tmp_path)),
    )

    ctx = types.SimpleNamespace(wal_writer=wal_writer, walacor_client=None)

    # Run individual checks directly
    from gateway.readiness.checks.security import _Sec01ApiKeyEnforced, _Sec02LineageAuthActive
    from gateway.readiness.checks.integrity import _Int01SigningKeyLoaded, _Int02SigningActive
    from gateway.readiness.checks.persistence import _Per01WalWritable
    from gateway.readiness.checks.dependencies import _Dep01WalacorAuth

    sec01 = await _Sec01ApiKeyEnforced().run(ctx)
    sec02 = await _Sec02LineageAuthActive().run(ctx)
    int01 = await _Int01SigningKeyLoaded().run(ctx)
    int02 = await _Int02SigningActive().run(ctx)
    per01 = await _Per01WalWritable().run(ctx)
    dep01 = await _Dep01WalacorAuth().run(ctx)

    # SEC-01: amber (no keys)
    assert sec01.status == "amber", f"SEC-01: {sec01.status} — {sec01.detail}"
    # SEC-02: green (no keys → not applicable)
    assert sec02.status == "green", f"SEC-02: {sec02.status} — {sec02.detail}"
    # INT-01: red (key not loaded)
    assert int01.status == "red", f"INT-01: {int01.status} — {int01.detail}"
    # INT-02: amber (no WAL records yet)
    assert int02.status == "amber", f"INT-02: {int02.status} — {int02.detail}"
    # PER-01: green (tmp_path writable)
    assert per01.status == "green", f"PER-01: {per01.status} — {per01.detail}"
    # DEP-01: amber (no walacor client)
    assert dep01.status == "amber", f"DEP-01: {dep01.status} — {dep01.detail}"
