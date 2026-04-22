"""Persistence batch: PER-02…PER-05 green+red path tests."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat as stat_mod
import types
from pathlib import Path

import pytest


def _run(coro):
    return asyncio.run(coro)


def _ctx(**kw):
    d = dict(wal_writer=None, walacor_client=None)
    d.update(kw)
    return types.SimpleNamespace(**d)


def _settings(**kw):
    defaults = dict(
        wal_path="/tmp/readiness-test-wal",
        wal_high_water_mark=10000,
        disk_min_free_percent=5.0,
        control_plane_enabled=True,
        control_plane_db_path="",
        record_signing_key_path="",
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


# ─── PER-02 ───────────────────────────────────────────────────────────────────

def test_per02_green(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.persistence.get_settings",
                        lambda: _settings(wal_path=str(tmp_path)))
    from gateway.readiness.checks.persistence import _Per02WalDiskHeadroom
    # Real disk with plenty of free space → green
    assert _run(_Per02WalDiskHeadroom().run(_ctx())).status == "green"


def test_per02_red_threshold_high(tmp_path, monkeypatch):
    # Force threshold above free space to trigger red
    monkeypatch.setattr(
        "gateway.readiness.checks.persistence.get_settings",
        lambda: _settings(wal_path=str(tmp_path), disk_min_free_percent=200.0),
    )
    from gateway.readiness.checks.persistence import _Per02WalDiskHeadroom
    assert _run(_Per02WalDiskHeadroom().run(_ctx())).status == "red"


# ─── PER-03 ───────────────────────────────────────────────────────────────────

def test_per03_green(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.persistence.get_settings",
                        lambda: _settings(wal_high_water_mark=100))
    writer = types.SimpleNamespace(pending_count=lambda: 10)
    from gateway.readiness.checks.persistence import _Per03WalBacklog
    assert _run(_Per03WalBacklog().run(_ctx(wal_writer=writer))).status == "green"


def test_per03_red_over_high_water(monkeypatch):
    monkeypatch.setattr("gateway.readiness.checks.persistence.get_settings",
                        lambda: _settings(wal_high_water_mark=100))
    writer = types.SimpleNamespace(pending_count=lambda: 150)
    from gateway.readiness.checks.persistence import _Per03WalBacklog
    assert _run(_Per03WalBacklog().run(_ctx(wal_writer=writer))).status == "red"


# ─── PER-04 ───────────────────────────────────────────────────────────────────

def test_per04_green(tmp_path, monkeypatch):
    db_path = str(tmp_path / "control.db")
    monkeypatch.setattr("gateway.readiness.checks.persistence.get_settings",
                        lambda: _settings(control_plane_db_path=db_path))
    from gateway.readiness.checks.persistence import _Per04ControlPlaneDbWritable
    assert _run(_Per04ControlPlaneDbWritable().run(_ctx())).status == "green"


def test_per04_red(monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.persistence.get_settings",
        lambda: _settings(control_plane_db_path="/nonexistent/dir/control.db"),
    )
    import unittest.mock as mock
    with mock.patch(
        "gateway.readiness.checks.persistence.Path.mkdir",
        side_effect=PermissionError("denied"),
    ):
        from gateway.readiness.checks.persistence import _Per04ControlPlaneDbWritable
        assert _run(_Per04ControlPlaneDbWritable().run(_ctx())).status == "red"


# ─── PER-05 ───────────────────────────────────────────────────────────────────

def test_per05_amber_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.readiness.checks.persistence.get_settings",
        lambda: _settings(record_signing_key_path=str(tmp_path / "nonexistent.pem")),
    )
    from gateway.readiness.checks.persistence import _Per05SigningKeyFileIntegrity
    assert _run(_Per05SigningKeyFileIntegrity().run(_ctx())).status == "amber"


def test_per05_green(tmp_path, monkeypatch):
    """Valid Ed25519 key with mode 0600."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, NoEncryption,
        )
    except ImportError:
        pytest.skip("cryptography not installed")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    key_file = tmp_path / "signing.pem"
    key_file.write_bytes(pem)
    key_file.chmod(0o600)

    monkeypatch.setattr(
        "gateway.readiness.checks.persistence.get_settings",
        lambda: _settings(record_signing_key_path=str(key_file)),
    )
    from gateway.readiness.checks.persistence import _Per05SigningKeyFileIntegrity
    assert _run(_Per05SigningKeyFileIntegrity().run(_ctx())).status == "green"


def test_per05_red_bad_mode(tmp_path, monkeypatch):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, NoEncryption,
        )
    except ImportError:
        pytest.skip("cryptography not installed")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    key_file = tmp_path / "signing.pem"
    key_file.write_bytes(pem)
    key_file.chmod(0o644)  # world-readable → bad

    monkeypatch.setattr(
        "gateway.readiness.checks.persistence.get_settings",
        lambda: _settings(record_signing_key_path=str(key_file)),
    )
    from gateway.readiness.checks.persistence import _Per05SigningKeyFileIntegrity
    assert _run(_Per05SigningKeyFileIntegrity().run(_ctx())).status == "red"
