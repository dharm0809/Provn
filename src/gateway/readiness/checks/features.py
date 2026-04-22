"""Feature-coherence readiness checks: FEA-01 through FEA-07.

Each row is 'X is enabled but X's dependency isn't'.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from gateway.config import get_settings
from gateway.readiness.protocol import Category, CheckResult, Severity
from gateway.readiness.registry import register

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext


async def _ping_ollama(url: str, timeout: float = 3.0) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.get(url.rstrip("/") + "/api/tags")
        return resp.status_code == 200
    except Exception:
        return False


class _Fea01LlamaGuard:
    id = "FEA-01"
    name = "Llama Guard"
    category = Category.feature
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if not settings.llama_guard_enabled:
            return CheckResult(status="green", detail="Llama Guard disabled", elapsed_ms=elapsed_ms())

        ollama_url = settings.llama_guard_ollama_url or settings.provider_ollama_url
        if not await _ping_ollama(ollama_url):
            return CheckResult(
                status="red",
                detail=f"Llama Guard enabled but Ollama ({ollama_url}) unreachable",
                remediation=f"Check Ollama is running or disable WALACOR_LLAMA_GUARD_ENABLED",
                elapsed_ms=elapsed_ms(),
            )
        return CheckResult(status="green", detail="Llama Guard Ollama reachable", elapsed_ms=elapsed_ms())


class _Fea02WebSearch:
    id = "FEA-02"
    name = "Web search"
    category = Category.feature
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if not settings.web_search_enabled:
            return CheckResult(status="green", detail="Web search disabled", elapsed_ms=elapsed)

        issues = []
        if not settings.tool_aware_enabled:
            issues.append("tool_aware_enabled=false")
        if ctx.tool_registry is None:
            issues.append("tool registry not initialized")
        elif ctx.tool_registry.get_tool_schema("web_search") is None:
            issues.append("web_search tool not registered")

        if issues:
            return CheckResult(
                status="red",
                detail="Web search enabled but: " + "; ".join(issues),
                remediation="Set WALACOR_TOOL_AWARE_ENABLED=true and verify web_search registration in startup",
                evidence={"issues": issues},
                elapsed_ms=elapsed,
            )
        return CheckResult(status="green", detail="Web search properly wired", elapsed_ms=elapsed)


class _Fea03Presidio:
    id = "FEA-03"
    name = "Presidio"
    category = Category.feature
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if not settings.presidio_pii_enabled:
            return CheckResult(status="green", detail="Presidio disabled", elapsed_ms=elapsed)
        try:
            import presidio_analyzer  # noqa: F401
            return CheckResult(status="green", detail="presidio_analyzer importable", elapsed_ms=elapsed)
        except ImportError as exc:
            return CheckResult(
                status="red",
                detail=f"Presidio enabled but import failed: {exc}",
                remediation="pip install presidio-analyzer (and language model) or disable the feature",
                elapsed_ms=elapsed,
            )


class _Fea04PromptGuard:
    id = "FEA-04"
    name = "Prompt Guard"
    category = Category.feature
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if not settings.prompt_guard_enabled:
            return CheckResult(status="green", detail="Prompt Guard disabled", elapsed_ms=elapsed)
        try:
            import transformers  # noqa: F401
            return CheckResult(status="green", detail="HF transformers importable", elapsed_ms=elapsed)
        except ImportError as exc:
            return CheckResult(
                status="red",
                detail=f"Prompt Guard enabled but HF stack missing: {exc}",
                remediation="pip install transformers torch or disable prompt_guard_enabled",
                elapsed_ms=elapsed,
            )


class _Fea05OTel:
    id = "FEA-05"
    name = "OTel"
    category = Category.feature
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if not settings.otel_enabled:
            return CheckResult(status="green", detail="OTel disabled", elapsed_ms=elapsed)

        issues = []
        try:
            import opentelemetry  # noqa: F401
        except ImportError:
            issues.append("opentelemetry not installed")
        if not settings.otel_endpoint:
            issues.append("otel_endpoint empty")
        if issues:
            return CheckResult(
                status="red",
                detail="OTel enabled but: " + "; ".join(issues),
                remediation="pip install 'walacor-gateway[telemetry]' and set WALACOR_OTEL_ENDPOINT",
                evidence={"issues": issues},
                elapsed_ms=elapsed,
            )
        return CheckResult(status="green", detail="OTel configured", elapsed_ms=elapsed)


class _Fea06WorkerRedis:
    id = "FEA-06"
    name = "Worker/Redis coherence"
    category = Category.feature
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if settings.uvicorn_workers > 1 and not settings.redis_url:
            return CheckResult(
                status="red",
                detail=f"uvicorn_workers={settings.uvicorn_workers} but redis_url empty — session chain and budget will desync",
                remediation="Set WALACOR_REDIS_URL or drop uvicorn_workers to 1",
                evidence={"workers": settings.uvicorn_workers, "redis_url_set": False},
                elapsed_ms=elapsed,
            )
        return CheckResult(status="green", detail="Worker/Redis coherent", elapsed_ms=elapsed)


class _Fea07Intelligence:
    id = "FEA-07"
    name = "Intelligence"
    category = Category.feature
    severity = Severity.ops

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        settings = get_settings()
        elapsed = int((time.monotonic() - t0) * 1000)

        if not settings.intelligence_enabled:
            return CheckResult(status="green", detail="Intelligence disabled", elapsed_ms=elapsed)

        issues = []
        if getattr(ctx, "model_registry", None) is None:
            issues.append("model registry not populated")
        if getattr(ctx, "intelligence_db", None) is None:
            issues.append("intelligence DB not available")
        if issues:
            return CheckResult(
                status="red",
                detail="Intelligence enabled but: " + "; ".join(issues),
                evidence={"issues": issues},
                elapsed_ms=elapsed,
            )
        return CheckResult(status="green", detail="Intelligence registry + DB available", elapsed_ms=elapsed)


register(_Fea01LlamaGuard())
register(_Fea02WebSearch())
register(_Fea03Presidio())
register(_Fea04PromptGuard())
register(_Fea05OTel())
register(_Fea06WorkerRedis())
register(_Fea07Intelligence())
