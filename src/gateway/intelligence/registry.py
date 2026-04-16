"""Filesystem-backed registry for Phase 25 ONNX model artifacts.

Layout:
  {base}/production/{model}.onnx        - currently serving
  {base}/candidates/{model}-{version}.onnx  - pending shadow validation
  {base}/archive/{model}-{version}.onnx - retired models (rollback targets)
  {base}/archive/failed/                - candidates that failed gates

Model names: lowercase-with-underscore (match `ModelVerdict.model_name`:
"intent", "schema_mapper", "safety"). Versions: alphanumeric + `._-`.

Task 9 is the skeleton - directory creation, listing, and per-model
asyncio.Lock factory. Atomic swap (promote/rollback) is Task 10,
InferenceSession reload signaling is Task 11.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Canonical set of model names this registry serves. Matches the `model_name`
# strings recorded by `ModelVerdict` (see `intelligence/types.py`) and the
# three ONNX inference sites wired in Task 7. Closed set — any model-name
# input that is NOT in here is rejected to prevent path traversal and to
# keep the candidate-filename regex from parsing phantom models (e.g.
# `prefix-intent-v2.onnx` would otherwise be accepted as `model=prefix`).
ALLOWED_MODEL_NAMES: frozenset[str] = frozenset({"intent", "schema_mapper", "safety"})


@dataclass(frozen=True)
class Candidate:
    model: str
    version: str
    path: Path


# Candidate filename format: <model>-<version>.onnx
# model: lowercase letters + underscores (matches ModelVerdict.model_name)
# version: alphanumeric + `._-` (e.g. "v1", "v3.2", "2026-04-16", "abc-1")
_CAND_RE = re.compile(r"^(?P<model>[a-z_]+)-(?P<version>[a-zA-Z0-9_.\-]+)\.onnx$")


def _validate_model_name(model: str) -> None:
    if model not in ALLOWED_MODEL_NAMES:
        raise ValueError(
            f"unknown model name {model!r}; "
            f"expected one of {sorted(ALLOWED_MODEL_NAMES)}"
        )


def _validate_archived_filename(filename: str, model: str) -> None:
    """Validate an archive filename is a safe pure filename and belongs to `model`.

    Rejects path separators and `..` traversal segments to keep the archive
    input to `rollback` confined to the archive directory. Also enforces that
    the filename starts with `{model}-` so callers can't rollback one model
    onto another's archive (e.g. restore a safety archive into intent's slot).
    """
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(
            f"invalid archived_filename {filename!r}: "
            "no path separators or '..' allowed"
        )
    if not filename.startswith(f"{model}-"):
        raise ValueError(
            f"archived_filename {filename!r} does not belong to model {model!r}"
        )


class ModelRegistry:
    def __init__(self, base_path: str) -> None:
        self.base = Path(base_path)
        self._locks: dict[str, asyncio.Lock] = {}
        # Monotonic per-model reload signal. Bumped inside `lock_for(model)`
        # on every successful promote/rollback. ONNX clients compare against
        # their last-observed value and rebuild their InferenceSession when
        # the counter moves. Initialized to 0 for every canonical model so
        # clients can seed `_last_generation=-1` and be guaranteed to reload
        # on first inference after wiring.
        self._generations: dict[str, int] = {m: 0 for m in ALLOWED_MODEL_NAMES}

    def ensure_structure(self) -> None:
        for sub in ("production", "candidates", "archive", "archive/failed"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)

    def production_path(self, model: str) -> Path:
        _validate_model_name(model)
        return self.base / "production" / f"{model}.onnx"

    def list_production_models(self) -> list[str]:
        prod = self.base / "production"
        if not prod.is_dir():
            return []
        # Only real .onnx files at the top level. Skip hidden files, non-onnx,
        # and stems containing a dash (defensive: a stray `intent-v2.onnx`
        # landing in production/ must not be treated as a production model).
        # Also filter to ALLOWED_MODEL_NAMES so only canonical names surface.
        return sorted(
            p.stem
            for p in prod.iterdir()
            if p.is_file()
            and p.suffix == ".onnx"
            and not p.name.startswith(".")
            and "-" not in p.stem
            and p.stem in ALLOWED_MODEL_NAMES
        )

    def list_candidates(self) -> list[Candidate]:
        cands_dir = self.base / "candidates"
        if not cands_dir.is_dir():
            return []
        out: list[Candidate] = []
        for p in cands_dir.iterdir():
            if not p.is_file() or p.suffix != ".onnx":
                continue
            m = _CAND_RE.match(p.name)
            if not m:
                continue
            # Filter to canonical models — kills phantom-model parses like
            # `prefix-intent-v2.onnx` (model=prefix, version=intent-v2), which
            # the regex alone would accept.
            if m["model"] not in ALLOWED_MODEL_NAMES:
                continue
            out.append(Candidate(model=m["model"], version=m["version"], path=p))
        # Sort for deterministic order across environments.
        return sorted(out, key=lambda c: (c.model, c.version))

    def get_generation(self, model: str) -> int:
        """Return the current reload-signal counter for `model`.

        Bumped on every successful `promote`/`rollback`. ONNX clients poll
        this in the hot path and rebuild their `InferenceSession` when the
        value has moved since their last inference. Cheap — a dict lookup.
        """
        _validate_model_name(model)
        return self._generations[model]

    def lock_for(self, model: str) -> asyncio.Lock:
        _validate_model_name(model)
        # Safe under single event loop: the check-then-insert has no `await`
        # between operations and dict access is atomic at the CPython
        # bytecode level. Thread-unsafe by design (matches VerdictBuffer).
        if model not in self._locks:
            self._locks[model] = asyncio.Lock()
        return self._locks[model]

    def _archive_filename(self, model: str) -> Path:
        """Generate a collision-safe archive filename for the current production file.

        Uses ISO-8601 UTC timestamp with microseconds, falling back to a counter
        suffix if (pathologically) the filename is already in use — catches
        clock-reset cases without racing. Called only from inside
        `self.lock_for(model)`, so concurrent callers for the same model are
        already serialized.
        """
        archive_dir = self.base / "archive"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        base = f"{model}-archived-{ts}.onnx"
        candidate = archive_dir / base
        if not candidate.exists():
            return candidate
        # Extremely unlikely under the lock, but defend against clock reset
        # or identical-microsecond calls: append a bounded counter.
        for i in range(1, 1000):
            alt = archive_dir / f"{model}-archived-{ts}-{i}.onnx"
            if not alt.exists():
                return alt
        raise RuntimeError(f"could not find unique archive filename for {model}")

    async def promote(self, model: str, version: str) -> None:
        """Atomically promote a candidate to production.

        1. Current production (if any) is moved to archive under a
           collision-safe timestamped filename.
        2. The candidate at `candidates/{model}-{version}.onnx` is renamed
           onto `production/{model}.onnx`.

        Both moves are `os.rename` within the same filesystem (subdirs of
        `base_path`), so each step is atomic on POSIX. Serialized per-model
        via `lock_for(model)`.
        """
        _validate_model_name(model)
        async with self.lock_for(model):
            cand_path = self.base / "candidates" / f"{model}-{version}.onnx"
            if not cand_path.exists():
                raise FileNotFoundError(cand_path)
            prod_path = self.production_path(model)
            if prod_path.exists():
                os.rename(prod_path, self._archive_filename(model))
            os.rename(cand_path, prod_path)
            # Only bump after both renames succeed — partial state must not
            # trigger client reloads onto a half-swapped production file.
            self._generations[model] += 1

    async def rollback(self, model: str, archived_filename: str) -> None:
        """Restore a previously archived version to production.

        The current production file (if any) is archived under a new
        collision-safe timestamped filename before the rollback target is
        moved into production — rollback must not discard live state.
        Serialized per-model via `lock_for(model)`.
        """
        _validate_model_name(model)
        _validate_archived_filename(archived_filename, model)
        async with self.lock_for(model):
            archive_path = self.base / "archive" / archived_filename
            if not archive_path.exists():
                raise FileNotFoundError(archive_path)
            prod_path = self.production_path(model)
            if prod_path.exists():
                os.rename(prod_path, self._archive_filename(model))
            os.rename(archive_path, prod_path)
            self._generations[model] += 1
