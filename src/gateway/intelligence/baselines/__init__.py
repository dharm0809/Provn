"""Bundled baseline ONNX manifest.

A baseline is the day-1 weights shipped inside the wheel for each
intelligence model (intent / safety / schema_mapper). The self-learning
loop trains shadows on top; until a shadow promotes, the baseline is
what serves verdicts.

This module loads `manifest.json`, exposes per-model metadata, and writes
a `production/{model}.baseline.json` sidecar at seed time so the API can
trustably distinguish "running the baseline" from "running a locally-
trained candidate". Without the sidecar we'd have to re-hash production
files on every API call to identify them.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"


@dataclass(frozen=True)
class Baseline:
    model_name: str
    version: str
    sha256: str
    size_bytes: int
    architecture: str
    parameters: str
    task: str
    notes: str


def load_manifest() -> dict[str, Any]:
    """Read and return the parsed manifest, or an empty skeleton on error.

    Fail-open: if the manifest is missing or malformed, the system still
    boots — baselines just become invisible to the dashboard. Better than
    crashing first-run.
    """
    try:
        with _MANIFEST_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        logger.warning("baseline manifest unreadable at %s", _MANIFEST_PATH, exc_info=True)
        return {"baselines": {}, "cold_start": {"samples_until_graduation": 200}}


def baseline_for(model_name: str) -> Baseline | None:
    """Return the declared baseline for `model_name`, or None if absent."""
    entry = load_manifest().get("baselines", {}).get(model_name)
    if not isinstance(entry, dict):
        return None
    try:
        return Baseline(
            model_name=model_name,
            version=str(entry["version"]),
            sha256=str(entry["sha256"]),
            size_bytes=int(entry["size_bytes"]),
            architecture=str(entry["architecture"]),
            parameters=str(entry["parameters"]),
            task=str(entry["task"]),
            notes=str(entry.get("notes") or ""),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("baseline manifest entry malformed for %r", model_name, exc_info=True)
        return None


def cold_start_threshold() -> int:
    """Predictions-per-7d below which a baseline is considered 'warming up'."""
    cs = load_manifest().get("cold_start") or {}
    try:
        return max(0, int(cs.get("samples_until_graduation", 200)))
    except (TypeError, ValueError):
        return 200


def baseline_sidecar_path(production_path: Path) -> Path:
    """Sidecar lives next to the production .onnx — `intent.baseline.json`."""
    return production_path.with_suffix(".baseline.json")


def write_sidecar(production_path: Path, baseline: Baseline, source_sha256: str) -> None:
    """Record that this production file is a freshly-seeded baseline.

    Called from the migration in main.py right after the baseline is
    copied into the registry. The sidecar lets the API answer "is this
    file the bundled baseline?" without re-hashing the full .onnx on
    every dashboard poll.
    """
    payload = {
        "model_name": baseline.model_name,
        "baseline_version": baseline.version,
        "expected_sha256": baseline.sha256,
        "source_sha256": source_sha256,
        "copied_at": datetime.now(timezone.utc).isoformat(),
        "architecture": baseline.architecture,
        "parameters": baseline.parameters,
    }
    sidecar = baseline_sidecar_path(production_path)
    try:
        sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("failed to write baseline sidecar %s", sidecar, exc_info=True)


def read_sidecar(production_path: Path) -> dict[str, Any] | None:
    """Return parsed sidecar metadata, or None if absent/unreadable."""
    sidecar = baseline_sidecar_path(production_path)
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def remove_sidecar(production_path: Path) -> None:
    """Drop the sidecar after a real promotion supersedes the baseline."""
    sidecar = baseline_sidecar_path(production_path)
    try:
        sidecar.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("failed to remove baseline sidecar %s", sidecar, exc_info=True)


def file_sha256(path: Path) -> str:
    """SHA-256 of a file's contents. Used to verify baselines at seed time."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
