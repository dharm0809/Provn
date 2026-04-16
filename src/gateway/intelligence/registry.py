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
import re
from dataclasses import dataclass
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


class ModelRegistry:
    def __init__(self, base_path: str) -> None:
        self.base = Path(base_path)
        self._locks: dict[str, asyncio.Lock] = {}

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

    def lock_for(self, model: str) -> asyncio.Lock:
        _validate_model_name(model)
        # Safe under single event loop: the check-then-insert has no `await`
        # between operations and dict access is atomic at the CPython
        # bytecode level. Thread-unsafe by design (matches VerdictBuffer).
        if model not in self._locks:
            self._locks[model] = asyncio.Lock()
        return self._locks[model]
