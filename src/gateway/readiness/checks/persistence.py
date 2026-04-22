"""Persistence readiness checks: PER-01 through PER-05."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat as stat_mod
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.config import get_settings
from gateway.readiness.protocol import Category, CheckResult, Severity
from gateway.readiness.registry import register

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext


class _Per01WalWritable:
    id = "PER-01"
    name = "WAL writable"
    category = Category.persistence
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        wal_dir = Path(settings.wal_path)
        probe = wal_dir / f".readiness-probe-{uuid.uuid4().hex}"
        try:
            wal_dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("probe")
            probe.unlink()
            return CheckResult(
                status="green",
                detail=f"WAL directory writable: {wal_dir}",
                evidence={"wal_path": str(wal_dir)},
                elapsed_ms=elapsed_ms(),
            )
        except Exception as exc:
            try:
                probe.unlink(missing_ok=True)
            except Exception:
                pass
            return CheckResult(
                status="red",
                detail=f"WAL directory not writable: {exc}",
                remediation=f"Ensure {wal_dir} exists and the process has write permission",
                evidence={"wal_path": str(wal_dir), "error": str(exc)},
                elapsed_ms=elapsed_ms(),
            )


class _Per02WalDiskHeadroom:
    id = "PER-02"
    name = "WAL disk headroom"
    category = Category.persistence
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        wal_dir = settings.wal_path
        if not os.path.exists(wal_dir):
            return CheckResult(status="amber", detail=f"wal_path does not exist: {wal_dir}", elapsed_ms=elapsed_ms())

        try:
            usage = shutil.disk_usage(wal_dir)
        except Exception as exc:
            return CheckResult(status="amber", detail=f"disk_usage failed: {exc}", elapsed_ms=elapsed_ms())

        free_pct = (usage.free / usage.total) * 100.0
        threshold = settings.disk_min_free_percent
        status = "green" if free_pct >= threshold else ("amber" if free_pct >= threshold / 2 else "red")
        return CheckResult(
            status=status,
            detail=f"WAL disk free: {free_pct:.1f}% (threshold {threshold:.1f}%)",
            remediation=None if status == "green" else f"Free disk on {wal_dir} — below {threshold}% threshold",
            evidence={"free_percent": round(free_pct, 2), "threshold": threshold, "total_bytes": usage.total, "free_bytes": usage.free},
            elapsed_ms=elapsed_ms(),
        )


class _Per03WalBacklog:
    id = "PER-03"
    name = "WAL backlog"
    category = Category.persistence
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if ctx.wal_writer is None:
            return CheckResult(status="amber", detail="WAL writer not available", elapsed_ms=elapsed_ms())

        try:
            pending = ctx.wal_writer.pending_count()
        except Exception as exc:
            return CheckResult(status="amber", detail=f"pending_count failed: {exc}", elapsed_ms=elapsed_ms())

        high = settings.wal_high_water_mark
        amber_at = int(high * 0.8)
        status = "green" if pending < amber_at else ("amber" if pending < high else "red")
        return CheckResult(
            status=status,
            detail=f"WAL pending={pending} (amber≥{amber_at}, red≥{high})",
            remediation=None if status == "green" else "Delivery worker behind — check walacor_client health or WAL disk",
            evidence={"pending": pending, "high_water_mark": high},
            elapsed_ms=elapsed_ms(),
        )


class _Per04ControlPlaneDbWritable:
    id = "PER-04"
    name = "Control-plane DB writable"
    category = Category.persistence
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if not settings.control_plane_enabled:
            return CheckResult(status="amber", detail="Control plane not enabled", elapsed_ms=elapsed_ms())

        db_path = settings.control_plane_db_path or os.path.join(settings.wal_path, "control.db")
        try:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE IF NOT EXISTS readiness_probe (k INTEGER)")
            conn.execute("DROP TABLE IF EXISTS readiness_probe")
            conn.commit()
            conn.close()
            return CheckResult(
                status="green",
                detail=f"Control-plane DB writable: {db_path}",
                evidence={"db_path": db_path},
                elapsed_ms=elapsed_ms(),
            )
        except Exception as exc:
            return CheckResult(
                status="red",
                detail=f"Control-plane DB write failed: {exc}",
                evidence={"db_path": db_path, "error": str(exc)},
                elapsed_ms=elapsed_ms(),
            )


class _Per05SigningKeyFileIntegrity:
    id = "PER-05"
    name = "Signing-key file integrity"
    category = Category.persistence
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        key_path = settings.record_signing_key_path or os.path.join(settings.wal_path, "record-signing.ed25519.pem")
        p = Path(key_path)
        if not p.exists():
            return CheckResult(
                status="amber",
                detail=f"Signing-key file not present: {key_path}",
                evidence={"key_path": key_path, "exists": False},
                elapsed_ms=elapsed_ms(),
            )

        try:
            st = p.stat()
            mode = stat_mod.S_IMODE(st.st_mode)
        except Exception as exc:
            return CheckResult(status="amber", detail=f"stat failed: {exc}", elapsed_ms=elapsed_ms())

        mode_ok = (mode & 0o077) == 0  # no group/world perms
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            load_pem_private_key(p.read_bytes(), password=None)
            parse_ok = True
            parse_err = None
        except Exception as exc:
            parse_ok = False
            parse_err = str(exc)

        if parse_ok and mode_ok:
            return CheckResult(
                status="green",
                detail=f"Signing key valid (mode {oct(mode)})",
                evidence={"key_path": key_path, "mode": oct(mode)},
                elapsed_ms=elapsed_ms(),
            )
        detail = []
        if not mode_ok: detail.append(f"mode {oct(mode)} (expected 0o600)")
        if not parse_ok: detail.append(f"parse failed: {parse_err}")
        return CheckResult(
            status="red",
            detail="Signing key problem: " + "; ".join(detail),
            remediation="Regenerate key with ensure_signing_key or chmod 0600",
            evidence={"key_path": key_path, "mode": oct(mode), "parse_ok": parse_ok},
            elapsed_ms=elapsed_ms(),
        )


register(_Per01WalWritable())
register(_Per02WalDiskHeadroom())
register(_Per03WalBacklog())
register(_Per04ControlPlaneDbWritable())
register(_Per05SigningKeyFileIntegrity())
