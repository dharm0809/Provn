"""Dependency readiness checks: DEP-01 through DEP-05."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from gateway.config import get_settings
from gateway.readiness.protocol import Category, CheckResult, Severity
from gateway.readiness.registry import register

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext


class _Dep01WalacorAuth:
    id = "DEP-01"
    name = "Walacor auth"
    category = Category.dependency
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if ctx.walacor_client is None:
            return CheckResult(
                status="amber",
                detail="Walacor storage not configured — skipping auth probe",
                evidence={"walacor_enabled": False},
                elapsed_ms=elapsed_ms(),
            )
        try:
            await ctx.walacor_client.start()
            return CheckResult(status="green", detail="Walacor auth succeeded", elapsed_ms=elapsed_ms())
        except Exception as exc:
            return CheckResult(
                status="red",
                detail=f"Walacor auth failed: {exc}",
                remediation="Check WALACOR_SERVER_URL, WALACOR_USERNAME, WALACOR_PASSWORD",
                evidence={"error": str(exc)},
                elapsed_ms=elapsed_ms(),
            )


class _Dep02WalacorQuery:
    id = "DEP-02"
    name = "Walacor query"
    category = Category.dependency
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if ctx.walacor_client is None:
            return CheckResult(status="amber", detail="Walacor client not available", elapsed_ms=elapsed_ms())

        try:
            await ctx.walacor_client.query_complex(
                settings.walacor_executions_etid,
                [{"$match": {}}, {"$limit": 1}],
            )
            return CheckResult(status="green", detail="Walacor query succeeded", elapsed_ms=elapsed_ms())
        except Exception as exc:
            return CheckResult(
                status="red",
                detail=f"Walacor query failed: {exc}",
                evidence={"etid": settings.walacor_executions_etid, "error": str(exc)},
                elapsed_ms=elapsed_ms(),
            )


class _Dep03OllamaReachable:
    id = "DEP-03"
    name = "Ollama reachable"
    category = Category.dependency
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        # "needs_ollama" was previously driven by the config DEFAULTS, not by
        # what the operator actually chose. `llama_guard_enabled` defaults to
        # True and `provider_ollama_url` has a non-empty default of
        # `http://localhost:11434` — so on any deployment that doesn't run
        # Ollama (e.g. EC2 prod with OpenAI + Anthropic backends), the check
        # fired red unconditionally. The intent of DEP-03 is "if THIS
        # deployment depends on Ollama, is it reachable?" Treat "operator
        # explicitly opted in" as the signal — checked via pydantic's
        # `model_fields_set`, which records exactly which fields received
        # an env/config override versus inheriting the default.
        explicitly_opted_in = (
            "llama_guard_enabled" in settings.model_fields_set
            or "provider_ollama_url" in settings.model_fields_set
            or "llama_guard_ollama_url" in settings.model_fields_set
        )
        if not explicitly_opted_in:
            return CheckResult(
                status="green",
                detail=(
                    "Ollama not configured for this deployment "
                    "(set WALACOR_LLAMA_GUARD_ENABLED or WALACOR_PROVIDER_OLLAMA_URL "
                    "to enable the probe)"
                ),
                elapsed_ms=elapsed_ms(),
            )

        url = settings.provider_ollama_url.rstrip("/") + "/api/tags"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.get(url)
            if resp.status_code == 200:
                return CheckResult(
                    status="green",
                    detail=f"Ollama reachable at {settings.provider_ollama_url}",
                    elapsed_ms=elapsed_ms(),
                )
            return CheckResult(
                status="red",
                detail=f"Ollama returned HTTP {resp.status_code}",
                evidence={"url": url, "status_code": resp.status_code},
                elapsed_ms=elapsed_ms(),
            )
        except Exception as exc:
            return CheckResult(
                status="red",
                detail=f"Ollama unreachable: {exc}",
                remediation=f"Check {settings.provider_ollama_url} is running",
                evidence={"url": url, "error": str(exc)},
                elapsed_ms=elapsed_ms(),
            )


class _Dep04RedisReachable:
    id = "DEP-04"
    name = "Redis reachable"
    category = Category.dependency
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if not settings.redis_url:
            return CheckResult(status="green", detail="Redis not configured (single-node mode)", elapsed_ms=elapsed_ms())

        if ctx.redis_client is None:
            return CheckResult(status="amber", detail="redis_client not initialized", elapsed_ms=elapsed_ms())
        try:
            pong = await ctx.redis_client.ping()
            if pong:
                return CheckResult(status="green", detail="Redis PING → PONG", elapsed_ms=elapsed_ms())
            return CheckResult(status="red", detail="Redis ping returned falsy", elapsed_ms=elapsed_ms())
        except Exception as exc:
            return CheckResult(
                status="red",
                detail=f"Redis ping failed: {exc}",
                evidence={"error": str(exc)},
                elapsed_ms=elapsed_ms(),
            )


class _Dep05ProviderKeysPresent:
    id = "DEP-05"
    name = "Provider keys present"
    category = Category.dependency
    severity = Severity.warn

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        """Shape check only — do NOT make outbound calls."""
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        issues = []
        # OpenAI key shape: starts with "sk-", ≥20 chars
        key = getattr(settings, "openai_api_key", "") or ""
        if key and not (key.startswith("sk-") and len(key) >= 20):
            issues.append("openai_api_key shape suspect")
        # Anthropic key shape: starts with "sk-ant-"
        key = getattr(settings, "anthropic_api_key", "") or ""
        if key and not (key.startswith("sk-ant-") and len(key) >= 20):
            issues.append("anthropic_api_key shape suspect")

        if not issues:
            return CheckResult(status="green", detail="Provider keys pass shape check (or none configured)", elapsed_ms=elapsed)
        return CheckResult(
            status="amber",
            detail="; ".join(issues),
            evidence={"issues": issues},
            elapsed_ms=elapsed,
        )


register(_Dep01WalacorAuth())
register(_Dep02WalacorQuery())
register(_Dep03OllamaReachable())
register(_Dep04RedisReachable())
register(_Dep05ProviderKeysPresent())
