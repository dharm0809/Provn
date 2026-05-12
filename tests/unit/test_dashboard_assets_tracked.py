"""Vite-built dashboard PNGs must be tracked by git.

pyproject.toml ships `src/gateway/lineage/static/**` as wheel package_data.
The root .gitignore has a blanket `*.png` rule; without an explicit re-include
for `src/gateway/lineage/static/assets/**/*.png` the built logos get excluded
from git → Docker image builds from a clean checkout end up with a logo-less
dashboard (exactly what was hot-patched in production once).

This test fails fast if the gitignore drifts back.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILT_ASSETS = REPO_ROOT / "src" / "gateway" / "lineage" / "static" / "assets"


@pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists() or shutil.which("git") is None,
    reason="not a git checkout / git not on PATH",
)
def test_built_dashboard_pngs_are_tracked():
    """Every PNG under static/assets/ must be tracked by git."""
    if not BUILT_ASSETS.exists():
        pytest.skip(
            f"{BUILT_ASSETS} does not exist — dashboard not built yet. "
            f"Run 'npm run build' under src/gateway/lineage/dashboard/ first."
        )

    on_disk = sorted(p for p in BUILT_ASSETS.glob("*.png"))
    if not on_disk:
        pytest.skip(
            f"No PNG assets present in {BUILT_ASSETS} — dashboard build may have skipped them."
        )

    tracked = subprocess.run(
        ["git", "ls-files", str(BUILT_ASSETS.relative_to(REPO_ROOT))],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout.splitlines()
    tracked_pngs = {Path(line) for line in tracked if line.endswith(".png")}
    on_disk_rel = {p.relative_to(REPO_ROOT) for p in on_disk}

    missing = on_disk_rel - tracked_pngs
    assert not missing, (
        "Built dashboard PNGs exist on disk but aren't tracked by git — the "
        ".gitignore *.png blanket rule is excluding them. Add a !-rule for "
        "src/gateway/lineage/static/assets/**/*.png. Untracked: "
        f"{sorted(str(p) for p in missing)}"
    )
