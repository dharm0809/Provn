# src/gateway/adaptive/startup_probes.py
"""Built-in startup probes — validate environment before accepting traffic.

All probes are fail-open: failures log warnings but never prevent startup.
Custom probes can be registered via WALACOR_CUSTOM_STARTUP_PROBES config.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from gateway.adaptive import load_custom_class, parse_custom_paths
from gateway.adaptive.interfaces import ProbeResult, StartupProbe

logger = logging.getLogger(__name__)


class ProviderHealthProbe(StartupProbe):
    """Ping each configured LLM provider to verify connectivity."""

    async def check(self, http_client: Any, settings: Any) -> ProbeResult:
        results: dict[str, Any] = {}
        probes = []
        if settings.provider_ollama_url:
            probes.append(("ollama", f"{settings.provider_ollama_url}/api/tags", {}))
        if settings.provider_openai_key:
            url = getattr(settings, "provider_openai_url", "https://api.openai.com")
            probes.append(("openai", f"{url}/v1/models",
                           {"Authorization": f"Bearer {settings.provider_openai_key}"}))
        if settings.provider_anthropic_key:
            probes.append(("anthropic", "https://api.anthropic.com/v1/models",
                           {"x-api-key": settings.provider_anthropic_key,
                            "anthropic-version": "2023-06-01"}))
        if not probes:
            return ProbeResult(name="provider_health", healthy=True, detail={})

        async def _ping(name: str, url: str, headers: dict) -> tuple[str, dict]:
            try:
                resp = await http_client.get(url, headers=headers, timeout=5.0)
                return name, {"ok": resp.status_code < 400, "status": resp.status_code}
            except Exception as e:
                return name, {"ok": False, "error": str(e)[:200]}

        tasks = [_ping(n, u, h) for n, u, h in probes]
        for coro in asyncio.as_completed(tasks):
            name, result = await coro
            results[name] = result

        any_ok = any(r.get("ok") for r in results.values())
        if not any_ok:
            logger.critical("No LLM providers reachable: %s", results)
        else:
            for name, r in results.items():
                if not r.get("ok"):
                    logger.warning("Provider %s unreachable: %s", name, r)
                else:
                    logger.info("Provider %s healthy", name)

        return ProbeResult(name="provider_health", healthy=any_ok, detail=results)


class RoutingEndpointProbe(StartupProbe):
    """Validate that all model routing endpoints are reachable."""

    async def check(self, http_client: Any, settings: Any) -> ProbeResult:
        routes = getattr(settings, "model_routes", None) or []
        if not routes:
            return ProbeResult(name="routing_endpoints", healthy=True, detail={})

        results: dict[str, Any] = {}
        for route in routes:
            pattern = route.get("pattern", "?")
            url = route.get("url", "")
            if not url:
                results[pattern] = {"ok": False, "error": "no url configured"}
                continue
            try:
                test_url = url.rstrip("/")
                if "/v1" not in test_url:
                    test_url += "/api/tags"
                resp = await http_client.get(test_url, timeout=5.0)
                results[pattern] = {"ok": resp.status_code < 500, "status": resp.status_code}
            except Exception as e:
                results[pattern] = {"ok": False, "error": str(e)[:200]}

        unreachable = [k for k, v in results.items() if not v.get("ok")]
        if unreachable:
            logger.warning("Unreachable routing endpoints: %s", unreachable)

        return ProbeResult(
            name="routing_endpoints", healthy=len(unreachable) == 0, detail=results)


class DiskSpaceProbe(StartupProbe):
    """Check WAL directory free space and auto-scale WAL limits."""

    async def check(self, http_client: Any, settings: Any) -> ProbeResult:
        try:
            usage = shutil.disk_usage(settings.wal_path)
        except OSError as e:
            logger.warning("Cannot check disk space for %s: %s", settings.wal_path, e)
            return ProbeResult(name="disk_space", healthy=False,
                               detail={"error": str(e)[:200]})

        free_pct = round((usage.free / usage.total) * 100, 1)
        free_gb = round(usage.free / (1024 ** 3), 2)
        auto_max_gb = round(min(usage.free * 0.8 / (1024 ** 3), settings.wal_max_size_gb), 2)
        healthy = free_pct > settings.disk_min_free_percent

        if not healthy:
            logger.critical("WAL disk critically low: %.1f%% free (%s)", free_pct, settings.wal_path)
        elif free_pct < 15:
            logger.warning("WAL disk space low: %.1f%% free (%s)", free_pct, settings.wal_path)

        return ProbeResult(
            name="disk_space", healthy=healthy,
            detail={"free_pct": free_pct, "free_gb": free_gb, "auto_max_gb": auto_max_gb})


class APIVersionProbe(StartupProbe):
    """Detect provider API versions for compatibility awareness."""

    async def check(self, http_client: Any, settings: Any) -> ProbeResult:
        results: dict[str, Any] = {}
        if settings.provider_ollama_url:
            try:
                resp = await http_client.get(
                    f"{settings.provider_ollama_url}/api/version", timeout=5.0)
                if resp.status_code == 200:
                    data = resp.json()
                    results["ollama_version"] = data.get("version", "unknown")
                    logger.info("Ollama version: %s", results["ollama_version"])
            except Exception:
                pass
        return ProbeResult(name="api_version", healthy=True, detail=results)


async def run_startup_probes(
    http_client: Any, settings: Any,
) -> dict[str, ProbeResult]:
    """Run all registered startup probes concurrently. Never raises."""
    probes: list[StartupProbe] = []
    if getattr(settings, "provider_health_check_on_startup", True):
        probes.extend([
            ProviderHealthProbe(),
            RoutingEndpointProbe(),
            DiskSpaceProbe(),
            APIVersionProbe(),
        ])
    custom_paths = parse_custom_paths(getattr(settings, "custom_startup_probes", ""))
    for path in custom_paths:
        try:
            cls = load_custom_class(path)
            probes.append(cls())
        except Exception as e:
            logger.warning("Failed to load custom probe %s: %s", path, e)

    results: dict[str, ProbeResult] = {}
    coros = []
    for probe in probes:
        coros.append(_safe_probe(probe, http_client, settings))
    done = await asyncio.gather(*coros)
    for r in done:
        if isinstance(r, ProbeResult):
            results[r.name] = r
    return results


async def _safe_probe(probe: StartupProbe, http_client: Any, settings: Any) -> ProbeResult:
    """Run a single probe with exception safety."""
    try:
        return await asyncio.wait_for(probe.check(http_client, settings), timeout=10.0)
    except Exception as e:
        name = type(probe).__name__
        logger.warning("Startup probe %s failed: %s", name, e)
        return ProbeResult(name=name, healthy=False, detail={"error": str(e)[:200]})
