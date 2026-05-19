"""WAL SQLite file path helpers — single-file and per-worker modes.

Phase 1 of the 3b multi-worker redesign. Two functions:

  ``wal_db_path(dir, workers)`` — the path THIS process should write to.
    workers <= 1 → ``{dir}/wal.db``  (byte-identical to legacy mode)
    workers >  1 → ``{dir}/wal-<pid>.db``  (per-process — SQLite cannot be
                                            safely written by N processes
                                            to one file even with the
                                            internal write_lock)

  ``iter_wal_db_paths(dir)`` — every WAL file in the directory, for the
    lineage/readiness *read* path. At workers=1 this is a one-element
    list containing the legacy file; at workers>1 the readers union
    across every per-worker file plus any legacy ``wal.db`` left over
    from a downgrade. Existence-checked and sorted for deterministic
    ordering across calls.

The single hard requirement of Phase 1: **workers=1 behaviour is
byte-identical** to before the refactor. Both helpers default to the
legacy single-file shape in that case so the regression gate holds.
"""

from __future__ import annotations

import os
from pathlib import Path


# Legacy single-file name. Kept as a module-level constant so the readers
# and writers agree without re-encoding the string in three places.
_LEGACY_DB = "wal.db"


def wal_db_path(wal_dir: str | os.PathLike[str], uvicorn_workers: int) -> str:
    """Return the SQLite WAL DB path for *this* process.

    workers<=1 yields the legacy ``wal.db`` (byte-identical with the
    pre-Phase-1 single-worker deployment). workers>1 yields
    ``wal-<pid>.db`` so each uvicorn worker has its own SQLite file —
    SQLite's WAL mode + ``synchronous=NORMAL`` cannot be safely shared
    across processes; per-worker files are the only safe path without
    introducing an external infra dependency.
    """
    base = Path(wal_dir)
    if uvicorn_workers and uvicorn_workers > 1:
        return str(base / f"wal-{os.getpid()}.db")
    return str(base / _LEGACY_DB)


def iter_wal_db_paths(wal_dir: str | os.PathLike[str]) -> list[str]:
    """Return every existing WAL DB file in *wal_dir*, for read aggregation.

    Includes the legacy ``wal.db`` and every per-worker ``wal-*.db``.
    Sorted (legacy first, then per-worker lexicographic) for deterministic
    ordering across calls. Missing directory or no files → empty list.
    """
    d = Path(wal_dir)
    if not d.is_dir():
        return []
    paths: list[Path] = []
    legacy = d / _LEGACY_DB
    if legacy.is_file():
        paths.append(legacy)
    paths.extend(sorted(p for p in d.glob("wal-*.db") if p.is_file()))
    return [str(p) for p in paths]
