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


def test_file_mode_is_0600_under_loose_umask(tmp_path, monkeypatch):
    """SECURITY: even with a permissive umask (022) inherited from the environment,
    the bootstrap key file must be born 0600 thanks to the explicit umask 077 set
    inside ensure_bootstrap_key. Without that, a chmod failure or interrupt between
    open() and chmod() would leave the file world-readable."""
    if os.name != "posix":
        pytest.skip("POSIX-only file-mode invariants")
    from gateway.auth.bootstrap_key import ensure_bootstrap_key

    saved = os.umask(0o022)  # simulate a loose default umask
    try:
        ensure_bootstrap_key(str(tmp_path))
    finally:
        os.umask(saved)

    path = tmp_path / "gateway-bootstrap-key.txt"
    mode = stat_mod.S_IMODE(path.stat().st_mode)
    assert (mode & 0o077) == 0, f"Expected no group/world perms, got mode {oct(mode)}"


def test_chmod_failure_logs_error_does_not_silently_pass(tmp_path, monkeypatch, caplog):
    """SECURITY: chmod failure must be logged at ERROR; before the fix it was a bare `pass`."""
    if os.name != "posix":
        pytest.skip("POSIX-only file-mode invariants")
    from gateway.auth.bootstrap_key import ensure_bootstrap_key

    real_chmod = Path.chmod

    def _raise_chmod(self, *a, **kw):  # noqa: ARG001
        # Only raise for the bootstrap-key tmp file, leave anything else (parent dirs etc.) alone.
        if self.name.startswith("gateway-bootstrap-key.txt"):
            raise OSError("simulated chmod failure")
        return real_chmod(self, *a, **kw)

    monkeypatch.setattr(Path, "chmod", _raise_chmod)

    with caplog.at_level("ERROR", logger="gateway.auth.bootstrap_key"):
        key, stable = ensure_bootstrap_key(str(tmp_path))

    assert stable is True  # umask + O_CREAT mode means the file is still safely 0600
    assert key.startswith("wgk-")
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("chmod" in r.message.lower() for r in errors), (
        f"Expected an ERROR log mentioning chmod, got: {[r.message for r in errors]}"
    )
