"""Tests for the multi-worker WAL path helpers (Phase 1 of the 3b redesign)."""

from __future__ import annotations

import os
from pathlib import Path

from gateway.wal.path import iter_wal_db_paths, wal_db_path


def test_workers_le_1_yields_legacy_path(tmp_path: Path) -> None:
    """workers<=1 → byte-identical legacy ``wal.db`` (regression gate)."""
    for w in (0, 1):
        assert wal_db_path(str(tmp_path), w) == str(tmp_path / "wal.db"), w


def test_workers_gt_1_yields_per_pid_path(tmp_path: Path) -> None:
    """workers>1 → ``wal-<pid>.db`` for this process."""
    p = wal_db_path(str(tmp_path), 4)
    assert p == str(tmp_path / f"wal-{os.getpid()}.db")


def test_iter_includes_legacy_and_per_worker(tmp_path: Path) -> None:
    """Reader-side union: enumerate every WAL file in the directory."""
    (tmp_path / "wal.db").touch()
    (tmp_path / "wal-101.db").touch()
    (tmp_path / "wal-202.db").touch()
    (tmp_path / "other.db").touch()  # unrelated; must be excluded
    (tmp_path / "wal-foo.txt").touch()  # not a .db; must be excluded

    found = iter_wal_db_paths(str(tmp_path))
    assert found == [
        str(tmp_path / "wal.db"),
        str(tmp_path / "wal-101.db"),
        str(tmp_path / "wal-202.db"),
    ], found


def test_iter_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert iter_wal_db_paths(str(tmp_path / "does-not-exist")) == []


def test_iter_only_legacy_present(tmp_path: Path) -> None:
    """The workers=1 case: union of one — must equal a single-element list
    containing the legacy file."""
    (tmp_path / "wal.db").touch()
    assert iter_wal_db_paths(str(tmp_path)) == [str(tmp_path / "wal.db")]


def test_iter_only_per_worker(tmp_path: Path) -> None:
    """Downgrade-resilience: no legacy file, only per-worker files."""
    (tmp_path / "wal-3.db").touch()
    (tmp_path / "wal-1.db").touch()
    (tmp_path / "wal-2.db").touch()
    # sorted lexicographically, no legacy prefix
    assert iter_wal_db_paths(str(tmp_path)) == [
        str(tmp_path / "wal-1.db"),
        str(tmp_path / "wal-2.db"),
        str(tmp_path / "wal-3.db"),
    ]
