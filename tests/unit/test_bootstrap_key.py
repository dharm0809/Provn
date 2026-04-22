"""Unit tests for auto-generated bootstrap API key persistence."""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path

import pytest


def test_first_run_generates_and_persists(tmp_path):
    from gateway.auth.bootstrap_key import ensure_bootstrap_key, bootstrap_key_stable

    key, stable = ensure_bootstrap_key(str(tmp_path))
    assert key.startswith("wgk-")
    assert len(key) > 20
    assert stable is True

    persisted = tmp_path / "gateway-bootstrap-key.txt"
    assert persisted.exists()
    assert persisted.read_text().strip() == key
    assert bootstrap_key_stable(str(tmp_path)) is True


def test_second_run_reloads_same_key(tmp_path):
    from gateway.auth.bootstrap_key import ensure_bootstrap_key

    key1, stable1 = ensure_bootstrap_key(str(tmp_path))
    key2, stable2 = ensure_bootstrap_key(str(tmp_path))
    assert key1 == key2, "Second call must reload the persisted key, not generate a new one"
    assert stable1 is True and stable2 is True


def test_file_mode_is_0600(tmp_path):
    """On POSIX, bootstrap key file must be chmod 0600."""
    from gateway.auth.bootstrap_key import ensure_bootstrap_key

    ensure_bootstrap_key(str(tmp_path))
    path = tmp_path / "gateway-bootstrap-key.txt"
    if os.name == "posix":
        mode = stat_mod.S_IMODE(path.stat().st_mode)
        assert (mode & 0o077) == 0, f"Expected no group/world perms, got mode {oct(mode)}"


def test_malformed_key_regenerates(tmp_path):
    from gateway.auth.bootstrap_key import ensure_bootstrap_key

    path = tmp_path / "gateway-bootstrap-key.txt"
    path.write_text("not-a-wgk-key")

    key, stable = ensure_bootstrap_key(str(tmp_path))
    assert key.startswith("wgk-") and key != "not-a-wgk-key"
    assert stable is True


def test_unpersistable_falls_back_to_in_memory(tmp_path, monkeypatch):
    """When the wal path can't be created/written, return an in-memory key with stable=False."""
    from gateway.auth.bootstrap_key import ensure_bootstrap_key

    import unittest.mock as mock
    with mock.patch(
        "gateway.auth.bootstrap_key.Path.mkdir",
        side_effect=PermissionError("denied"),
    ):
        key, stable = ensure_bootstrap_key("/nonexistent/readonly/path")
    assert key.startswith("wgk-")
    assert stable is False


def test_bootstrap_key_stable_false_when_absent(tmp_path):
    from gateway.auth.bootstrap_key import bootstrap_key_stable
    assert bootstrap_key_stable(str(tmp_path)) is False


def test_bootstrap_key_stable_false_when_malformed(tmp_path):
    from gateway.auth.bootstrap_key import bootstrap_key_stable
    (tmp_path / "gateway-bootstrap-key.txt").write_text("bogus")
    assert bootstrap_key_stable(str(tmp_path)) is False
