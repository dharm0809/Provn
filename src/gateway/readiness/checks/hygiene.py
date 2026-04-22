"""Hygiene readiness checks: HYG-01 through HYG-03."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.config import get_settings
from gateway.readiness.checks.security import _is_prod_host
from gateway.readiness.protocol import Category, CheckResult, Severity
from gateway.readiness.registry import register

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext


class _Hyg01LogLevel:
    id = "HYG-01"
    name = "Log level"
    category = Category.hygiene
    severity = Severity.warn

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        level = (settings.log_level or "INFO").upper()
        is_prod = _is_prod_host(settings)
        if level == "INFO":
            return CheckResult(status="green", detail=f"log_level={level}", elapsed_ms=elapsed)
        if level == "DEBUG" and is_prod:
            return CheckResult(
                status="amber",
                detail="log_level=DEBUG on prod host — risks leaking sensitive data",
                remediation="Set WALACOR_LOG_LEVEL=INFO",
                evidence={"log_level": level, "is_prod_host": is_prod},
                elapsed_ms=elapsed,
            )
        return CheckResult(
            status="green" if not is_prod else "amber",
            detail=f"log_level={level}",
            evidence={"log_level": level, "is_prod_host": is_prod},
            elapsed_ms=elapsed,
        )


class _Hyg02RateLimiting:
    id = "HYG-02"
    name = "Rate limiting"
    category = Category.hygiene
    severity = Severity.warn

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if settings.rate_limit_enabled:
            return CheckResult(status="green", detail="Rate limiting enabled", elapsed_ms=elapsed)
        return CheckResult(
            status="amber",
            detail="Rate limiting disabled",
            remediation="Set WALACOR_RATE_LIMIT_ENABLED=true",
            elapsed_ms=elapsed,
        )


_OPENWEBUI_VOLUME_PATHS = (
    "/var/lib/docker/volumes/gateway_dharm_webui-data/_data/.webui_secret_key",
    "/var/lib/docker/volumes/webui-data/_data/.webui_secret_key",
)


class _Hyg03OpenWebUISecretPersistence:
    id = "HYG-03"
    name = "OpenWebUI secret persistence"
    category = Category.hygiene
    severity = Severity.warn

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        found = [p for p in _OPENWEBUI_VOLUME_PATHS if Path(p).exists()]
        if not found:
            return CheckResult(
                status="green",
                detail="OpenWebUI not co-located — skipping",
                elapsed_ms=elapsed_ms(),
            )
        key_path = found[0]
        try:
            stat = Path(key_path).stat()
            size = stat.st_size
        except Exception as exc:
            return CheckResult(status="amber", detail=f"stat failed: {exc}", elapsed_ms=elapsed_ms())

        if size < 20:
            return CheckResult(
                status="amber",
                detail=".webui_secret_key present but suspiciously small",
                evidence={"path": key_path, "size": size},
                elapsed_ms=elapsed_ms(),
            )
        return CheckResult(
            status="green",
            detail=".webui_secret_key persisted on disk",
            evidence={"path": key_path, "size": size},
            elapsed_ms=elapsed_ms(),
        )


register(_Hyg01LogLevel())
register(_Hyg02RateLimiting())
register(_Hyg03OpenWebUISecretPersistence())
