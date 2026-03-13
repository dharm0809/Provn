# Phase 23: Adaptive Gateway Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the Gateway self-configuring, runtime-adaptive, and enterprise-extensible by adding startup probes, smart request classification, configurable content policies, and runtime adaptation — with zero new dependencies.

**Architecture:** New `src/gateway/adaptive/` package with 5 ABCs (StartupProbe, RequestClassifier, CapabilityProbe, IdentityValidator, ResourceMonitor). Each has a default implementation and can be overridden via config. Control plane gets 2 new tables for content policies and provider status. All probes/monitors fail-open.

**Tech Stack:** Python 3.12, Starlette, Pydantic Settings, SQLite (existing), httpx (existing), shutil (stdlib), importlib (stdlib)

**Design doc:** `docs/plans/2026-03-13-adaptive-gateway-design.md`

---

## Task 1: Adaptive Package Foundation — Interfaces + Loader

**Files:**
- Create: `src/gateway/adaptive/__init__.py`
- Create: `src/gateway/adaptive/interfaces.py`
- Test: `tests/unit/test_adaptive_interfaces.py`

### Step 1: Write failing tests

```python
# tests/unit/test_adaptive_interfaces.py
"""Tests for adaptive package interfaces and loader utility."""
import pytest
from gateway.adaptive import load_custom_class
from gateway.adaptive.interfaces import (
    StartupProbe, RequestClassifier, IdentityValidator,
    ResourceMonitor, ProbeResult, ValidationResult, ResourceStatus,
)


def test_load_custom_class_valid():
    cls = load_custom_class("gateway.adaptive.interfaces.StartupProbe")
    assert cls is StartupProbe


def test_load_custom_class_invalid_module():
    with pytest.raises((ModuleNotFoundError, ImportError)):
        load_custom_class("nonexistent.module.Class")


def test_load_custom_class_invalid_class():
    with pytest.raises(AttributeError):
        load_custom_class("gateway.adaptive.interfaces.NonexistentClass")


def test_probe_result_dataclass():
    r = ProbeResult(name="test", healthy=True, results={"key": "val"})
    assert r.name == "test"
    assert r.healthy is True
    assert r.results == {"key": "val"}


def test_validation_result_dataclass():
    r = ValidationResult(valid=True, identity=None, source="jwt", warnings=[])
    assert r.valid is True
    assert r.warnings == []


def test_resource_status_dataclass():
    r = ResourceStatus(disk_free_pct=42.5, disk_healthy=True,
                       active_requests=3, provider_error_rates={})
    assert r.disk_free_pct == 42.5


def test_startup_probe_is_abstract():
    with pytest.raises(TypeError):
        StartupProbe()


def test_request_classifier_is_abstract():
    with pytest.raises(TypeError):
        RequestClassifier()


def test_identity_validator_is_abstract():
    with pytest.raises(TypeError):
        IdentityValidator()


def test_resource_monitor_is_abstract():
    with pytest.raises(TypeError):
        ResourceMonitor()
```

### Step 2: Run tests to verify they fail

Run: `python -m pytest tests/unit/test_adaptive_interfaces.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.adaptive'`

### Step 3: Write the implementation

```python
# src/gateway/adaptive/__init__.py
"""Adaptive Gateway — self-configuring intelligence layer.

Enterprise extension: implement any ABC from interfaces.py and register
via WALACOR_CUSTOM_*_PROBES config (comma-separated Python dotted paths).
"""
from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)


def load_custom_class(dotted_path: str) -> type:
    """Import a class by dotted path, e.g. 'mycompany.probes.DatadogProbe'."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def parse_custom_paths(csv: str) -> list[str]:
    """Parse comma-separated dotted paths, stripping whitespace."""
    return [p.strip() for p in csv.split(",") if p.strip()]
```

```python
# src/gateway/adaptive/interfaces.py
"""Abstract base classes for all adaptive gateway components.

Every decision point in the gateway has a documented interface that
enterprises can override without forking. Implement any ABC below
and register via WALACOR_CUSTOM_* config fields.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProbeResult:
    """Result of a startup probe check."""
    name: str
    healthy: bool
    results: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """Result of identity cross-validation."""
    valid: bool
    identity: Any  # CallerIdentity or None
    source: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResourceStatus:
    """Snapshot of system resource health."""
    disk_free_pct: float
    disk_healthy: bool
    active_requests: int
    provider_error_rates: dict[str, float] = field(default_factory=dict)


# ── Abstract Base Classes ─────────────────────────────────────────────────────

class StartupProbe(ABC):
    """Runs at gateway startup to validate environment readiness.

    Contract:
    - Must complete within 10 seconds
    - Must never raise — return ProbeResult(healthy=False) on failure
    - Results exposed in /health endpoint
    """

    @abstractmethod
    async def check(self, http_client: Any, settings: Any) -> ProbeResult: ...


class RequestClassifier(ABC):
    """Classifies incoming requests by type.

    Contract:
    - Must be synchronous (called in hot path)
    - Return value stored in metadata.request_type
    - "user_message" is the default; any other value is a system/synthetic task
    """

    @abstractmethod
    def classify(self, prompt: str, headers: dict[str, str],
                 body: dict[str, Any]) -> str: ...


class CapabilityProbe(ABC):
    """Discovers model capabilities at runtime.

    Contract:
    - Async, may make HTTP calls to providers
    - Return dict of capability_key → value
    - Must handle timeouts gracefully (return empty dict)
    """

    @abstractmethod
    async def probe(self, model_id: str, provider: str,
                    http_client: Any) -> dict[str, Any]: ...


class IdentityValidator(ABC):
    """Validates caller identity consistency across auth sources.

    Contract:
    - Synchronous (called in middleware hot path)
    - JWT identity takes priority over header identity on conflict
    - Mismatches are warnings, not errors (fail-open)
    """

    @abstractmethod
    def validate(self, jwt_identity: Any, header_identity: Any,
                 request: Any) -> ValidationResult: ...


class ResourceMonitor(ABC):
    """Monitors system resources and reports health status.

    Contract:
    - Async (may perform I/O like disk checks)
    - Called periodically by background task
    - Results fed into /health endpoint and routing decisions
    """

    @abstractmethod
    async def check(self) -> ResourceStatus: ...
```

### Step 4: Run tests to verify they pass

Run: `python -m pytest tests/unit/test_adaptive_interfaces.py -v`
Expected: 10 PASS

### Step 5: Commit

```bash
git add src/gateway/adaptive/ tests/unit/test_adaptive_interfaces.py
git commit -m "feat(adaptive): add package foundation with 5 ABCs and class loader"
```

---

## Task 2: Config Fields for Phase 23

**Files:**
- Modify: `src/gateway/config.py` (after line 331, before validators)
- Modify: `src/gateway/pipeline/context.py` (add new fields to PipelineContext)

### Step 1: Add config fields

Add after the last config field (line 331 `otel_service_name`) and before `@property` at line 334:

```python
    # ── Phase 23: Adaptive Gateway ────────────────────────────────────────────
    startup_probes_enabled: bool = Field(default=True, description="Run startup probes (provider health, disk, routing)")
    provider_health_check_on_startup: bool = Field(default=True, description="Ping providers at startup")
    capability_probe_ttl_seconds: int = Field(default=86400, description="Re-probe model capabilities after this many seconds")
    identity_validation_enabled: bool = Field(default=True, description="Cross-validate JWT claims against headers")
    disk_monitor_enabled: bool = Field(default=True, description="Monitor WAL disk space")
    disk_min_free_percent: float = Field(default=5.0, description="Minimum free disk % before warning")
    resource_monitor_interval_seconds: int = Field(default=60, description="Resource monitor check interval")
    # Enterprise extension points (comma-separated Python dotted class paths)
    custom_startup_probes: str = Field(default="", description="Custom StartupProbe classes")
    custom_request_classifiers: str = Field(default="", description="Custom RequestClassifier classes")
    custom_identity_validators: str = Field(default="", description="Custom IdentityValidator classes")
    custom_resource_monitors: str = Field(default="", description="Custom ResourceMonitor classes")
```

### Step 2: Add PipelineContext fields

Add to `PipelineContext.__init__()` in `src/gateway/pipeline/context.py` after the last field assignment:

```python
        # Phase 23: Adaptive Gateway
        self.startup_probe_results: dict = {}
        self.request_classifier = None   # set in on_startup
        self.identity_validator = None   # set in on_startup
        self.resource_monitor = None     # set in on_startup
        self.capability_registry = None  # set in on_startup
        self.effective_wal_max_gb: float | None = None  # auto-scaled by DiskSpaceProbe
```

### Step 3: Verify existing tests still pass

Run: `python -m pytest tests/unit/ -x -q`
Expected: All existing tests pass (config changes are additive with defaults)

### Step 4: Commit

```bash
git add src/gateway/config.py src/gateway/pipeline/context.py
git commit -m "feat(adaptive): add config fields and PipelineContext slots for Phase 23"
```

---

## Task 3: Startup Probes — Provider Health + Disk + Routing

**Files:**
- Create: `src/gateway/adaptive/startup_probes.py`
- Test: `tests/unit/test_startup_probes.py`

### Step 1: Write failing tests

```python
# tests/unit/test_startup_probes.py
"""Tests for startup probe implementations."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from gateway.adaptive.startup_probes import (
    ProviderHealthProbe, RoutingEndpointProbe, DiskSpaceProbe,
    APIVersionProbe, run_startup_probes,
)
from gateway.adaptive.interfaces import ProbeResult

anyio_backend = ["asyncio"]


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.provider_ollama_url = "http://localhost:11434"
    s.provider_openai_key = ""
    s.provider_anthropic_key = ""
    s.provider_openai_url = "https://api.openai.com"
    s.model_routes = []
    s.wal_path = "/tmp"
    s.wal_max_size_gb = 10.0
    s.disk_min_free_percent = 5.0
    s.custom_startup_probes = ""
    s.provider_health_check_on_startup = True
    return s


@pytest.mark.anyio
async def test_provider_health_probe_ollama_reachable(mock_settings):
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    client.get = AsyncMock(return_value=resp)
    probe = ProviderHealthProbe()
    result = await probe.check(client, mock_settings)
    assert result.healthy is True
    assert "ollama" in result.results


@pytest.mark.anyio
async def test_provider_health_probe_ollama_down(mock_settings):
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("Connection refused"))
    probe = ProviderHealthProbe()
    result = await probe.check(client, mock_settings)
    assert result.results["ollama"]["ok"] is False


@pytest.mark.anyio
async def test_provider_health_probe_no_providers(mock_settings):
    mock_settings.provider_ollama_url = ""
    client = AsyncMock()
    probe = ProviderHealthProbe()
    result = await probe.check(client, mock_settings)
    assert result.healthy is True  # no providers = nothing to check


@pytest.mark.anyio
async def test_routing_endpoint_probe_no_routes(mock_settings):
    probe = RoutingEndpointProbe()
    result = await probe.check(AsyncMock(), mock_settings)
    assert result.healthy is True


@pytest.mark.anyio
async def test_routing_endpoint_probe_bad_url(mock_settings):
    mock_settings.model_routes = [{"pattern": "gpt-*", "url": "http://dead:1234"}]
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("unreachable"))
    probe = RoutingEndpointProbe()
    result = await probe.check(client, mock_settings)
    assert result.healthy is False


@pytest.mark.anyio
async def test_disk_space_probe_healthy():
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=50_000_000_000, used=50_000_000_000)
        probe = DiskSpaceProbe()
        settings = MagicMock(wal_path="/tmp", wal_max_size_gb=10.0, disk_min_free_percent=5.0)
        result = await probe.check(AsyncMock(), settings)
        assert result.healthy is True
        assert result.results["free_pct"] == 50.0


@pytest.mark.anyio
async def test_disk_space_probe_low_disk():
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=2_000_000_000, used=98_000_000_000)
        probe = DiskSpaceProbe()
        settings = MagicMock(wal_path="/tmp", wal_max_size_gb=10.0, disk_min_free_percent=5.0)
        result = await probe.check(AsyncMock(), settings)
        assert result.healthy is False
        assert result.results["free_pct"] == 2.0


@pytest.mark.anyio
async def test_disk_space_auto_scale_caps_at_configured_max():
    with patch("shutil.disk_usage") as mock_du:
        # 500GB free — auto_max should cap at configured 10GB
        mock_du.return_value = MagicMock(total=1_000_000_000_000, free=500_000_000_000, used=500_000_000_000)
        probe = DiskSpaceProbe()
        settings = MagicMock(wal_path="/tmp", wal_max_size_gb=10.0, disk_min_free_percent=5.0)
        result = await probe.check(AsyncMock(), settings)
        assert result.results["auto_max_gb"] == 10.0


@pytest.mark.anyio
async def test_api_version_probe_ollama(mock_settings):
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"version": "0.6.2"})
    client.get = AsyncMock(return_value=resp)
    probe = APIVersionProbe()
    result = await probe.check(client, mock_settings)
    assert result.results.get("ollama_version") == "0.6.2"


@pytest.mark.anyio
async def test_run_startup_probes_concurrent(mock_settings):
    client = AsyncMock()
    resp = MagicMock(status_code=200)
    resp.json = MagicMock(return_value={"version": "0.6.2"})
    client.get = AsyncMock(return_value=resp)
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=50_000_000_000, used=50_000_000_000)
        results = await run_startup_probes(client, mock_settings)
    assert "provider_health" in results
    assert "disk_space" in results


@pytest.mark.anyio
async def test_probe_exception_doesnt_crash():
    """A single probe failure must not crash the entire probe run."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("boom"))
    with patch("shutil.disk_usage", side_effect=OSError("no such dir")):
        settings = MagicMock(
            provider_ollama_url="http://bad:11434", provider_openai_key="",
            provider_anthropic_key="", model_routes=[], wal_path="/nonexistent",
            wal_max_size_gb=10.0, disk_min_free_percent=5.0,
            custom_startup_probes="", provider_health_check_on_startup=True)
        results = await run_startup_probes(client, settings)
    # Should still return results dict (probes failed gracefully)
    assert isinstance(results, dict)
```

### Step 2: Run tests to verify they fail

Run: `python -m pytest tests/unit/test_startup_probes.py -v`
Expected: FAIL — `ModuleNotFoundError`

### Step 3: Write implementation

```python
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
            return ProbeResult(name="provider_health", healthy=True, results={})

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

        return ProbeResult(name="provider_health", healthy=any_ok, results=results)


class RoutingEndpointProbe(StartupProbe):
    """Validate that all model routing endpoints are reachable."""

    async def check(self, http_client: Any, settings: Any) -> ProbeResult:
        routes = getattr(settings, "model_routes", None) or []
        if not routes:
            return ProbeResult(name="routing_endpoints", healthy=True, results={})

        results: dict[str, Any] = {}
        for route in routes:
            pattern = route.get("pattern", "?")
            url = route.get("url", "")
            if not url:
                results[pattern] = {"ok": False, "error": "no url configured"}
                continue
            try:
                # Try health-style endpoint — just check connectivity
                test_url = url.rstrip("/")
                if "/v1" not in test_url:
                    test_url += "/api/tags"  # Ollama-style
                resp = await http_client.get(test_url, timeout=5.0)
                results[pattern] = {"ok": resp.status_code < 500, "status": resp.status_code}
            except Exception as e:
                results[pattern] = {"ok": False, "error": str(e)[:200]}

        unreachable = [k for k, v in results.items() if not v.get("ok")]
        if unreachable:
            logger.warning("Unreachable routing endpoints: %s", unreachable)

        return ProbeResult(
            name="routing_endpoints", healthy=len(unreachable) == 0, results=results)


class DiskSpaceProbe(StartupProbe):
    """Check WAL directory free space and auto-scale WAL limits."""

    async def check(self, http_client: Any, settings: Any) -> ProbeResult:
        try:
            usage = shutil.disk_usage(settings.wal_path)
        except OSError as e:
            logger.warning("Cannot check disk space for %s: %s", settings.wal_path, e)
            return ProbeResult(name="disk_space", healthy=False,
                               results={"error": str(e)[:200]})

        free_pct = round((usage.free / usage.total) * 100, 1)
        free_gb = round(usage.free / (1024 ** 3), 2)
        # Auto-scale: 80% of free space, capped at configured max
        auto_max_gb = round(min(usage.free * 0.8 / (1024 ** 3), settings.wal_max_size_gb), 2)
        healthy = free_pct > settings.disk_min_free_percent

        if not healthy:
            logger.critical("WAL disk critically low: %.1f%% free (%s)", free_pct, settings.wal_path)
        elif free_pct < 15:
            logger.warning("WAL disk space low: %.1f%% free (%s)", free_pct, settings.wal_path)

        return ProbeResult(
            name="disk_space", healthy=healthy,
            results={"free_pct": free_pct, "free_gb": free_gb, "auto_max_gb": auto_max_gb})


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
                pass  # version detection is best-effort
        return ProbeResult(name="api_version", healthy=True, results=results)


# ── Probe Runner ──────────────────────────────────────────────────────────────

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
    # Load custom probes
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
        return ProbeResult(name=name, healthy=False, results={"error": str(e)[:200]})
```

### Step 4: Run tests

Run: `python -m pytest tests/unit/test_startup_probes.py -v`
Expected: 12 PASS

### Step 5: Commit

```bash
git add src/gateway/adaptive/startup_probes.py tests/unit/test_startup_probes.py
git commit -m "feat(adaptive): add startup probes — provider health, disk, routing, API version"
```

---

## Task 4: Integrate Startup Probes into Main

**Files:**
- Modify: `src/gateway/main.py` (add `_run_startup_probes()` call in `on_startup()`)
- Modify: `src/gateway/health.py` (expose probe results)

### Step 1: Add startup probes call to `main.py`

In `on_startup()`, add after `_init_load_balancer()` (around line 660) and before `_self_test()` (line 662):

```python
    # Phase 23: Startup probes (provider health, disk, routing)
    if settings.startup_probes_enabled:
        from gateway.adaptive.startup_probes import run_startup_probes
        ctx.startup_probe_results = await run_startup_probes(ctx.http_client, settings)
        # Apply disk auto-scaling
        disk_probe = ctx.startup_probe_results.get("disk_space")
        if disk_probe and disk_probe.results.get("auto_max_gb") is not None:
            ctx.effective_wal_max_gb = disk_probe.results["auto_max_gb"]
            logger.info("WAL max size auto-scaled to %.2f GB", ctx.effective_wal_max_gb)
```

### Step 2: Add probe results to health endpoint

In `health.py`, after the `model_capabilities` block (around line 90), add:

```python
    if ctx.startup_probe_results:
        status["startup_probes"] = {
            name: {"healthy": r.healthy, **r.results}
            for name, r in ctx.startup_probe_results.items()
        }
```

### Step 3: Verify tests pass

Run: `python -m pytest tests/unit/ -x -q`
Expected: All tests pass

### Step 4: Commit

```bash
git add src/gateway/main.py src/gateway/health.py
git commit -m "feat(adaptive): integrate startup probes into lifecycle and health endpoint"
```

---

## Task 5: Request Classifier — Body + Header + Prompt Detection

**Files:**
- Create: `src/gateway/adaptive/request_classifier.py`
- Test: `tests/unit/test_request_classifier.py`

### Step 1: Write failing tests

```python
# tests/unit/test_request_classifier.py
"""Tests for smart request classification."""
import pytest
from gateway.adaptive.request_classifier import DefaultRequestClassifier


@pytest.fixture
def classifier():
    return DefaultRequestClassifier()


class TestBodyTaskDetection:
    def test_title_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "title_generation"}) == "system_task:title_generation"

    def test_tags_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "tags_generation"}) == "system_task:tags_generation"

    def test_query_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "query_generation"}) == "system_task:query_generation"

    def test_emoji_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "emoji_generation"}) == "system_task:emoji_generation"

    def test_follow_up_generation(self, classifier):
        assert classifier.classify("", {}, {"task": "follow_up_generation"}) == "system_task:follow_up_generation"

    def test_metadata_task(self, classifier):
        body = {"metadata": {"task": "title_generation"}}
        assert classifier.classify("", {}, body) == "system_task:title_generation"


class TestSyntheticDetection:
    def test_curl(self, classifier):
        assert classifier.classify("hello", {"user-agent": "curl/8.1.2"}, {}) == "synthetic"

    def test_httpie(self, classifier):
        assert classifier.classify("hello", {"user-agent": "HTTPie/3.2"}, {}) == "synthetic"

    def test_python_requests(self, classifier):
        assert classifier.classify("hi", {"user-agent": "python-requests/2.31"}, {}) == "synthetic"

    def test_python_httpx(self, classifier):
        assert classifier.classify("hi", {"user-agent": "python-httpx/0.28"}, {}) == "synthetic"

    def test_k6_load_tester(self, classifier):
        assert classifier.classify("hi", {"user-agent": "k6/0.50"}, {}) == "synthetic"

    def test_real_browser_not_synthetic(self, classifier):
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        assert classifier.classify("hi", {"user-agent": ua}, {}) == "user_message"


class TestPromptFallback:
    def test_title_generation_prompt(self, classifier):
        prompt = "Generate a concise title for this conversation"
        assert classifier.classify(prompt, {}, {}).startswith("system_task:")

    def test_follow_up_prompt(self, classifier):
        prompt = "Generate 3 follow-up questions based on the response"
        assert classifier.classify(prompt, {}, {}).startswith("system_task:")

    def test_autocomplete_prompt(self, classifier):
        prompt = "### Task: You are an autocompletion system. Continue the text"
        assert classifier.classify(prompt, {}, {}).startswith("system_task")

    def test_normal_user_message(self, classifier):
        assert classifier.classify("What is quantum computing?", {}, {}) == "user_message"

    def test_empty_prompt(self, classifier):
        assert classifier.classify("", {}, {}) == "user_message"


class TestPriority:
    def test_body_takes_priority_over_prompt(self, classifier):
        """Body task field wins over prompt-based detection."""
        result = classifier.classify(
            "What is AI?",  # looks like user_message
            {},
            {"task": "title_generation"})  # but body says system task
        assert result == "system_task:title_generation"

    def test_body_takes_priority_over_synthetic_ua(self, classifier):
        """Body task field wins over user-agent detection."""
        result = classifier.classify(
            "", {"user-agent": "curl/8.1"}, {"task": "title_generation"})
        assert result == "system_task:title_generation"
```

### Step 2: Run tests to verify they fail

Run: `python -m pytest tests/unit/test_request_classifier.py -v`
Expected: FAIL

### Step 3: Write implementation

```python
# src/gateway/adaptive/request_classifier.py
"""Smart request classification — body > headers > prompt.

Detects OpenWebUI background tasks, synthetic traffic (curl/k6/etc.),
and falls back to prompt-based regex for backward compatibility.
"""
from __future__ import annotations

import re
import logging
from typing import Any

from gateway.adaptive.interfaces import RequestClassifier

logger = logging.getLogger(__name__)


class DefaultRequestClassifier(RequestClassifier):
    """Multi-source request classifier with priority: body > headers > prompt."""

    # OpenWebUI sends these as the "task" field in the request body
    _BODY_TASK_TYPES = frozenset({
        "title_generation", "tags_generation", "query_generation",
        "emoji_generation", "follow_up_generation",
    })

    # User-agent substrings indicating synthetic/testing traffic
    _SYNTHETIC_UA = (
        "curl/", "httpie/", "python-requests/", "python-httpx/",
        "k6/", "artillery/", "wrk", "ab/", "siege/", "vegeta",
    )

    # Prompt-based fallback patterns (kept for backward compat with
    # older OpenWebUI versions or other clients)
    _PROMPT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        ("title_generation", re.compile(
            r"generate a (?:concise|brief|short).*?title", re.IGNORECASE)),
        ("autocomplete", re.compile(
            r"### Task:.*?autocompletion system", re.IGNORECASE | re.DOTALL)),
        ("follow_up", re.compile(
            r"generate (?:\d+ )?(?:follow[- ]?up|suggested|relevant).*?question",
            re.IGNORECASE)),
        ("tag_generation", re.compile(
            r"generate (?:\d+ )?(?:concise )?tags?\b", re.IGNORECASE)),
        ("emoji_generation", re.compile(
            r"generate (?:a single |an? )?emoji", re.IGNORECASE)),
        ("search_query", re.compile(
            r"generate (?:a )?search query", re.IGNORECASE)),
    ]

    def classify(self, prompt: str, headers: dict[str, str],
                 body: dict[str, Any]) -> str:
        # Priority 1: explicit task field in request body
        task = body.get("task")
        if task and task in self._BODY_TASK_TYPES:
            return f"system_task:{task}"

        # Also check metadata.task (some OpenWebUI versions nest it)
        metadata = body.get("metadata")
        if isinstance(metadata, dict):
            meta_task = metadata.get("task")
            if meta_task and meta_task in self._BODY_TASK_TYPES:
                return f"system_task:{meta_task}"

        # Priority 2: synthetic traffic detection via user-agent
        ua = headers.get("user-agent", "").lower()
        if any(s in ua for s in self._SYNTHETIC_UA):
            return "synthetic"

        # Priority 3: prompt-based fallback (regex)
        if prompt:
            text = prompt[:1000]
            for task_type, pattern in self._PROMPT_PATTERNS:
                if pattern.search(text):
                    return f"system_task:{task_type}"
            if text.lstrip().startswith("### Task:"):
                return "system_task"

        return "user_message"
```

### Step 4: Run tests

Run: `python -m pytest tests/unit/test_request_classifier.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add src/gateway/adaptive/request_classifier.py tests/unit/test_request_classifier.py
git commit -m "feat(adaptive): smart request classifier with body/header/prompt detection"
```

---

## Task 6: Identity Validator — JWT↔Header Cross-Check

**Files:**
- Create: `src/gateway/adaptive/identity_validator.py`
- Test: `tests/unit/test_identity_validator.py`

### Step 1: Write failing tests

```python
# tests/unit/test_identity_validator.py
"""Tests for identity cross-validation."""
import pytest
from unittest.mock import MagicMock
from gateway.adaptive.identity_validator import DefaultIdentityValidator
from gateway.auth.identity import CallerIdentity


@pytest.fixture
def validator():
    return DefaultIdentityValidator()


def _make_request(headers=None):
    req = MagicMock()
    req.headers = headers or {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def test_no_jwt_returns_header_identity(validator):
    header_id = CallerIdentity(user_id="alice", source="header_unverified")
    result = validator.validate(None, header_id, _make_request())
    assert result.valid is True
    assert result.identity.user_id == "alice"
    assert result.source == "header_unverified"


def test_no_jwt_no_header_returns_none(validator):
    result = validator.validate(None, None, _make_request())
    assert result.valid is True
    assert result.identity is None


def test_jwt_wins_over_header(validator):
    jwt_id = CallerIdentity(user_id="bob", email="bob@co.com", source="jwt")
    header_id = CallerIdentity(user_id="alice", source="header_unverified")
    result = validator.validate(jwt_id, header_id, _make_request({"x-user-id": "alice"}))
    assert result.identity.user_id == "bob"
    assert result.source == "jwt_verified"


def test_jwt_header_match_no_warnings(validator):
    jwt_id = CallerIdentity(user_id="bob", source="jwt")
    result = validator.validate(jwt_id, None, _make_request({"x-user-id": "bob"}))
    assert result.valid is True
    assert len(result.warnings) == 0


def test_jwt_header_mismatch_warning(validator):
    jwt_id = CallerIdentity(user_id="bob", source="jwt")
    result = validator.validate(jwt_id, None, _make_request({"x-user-id": "alice"}))
    assert result.valid is False
    assert len(result.warnings) == 1
    assert "alice" in result.warnings[0]
    assert "bob" in result.warnings[0]


def test_jwt_no_header_user_id_no_warning(validator):
    jwt_id = CallerIdentity(user_id="bob", source="jwt")
    result = validator.validate(jwt_id, None, _make_request({}))
    assert result.valid is True
    assert len(result.warnings) == 0


def test_merge_fills_gaps_from_header(validator):
    jwt_id = CallerIdentity(user_id="bob", email="", roles=[], source="jwt")
    header_id = CallerIdentity(user_id="bob", email="bob@co.com",
                                roles=["admin"], team="eng", source="header_unverified")
    result = validator.validate(jwt_id, header_id, _make_request())
    assert result.identity.email == "bob@co.com"
    assert result.identity.roles == ["admin"]
    assert result.identity.team == "eng"
```

### Step 2: Run tests to verify they fail

Run: `python -m pytest tests/unit/test_identity_validator.py -v`

### Step 3: Write implementation

```python
# src/gateway/adaptive/identity_validator.py
"""Identity cross-validation — JWT claims vs header-claimed identity.

When both JWT and headers provide identity, JWT always wins on conflict.
Mismatches are logged as warnings and included in audit metadata but
do not block requests (fail-open).
"""
from __future__ import annotations

import logging
from typing import Any

from gateway.adaptive.interfaces import IdentityValidator, ValidationResult
from gateway.auth.identity import CallerIdentity

logger = logging.getLogger(__name__)


class DefaultIdentityValidator(IdentityValidator):
    """Cross-check header-claimed identity against JWT-proven identity."""

    def validate(self, jwt_identity: CallerIdentity | None,
                 header_identity: CallerIdentity | None,
                 request: Any) -> ValidationResult:
        # No JWT — return header identity as-is (unverified)
        if jwt_identity is None:
            return ValidationResult(
                valid=True, identity=header_identity,
                source="header_unverified" if header_identity else "none",
                warnings=[])

        # JWT present — cross-check headers
        warnings: list[str] = []
        headers = getattr(request, "headers", {})
        header_user = headers.get("x-user-id", "").strip()

        if header_user and header_user != jwt_identity.user_id:
            warnings.append(
                f"X-User-Id '{header_user}' does not match "
                f"JWT sub '{jwt_identity.user_id}'")
            client_ip = ""
            if hasattr(request, "client") and request.client:
                client_ip = request.client.host
            logger.warning(
                "Identity mismatch: header=%s jwt=%s ip=%s",
                header_user, jwt_identity.user_id, client_ip)

        # Merge: JWT fields take priority, headers fill gaps
        merged = CallerIdentity(
            user_id=jwt_identity.user_id,
            email=jwt_identity.email or (
                header_identity.email if header_identity else ""),
            roles=jwt_identity.roles or (
                header_identity.roles if header_identity else []),
            team=jwt_identity.team or (
                header_identity.team if header_identity else None),
            source="jwt_verified",
        )

        return ValidationResult(
            valid=len(warnings) == 0,
            identity=merged,
            source="jwt_verified",
            warnings=warnings)
```

### Step 4: Run tests

Run: `python -m pytest tests/unit/test_identity_validator.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add src/gateway/adaptive/identity_validator.py tests/unit/test_identity_validator.py
git commit -m "feat(adaptive): identity validator with JWT↔header cross-check"
```

---

## Task 7: Integrate Request Classifier + Identity Validator into Pipeline

**Files:**
- Modify: `src/gateway/main.py` (init classifier + validator in `on_startup()`)
- Modify: `src/gateway/pipeline/orchestrator.py` (use `ctx.request_classifier`)
- Modify: `src/gateway/main.py` (use `ctx.identity_validator` in middleware)

### Step 1: Init classifier and validator in `on_startup()`

Add after the startup probes block in `main.py on_startup()`:

```python
    # Phase 23: Request classifier + identity validator
    from gateway.adaptive.request_classifier import DefaultRequestClassifier
    from gateway.adaptive.identity_validator import DefaultIdentityValidator
    ctx.request_classifier = DefaultRequestClassifier()
    ctx.identity_validator = DefaultIdentityValidator()
    # Load custom implementations if configured
    if settings.custom_request_classifiers:
        from gateway.adaptive import load_custom_class, parse_custom_paths
        paths = parse_custom_paths(settings.custom_request_classifiers)
        if paths:
            cls = load_custom_class(paths[0])  # use first custom classifier
            ctx.request_classifier = cls()
    if settings.custom_identity_validators:
        from gateway.adaptive import load_custom_class, parse_custom_paths
        paths = parse_custom_paths(settings.custom_identity_validators)
        if paths:
            cls = load_custom_class(paths[0])
            ctx.identity_validator = cls()
```

### Step 2: Replace `_classify_request_type()` usage in orchestrator.py

Replace the classification block in `handle_request()` (currently around lines 1451-1458):

```python
    # Classify request type: prefer metadata from OpenWebUI filter plugin,
    # fall back to multi-source adaptive classifier.
    _meta_rt = call.metadata.get("request_type")
    if _meta_rt:
        extra["request_type"] = _meta_rt
    elif ctx.request_classifier:
        body_dict = {}
        try:
            body_dict = json.loads(call.raw_body) if call.raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        extra["request_type"] = ctx.request_classifier.classify(
            call.prompt_text or "", dict(request.headers), body_dict)
    else:
        extra["request_type"] = _classify_request_type(call.prompt_text or "")
```

### Step 3: Add identity validation to `api_key_middleware()` in main.py

After the existing JWT + header identity resolution (around line 140), add:

```python
    # Phase 23: Cross-validate identity sources
    if settings.identity_validation_enabled and ctx.identity_validator:
        jwt_id = getattr(request.state, "jwt_identity", None)
        header_id = getattr(request.state, "header_identity", None)
        val_result = ctx.identity_validator.validate(jwt_id, header_id, request)
        if val_result.identity:
            request.state.caller_identity = val_result.identity
        if val_result.warnings:
            request.state.identity_warnings = val_result.warnings
```

Note: This requires storing jwt_identity and header_identity separately on request.state during the existing auth flow. Add `request.state.jwt_identity = identity` after `_try_jwt_auth()` and `request.state.header_identity = identity` after `_resolve_header_identity_fallback()`.

### Step 4: Run full test suite

Run: `python -m pytest tests/unit/ -x -q`
Expected: All pass

### Step 5: Commit

```bash
git add src/gateway/main.py src/gateway/pipeline/orchestrator.py
git commit -m "feat(adaptive): integrate request classifier and identity validator into pipeline"
```

---

## Task 8: Content Policy Engine — Control Plane Table + CRUD

**Files:**
- Modify: `src/gateway/control/store.py` (add `content_policies` table + CRUD)
- Modify: `src/gateway/control/api.py` (add 3 endpoints + refresh hook)
- Test: `tests/unit/test_content_policies.py`

### Step 1: Write failing tests

```python
# tests/unit/test_content_policies.py
"""Tests for content policy CRUD in control plane store."""
import pytest
import tempfile
import os
from gateway.control.store import ControlPlaneStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as td:
        s = ControlPlaneStore(os.path.join(td, "test.db"))
        yield s


def test_upsert_content_policy(store):
    p = store.upsert_content_policy(
        tenant_id="t1", analyzer_id="walacor.pii.v1",
        category="credit_card", action="block")
    assert p["analyzer_id"] == "walacor.pii.v1"
    assert p["action"] == "block"
    assert "id" in p


def test_upsert_content_policy_idempotent(store):
    p1 = store.upsert_content_policy(
        tenant_id="t1", analyzer_id="walacor.pii.v1",
        category="credit_card", action="block")
    p2 = store.upsert_content_policy(
        tenant_id="t1", analyzer_id="walacor.pii.v1",
        category="credit_card", action="warn")
    assert p1["id"] == p2["id"]
    assert p2["action"] == "warn"


def test_list_content_policies(store):
    store.upsert_content_policy("t1", "walacor.pii.v1", "credit_card", "block")
    store.upsert_content_policy("t1", "walacor.pii.v1", "ssn", "block")
    store.upsert_content_policy("t1", "walacor.toxicity.v1", "self_harm", "warn")
    policies = store.list_content_policies()
    assert len(policies) == 3


def test_list_content_policies_by_analyzer(store):
    store.upsert_content_policy("t1", "walacor.pii.v1", "credit_card", "block")
    store.upsert_content_policy("t1", "walacor.toxicity.v1", "self_harm", "warn")
    policies = store.list_content_policies(analyzer_id="walacor.pii.v1")
    assert len(policies) == 1
    assert policies[0]["category"] == "credit_card"


def test_delete_content_policy(store):
    p = store.upsert_content_policy("t1", "walacor.pii.v1", "credit_card", "block")
    assert store.delete_content_policy(p["id"]) is True
    assert len(store.list_content_policies()) == 0


def test_delete_nonexistent_policy(store):
    assert store.delete_content_policy("nonexistent") is False


def test_seed_defaults(store):
    store.seed_default_content_policies()
    policies = store.list_content_policies()
    # Should have defaults for PII (7 categories) + Llama Guard (14) + Toxicity (3)
    assert len(policies) > 10
    # Check S4 defaults to block
    s4 = [p for p in policies if p["category"] == "S4"]
    assert len(s4) == 1
    assert s4[0]["action"] == "block"


def test_seed_defaults_idempotent(store):
    store.seed_default_content_policies()
    count1 = len(store.list_content_policies())
    store.seed_default_content_policies()
    count2 = len(store.list_content_policies())
    assert count1 == count2
```

### Step 2: Run tests to verify they fail

Run: `python -m pytest tests/unit/test_content_policies.py -v`

### Step 3: Add content_policies table to store.py

Add to `_SCHEMA_SQL` in `store.py` (after the budgets table):

```sql
CREATE TABLE IF NOT EXISTS content_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT '*',
    analyzer_id TEXT NOT NULL,
    category TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'warn',
    threshold REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, analyzer_id, category)
);
```

Add CRUD methods after the last method in `ControlPlaneStore`:

```python
    # ── Content Policies ──────────────────────────────────────────────────────

    def list_content_policies(self, analyzer_id: str | None = None) -> list[dict]:
        conn = self._ensure_conn()
        if analyzer_id:
            cur = conn.execute(
                "SELECT * FROM content_policies WHERE analyzer_id = ? ORDER BY category",
                (analyzer_id,))
        else:
            cur = conn.execute("SELECT * FROM content_policies ORDER BY analyzer_id, category")
        return [dict(row) for row in cur.fetchall()]

    def upsert_content_policy(self, tenant_id: str, analyzer_id: str,
                              category: str, action: str,
                              threshold: float = 0.5) -> dict:
        conn = self._ensure_conn()
        now = self._now()
        pid = self._new_id()
        cur = conn.execute(
            """INSERT INTO content_policies (id, tenant_id, analyzer_id, category, action, threshold, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, analyzer_id, category) DO UPDATE SET
                 action = excluded.action, threshold = excluded.threshold, updated_at = excluded.updated_at
               RETURNING *""",
            (pid, tenant_id, analyzer_id, category, action, threshold, now, now))
        conn.commit()
        return dict(cur.fetchone())

    def delete_content_policy(self, policy_id: str) -> bool:
        conn = self._ensure_conn()
        cur = conn.execute("DELETE FROM content_policies WHERE id = ?", (policy_id,))
        conn.commit()
        return cur.rowcount > 0

    def seed_default_content_policies(self) -> None:
        """Seed default content policies if table is empty."""
        existing = self.list_content_policies()
        if existing:
            return
        defaults = [
            # PII
            ("*", "walacor.pii.v1", "credit_card", "block"),
            ("*", "walacor.pii.v1", "ssn", "block"),
            ("*", "walacor.pii.v1", "aws_access_key", "block"),
            ("*", "walacor.pii.v1", "api_key", "block"),
            ("*", "walacor.pii.v1", "email_address", "warn"),
            ("*", "walacor.pii.v1", "phone_number", "warn"),
            ("*", "walacor.pii.v1", "ip_address", "warn"),
            # Llama Guard
            *[("*", "walacor.llama_guard.v1", f"S{i}",
               "block" if i == 4 else "warn") for i in range(1, 15)],
            # Toxicity
            ("*", "walacor.toxicity.v1", "child_safety", "block"),
            ("*", "walacor.toxicity.v1", "self_harm", "warn"),
            ("*", "walacor.toxicity.v1", "violence", "warn"),
        ]
        for tenant, analyzer, category, action in defaults:
            self.upsert_content_policy(tenant, analyzer, category, action)
```

### Step 4: Run tests

Run: `python -m pytest tests/unit/test_content_policies.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add src/gateway/control/store.py tests/unit/test_content_policies.py
git commit -m "feat(adaptive): content_policies table with CRUD and default seeding"
```

---

## Task 9: Content Analyzer Configure Methods

**Files:**
- Modify: `src/gateway/content/pii_detector.py` (add `configure()`)
- Modify: `src/gateway/content/toxicity_detector.py` (add `configure()`)
- Modify: `src/gateway/content/llama_guard.py` (add `configure()`)
- Test: `tests/unit/test_content_policy_configure.py`

### Step 1: Write failing tests

```python
# tests/unit/test_content_policy_configure.py
"""Tests for dynamic content analyzer configuration."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from gateway.content.pii_detector import PIIDetector
from gateway.content.toxicity_detector import ToxicityDetector
from gateway.content.llama_guard import LlamaGuardAnalyzer
from gateway.content.base import Verdict

anyio_backend = ["asyncio"]


class TestPIIDetectorConfigure:
    def test_configure_changes_block_types(self):
        d = PIIDetector()
        d.configure([
            {"category": "email_address", "action": "block"},
            {"category": "credit_card", "action": "warn"},
        ])
        assert "email_address" in d._block_types
        assert "credit_card" not in d._block_types

    @pytest.mark.anyio
    async def test_configured_block_type_blocks(self):
        d = PIIDetector()
        d.configure([{"category": "email_address", "action": "block"}])
        result = await d.analyze("contact us at admin@example.com")
        assert result.verdict == Verdict.BLOCK

    @pytest.mark.anyio
    async def test_configured_pass_type_passes(self):
        d = PIIDetector()
        d.configure([{"category": "credit_card", "action": "pass"}])
        result = await d.analyze("card 4111111111111111")
        assert result.verdict == Verdict.PASS


class TestToxicityDetectorConfigure:
    def test_configure_changes_block_categories(self):
        d = ToxicityDetector()
        d.configure([
            {"category": "violence", "action": "block"},
            {"category": "child_safety", "action": "warn"},
        ])
        assert "violence" in d._block_categories
        assert "child_safety" not in d._block_categories

    @pytest.mark.anyio
    async def test_default_behavior_without_configure(self):
        d = ToxicityDetector()
        # child_safety should block by default
        result = await d.analyze("csam content here")
        assert result.verdict == Verdict.BLOCK


class TestLlamaGuardConfigure:
    def test_configure_changes_category_actions(self):
        d = LlamaGuardAnalyzer(ollama_url="http://localhost:11434")
        d.configure([
            {"category": "S1", "action": "block"},
            {"category": "S4", "action": "warn"},
        ])
        assert d._category_actions.get("S1") == "block"
        assert d._category_actions.get("S4") == "warn"
```

### Step 2: Run tests to verify they fail

Run: `python -m pytest tests/unit/test_content_policy_configure.py -v`

### Step 3: Add `configure()` to each analyzer

**pii_detector.py** — add method to `PIIDetector` class:

```python
    def configure(self, policies: list[dict]) -> None:
        """Update block/warn/pass tiers from control plane policies."""
        if not policies:
            return
        self._block_types = {p["category"] for p in policies if p.get("action") == "block"}
        self._warn_types = {p["category"] for p in policies if p.get("action") == "warn"}
        self._pass_types = {p["category"] for p in policies if p.get("action") == "pass"}
```

Also change the `analyze()` method to use `self._block_types` and `self._warn_types` instead of the module-level `_BLOCK_PII_TYPES`. Initialize them in `__init__`:

```python
    def __init__(self) -> None:
        self._block_types: set[str] = set(_BLOCK_PII_TYPES)
        self._warn_types: set[str] = {
            name for name, _ in _PATTERNS if name not in _BLOCK_PII_TYPES}
        self._pass_types: set[str] = set()
```

Update `analyze()` to check `self._pass_types` first, then `self._block_types`, then `self._warn_types`.

**toxicity_detector.py** — add:

```python
    def configure(self, policies: list[dict]) -> None:
        """Update block/warn categories from control plane policies."""
        if not policies:
            return
        self._block_categories = {p["category"] for p in policies if p.get("action") == "block"}
        self._warn_categories = {p["category"] for p in policies if p.get("action") == "warn"}
```

Initialize `_block_categories` and `_warn_categories` in `__init__`:

```python
        self._block_categories: set[str] = {"child_safety"}
        self._warn_categories: set[str] = {"self_harm_indicator", "violence_instruction"}
```

**llama_guard.py** — add:

```python
    def configure(self, policies: list[dict]) -> None:
        """Update per-category actions from control plane policies."""
        if not policies:
            return
        self._category_actions = {
            p["category"]: p["action"] for p in policies}
```

Initialize `_category_actions` in `__init__`:

```python
        self._category_actions: dict[str, str] = {
            "S4": "block",  # child safety always blocks by default
        }
```

### Step 4: Run tests

Run: `python -m pytest tests/unit/test_content_policy_configure.py -v`
Expected: All PASS

### Step 5: Run full test suite for regressions

Run: `python -m pytest tests/unit/ -x -q`
Expected: All existing tests pass (configure() is additive; default behavior unchanged)

### Step 6: Commit

```bash
git add src/gateway/content/pii_detector.py src/gateway/content/toxicity_detector.py src/gateway/content/llama_guard.py tests/unit/test_content_policy_configure.py
git commit -m "feat(adaptive): add configure() to content analyzers for dynamic policy updates"
```

---

## Task 10: Content Policy API Endpoints + Cache Refresh

**Files:**
- Modify: `src/gateway/control/api.py` (add 3 endpoints + `_refresh_content_policies()`)
- Modify: `src/gateway/control/loader.py` (load content policies on startup)
- Modify: `src/gateway/main.py` (seed defaults on startup)

### Step 1: Add API endpoints in `api.py`

After the last route handler (`control_discover_models`), add:

```python
# ── Content Policies ──────────────────────────────────────────────────────────

def _refresh_content_policies() -> None:
    """Reload content policies from store into running analyzers."""
    ctx = get_pipeline_context()
    store = ctx.control_store
    if not store:
        return
    policies = store.list_content_policies()
    for analyzer in ctx.content_analyzers:
        aid = getattr(analyzer, "analyzer_id", None)
        if aid and hasattr(analyzer, "configure"):
            relevant = [p for p in policies if p["analyzer_id"] == aid]
            analyzer.configure(relevant)
    logger.info("Content policies refreshed: %d rules across %d analyzers",
                len(policies), len(ctx.content_analyzers))


async def control_list_content_policies(request: Request) -> JSONResponse:
    store = _store_or_503()
    analyzer_id = request.query_params.get("analyzer_id")
    policies = store.list_content_policies(analyzer_id=analyzer_id)
    return JSONResponse({"policies": policies, "count": len(policies)})


async def control_upsert_content_policy(request: Request) -> JSONResponse:
    store = _store_or_503()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    tenant = body.get("tenant_id", _tenant(request))
    analyzer_id = body.get("analyzer_id")
    category = body.get("category")
    action = body.get("action", "warn")
    if not analyzer_id or not category:
        return JSONResponse({"error": "analyzer_id and category required"}, status_code=400)
    if action not in ("block", "warn", "pass"):
        return JSONResponse({"error": "action must be block, warn, or pass"}, status_code=400)
    threshold = float(body.get("threshold", 0.5))
    policy = store.upsert_content_policy(tenant, analyzer_id, category, action, threshold)
    _refresh_content_policies()
    return JSONResponse(policy, status_code=201)


async def control_delete_content_policy(request: Request) -> JSONResponse:
    store = _store_or_503()
    policy_id = request.path_params["policy_id"]
    deleted = store.delete_content_policy(policy_id)
    if not deleted:
        return JSONResponse({"error": "Not found"}, status_code=404)
    _refresh_content_policies()
    return JSONResponse({"deleted": True})
```

Register routes in the route list (wherever control routes are added — check `main.py` for `Route` objects):

```python
Route("/v1/control/content-policies", control_list_content_policies, methods=["GET"]),
Route("/v1/control/content-policies", control_upsert_content_policy, methods=["POST"]),
Route("/v1/control/content-policies/{policy_id}", control_delete_content_policy, methods=["DELETE"]),
```

### Step 2: Add content policy loading to `loader.py`

In `load_into_caches()`, add after existing policy/budget loading:

```python
    # Load content policies into analyzers
    if control_store:
        from gateway.control.api import _refresh_content_policies
        _refresh_content_policies()
```

### Step 3: Seed defaults on startup in `main.py`

After `_init_control_plane()`, add:

```python
    # Seed default content policies if control plane is active
    if ctx.control_store:
        ctx.control_store.seed_default_content_policies()
```

### Step 4: Run full test suite

Run: `python -m pytest tests/unit/ -x -q`
Expected: All pass

### Step 5: Commit

```bash
git add src/gateway/control/api.py src/gateway/control/loader.py src/gateway/main.py
git commit -m "feat(adaptive): content policy API endpoints with cache refresh on mutation"
```

---

## Task 11: Resource Monitor + Capability Registry

**Files:**
- Create: `src/gateway/adaptive/resource_monitor.py`
- Create: `src/gateway/adaptive/capability_registry.py`
- Test: `tests/unit/test_resource_monitor.py`
- Test: `tests/unit/test_capability_registry.py`

### Step 1: Write tests for resource monitor

```python
# tests/unit/test_resource_monitor.py
"""Tests for runtime resource monitoring."""
import pytest
import time
from unittest.mock import patch, MagicMock
from gateway.adaptive.resource_monitor import DefaultResourceMonitor

anyio_backend = ["asyncio"]


@pytest.fixture
def monitor():
    return DefaultResourceMonitor(wal_path="/tmp", min_free_pct=5.0)


@pytest.mark.anyio
async def test_disk_check_healthy(monitor):
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=50_000_000_000)
        status = await monitor.check()
    assert status.disk_healthy is True
    assert status.disk_free_pct == 50.0


@pytest.mark.anyio
async def test_disk_check_unhealthy(monitor):
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=2_000_000_000)
        status = await monitor.check()
    assert status.disk_healthy is False


def test_provider_cooldown_no_errors(monitor):
    assert monitor.get_provider_cooldown("ollama") is None


def test_provider_cooldown_under_threshold(monitor):
    # 2 failures out of 10 = 20% — no cooldown
    for i in range(10):
        monitor.record_provider_result("ollama", success=(i >= 2))
    assert monitor.get_provider_cooldown("ollama") is None


def test_provider_cooldown_over_threshold(monitor):
    # 8 failures out of 10 = 80% — should trigger cooldown
    for i in range(10):
        monitor.record_provider_result("ollama", success=(i >= 8))
    cooldown = monitor.get_provider_cooldown("ollama")
    assert cooldown is not None
    assert cooldown > 0


def test_provider_cooldown_ignores_old_errors(monitor):
    # Record old failures (>60s ago won't exist since we use real time)
    # Just verify that few recent errors don't trigger
    monitor.record_provider_result("ollama", success=False)
    monitor.record_provider_result("ollama", success=True)
    assert monitor.get_provider_cooldown("ollama") is None  # too few samples
```

### Step 2: Write tests for capability registry

```python
# tests/unit/test_capability_registry.py
"""Tests for model capability registry with TTL."""
import pytest
import time
from gateway.adaptive.capability_registry import CapabilityRegistry, ModelCapability


def test_unknown_model_returns_none():
    reg = CapabilityRegistry(ttl_seconds=3600)
    assert reg.supports_tools("unknown-model") is None


def test_record_and_query():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("qwen3:4b", supports_tools=True, provider="ollama")
    assert reg.supports_tools("qwen3:4b") is True


def test_record_false():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("gemma3:1b", supports_tools=False, provider="ollama")
    assert reg.supports_tools("gemma3:1b") is False


def test_ttl_expiry():
    reg = CapabilityRegistry(ttl_seconds=1)
    reg.record("qwen3:4b", supports_tools=True, provider="ollama")
    # Manually expire
    reg._cache["qwen3:4b"] = reg._cache["qwen3:4b"]._replace(
        probed_at=time.time() - 10)
    assert reg.supports_tools("qwen3:4b") is None  # stale


def test_get_timeout_default():
    reg = CapabilityRegistry(ttl_seconds=3600)
    assert reg.get_timeout("unknown", default=60.0) == 60.0


def test_get_timeout_reasoning():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("qwen3:4b", supports_tools=True, provider="ollama",
               model_type="reasoning")
    assert reg.get_timeout("qwen3:4b", default=60.0) == 120.0


def test_get_timeout_embedding():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("embed-model", supports_tools=False, provider="openai",
               model_type="embedding")
    assert reg.get_timeout("embed-model", default=60.0) == 30.0


def test_get_stale_models():
    reg = CapabilityRegistry(ttl_seconds=1)
    reg.record("m1", supports_tools=True, provider="ollama")
    reg.record("m2", supports_tools=False, provider="ollama")
    # Expire m1
    reg._cache["m1"] = reg._cache["m1"]._replace(probed_at=time.time() - 10)
    stale = reg.get_stale_models()
    assert "m1" in stale
    assert "m2" not in stale


def test_mark_for_reprobe():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("qwen3:4b", supports_tools=True, provider="ollama")
    reg.mark_for_reprobe("qwen3:4b")
    assert reg.supports_tools("qwen3:4b") is None


def test_all_capabilities():
    reg = CapabilityRegistry(ttl_seconds=3600)
    reg.record("m1", supports_tools=True, provider="ollama")
    reg.record("m2", supports_tools=False, provider="openai")
    caps = reg.all_capabilities()
    assert len(caps) == 2
```

### Step 3: Write implementations

```python
# src/gateway/adaptive/capability_registry.py
"""Model capability registry with TTL-based re-probing.

Replaces the simple _model_capabilities dict in orchestrator.py with
a richer registry that supports TTL expiry, model type classification,
per-model timeouts, and optional persistence to the control plane store.
"""
from __future__ import annotations

import logging
import time
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)


class ModelCapability(NamedTuple):
    """Cached capabilities for a single model."""
    model_id: str
    provider: str = ""
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    model_type: str = "chat"  # chat, reasoning, embedding, code
    probed_at: float = 0.0
    probe_count: int = 0

    def _replace(self, **kwargs) -> "ModelCapability":
        return ModelCapability(**{**self._asdict(), **kwargs})


class CapabilityRegistry:
    """Model capability cache with TTL and optional persistence."""

    def __init__(self, ttl_seconds: int = 86400, control_store: Any = None):
        self._cache: dict[str, ModelCapability] = {}
        self._ttl = ttl_seconds
        self._store = control_store

    def supports_tools(self, model_id: str) -> bool | None:
        cap = self._cache.get(model_id)
        if cap is None:
            return None
        if self._is_stale(cap):
            return None
        return cap.supports_tools

    def record(self, model_id: str, **kwargs: Any) -> None:
        existing = self._cache.get(model_id)
        if existing:
            updates = {k: v for k, v in kwargs.items() if v is not None}
            updated = existing._replace(
                probed_at=time.time(),
                probe_count=existing.probe_count + 1,
                **updates)
        else:
            updated = ModelCapability(
                model_id=model_id,
                probed_at=time.time(),
                probe_count=1,
                **{k: v for k, v in kwargs.items() if v is not None})
        self._cache[model_id] = updated
        logger.info("Model capability recorded: %s = %s", model_id, dict(updated._asdict()))

    def get_timeout(self, model_id: str, default: float = 60.0) -> float:
        cap = self._cache.get(model_id)
        if not cap:
            return default
        if cap.model_type == "reasoning":
            return default * 2.0
        if cap.model_type == "embedding":
            return default * 0.5
        return default

    def get_stale_models(self) -> list[str]:
        return [mid for mid, cap in self._cache.items() if self._is_stale(cap)]

    def mark_for_reprobe(self, model_id: str) -> None:
        cap = self._cache.get(model_id)
        if cap:
            self._cache[model_id] = cap._replace(probed_at=0)

    def all_capabilities(self) -> dict[str, dict[str, Any]]:
        return {mid: dict(cap._asdict()) for mid, cap in self._cache.items()}

    def _is_stale(self, cap: ModelCapability) -> bool:
        return (time.time() - cap.probed_at) > self._ttl
```

```python
# src/gateway/adaptive/resource_monitor.py
"""Runtime resource monitoring — disk, connections, provider health.

Tracks provider error rates using a sliding window and implements
LiteLLM-style cooldown when failure rates exceed thresholds.
"""
from __future__ import annotations

import logging
import shutil
import time
from collections import defaultdict, deque
from typing import Any

from gateway.adaptive.interfaces import ResourceMonitor, ResourceStatus

logger = logging.getLogger(__name__)


class DefaultResourceMonitor(ResourceMonitor):
    """Monitors disk space and provider error rates."""

    def __init__(self, wal_path: str, min_free_pct: float = 5.0,
                 window_seconds: float = 60.0, cooldown_seconds: float = 30.0,
                 failure_threshold: float = 0.5, min_samples: int = 3):
        self._wal_path = wal_path
        self._min_free_pct = min_free_pct
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._failure_threshold = failure_threshold
        self._min_samples = min_samples
        self._provider_results: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=100))
        self._active_requests = 0

    async def check(self) -> ResourceStatus:
        try:
            usage = shutil.disk_usage(self._wal_path)
            free_pct = round((usage.free / usage.total) * 100, 1)
            healthy = free_pct > self._min_free_pct
        except OSError:
            free_pct = 0.0
            healthy = False

        return ResourceStatus(
            disk_free_pct=free_pct,
            disk_healthy=healthy,
            active_requests=self._active_requests,
            provider_error_rates=self._get_error_rates())

    def record_provider_result(self, provider: str, success: bool) -> None:
        self._provider_results[provider].append((time.time(), success))

    def get_provider_cooldown(self, provider: str) -> float | None:
        """Returns cooldown seconds if provider failure rate exceeds threshold."""
        results = self._provider_results.get(provider)
        if not results:
            return None
        cutoff = time.time() - self._window_seconds
        recent = [(t, ok) for t, ok in results if t > cutoff]
        if len(recent) < self._min_samples:
            return None
        fail_count = sum(1 for _, ok in recent if not ok)
        fail_rate = fail_count / len(recent)
        if fail_rate > self._failure_threshold:
            return self._cooldown_seconds
        return None

    def increment_active(self) -> None:
        self._active_requests += 1

    def decrement_active(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)

    def _get_error_rates(self) -> dict[str, float]:
        rates = {}
        cutoff = time.time() - self._window_seconds
        for provider, results in self._provider_results.items():
            recent = [(t, ok) for t, ok in results if t > cutoff]
            if recent:
                rates[provider] = round(
                    sum(1 for _, ok in recent if not ok) / len(recent), 2)
        return rates
```

### Step 4: Run tests

Run: `python -m pytest tests/unit/test_resource_monitor.py tests/unit/test_capability_registry.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add src/gateway/adaptive/resource_monitor.py src/gateway/adaptive/capability_registry.py tests/unit/test_resource_monitor.py tests/unit/test_capability_registry.py
git commit -m "feat(adaptive): resource monitor with provider cooldown and capability registry with TTL"
```

---

## Task 12: Integrate Runtime Adaptation into Pipeline

**Files:**
- Modify: `src/gateway/main.py` (init resource_monitor, capability_registry, httpx hooks, background tasks)
- Modify: `src/gateway/pipeline/orchestrator.py` (replace `_model_capabilities` with registry, add provider-aware retry)
- Modify: `src/gateway/health.py` (expose resource monitor status)

### Step 1: Init in `on_startup()`

After the request classifier/validator init block:

```python
    # Phase 23: Capability registry + resource monitor
    from gateway.adaptive.capability_registry import CapabilityRegistry
    from gateway.adaptive.resource_monitor import DefaultResourceMonitor
    ctx.capability_registry = CapabilityRegistry(
        ttl_seconds=settings.capability_probe_ttl_seconds,
        control_store=ctx.control_store)
    if settings.disk_monitor_enabled:
        ctx.resource_monitor = DefaultResourceMonitor(
            wal_path=settings.wal_path,
            min_free_pct=settings.disk_min_free_percent)
```

### Step 2: Add httpx event hooks

Modify the `ctx.http_client` creation to include response hooks:

```python
    async def _on_provider_response(response):
        if ctx.resource_monitor:
            provider = _extract_provider_from_url(str(response.url))
            ctx.resource_monitor.record_provider_result(
                provider, response.status_code < 500)

    ctx.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.provider_timeout, connect=settings.provider_connect_timeout),
        limits=httpx.Limits(max_connections=settings.provider_max_connections,
                            max_keepalive_connections=settings.provider_max_keepalive),
        http2=True,
        event_hooks={"response": [_on_provider_response]},
    )
```

Add helper:

```python
def _extract_provider_from_url(url: str) -> str:
    if "ollama" in url or ":11434" in url:
        return "ollama"
    if "openai" in url:
        return "openai"
    if "anthropic" in url:
        return "anthropic"
    return "unknown"
```

### Step 3: Add background tasks

In `on_startup()` after all init:

```python
    # Phase 23: Resource monitor background task
    if ctx.resource_monitor and settings.disk_monitor_enabled:
        async def _resource_monitor_loop():
            while True:
                await asyncio.sleep(settings.resource_monitor_interval_seconds)
                try:
                    status = await ctx.resource_monitor.check()
                    if not status.disk_healthy:
                        logger.warning("Resource monitor: disk %.1f%% free", status.disk_free_pct)
                except Exception as e:
                    logger.debug("Resource monitor check failed: %s", e)
        ctx.resource_monitor_task = asyncio.create_task(_resource_monitor_loop())
```

### Step 4: Update orchestrator to use CapabilityRegistry

Replace `_model_supports_tools()` calls with `ctx.capability_registry.supports_tools()` and `_record_model_capability()` with `ctx.capability_registry.record()`. This is a search-and-replace in orchestrator.py:

- `_model_supports_tools(call.model_id)` → `ctx.capability_registry.supports_tools(call.model_id) if ctx.capability_registry else _model_supports_tools(call.model_id)`
- `_record_model_capability(model_id, True/False)` → `if ctx.capability_registry: ctx.capability_registry.record(model_id, supports_tools=True/False, provider=provider)`

Keep the old functions as fallback for when capability_registry is None.

### Step 5: Update health.py

Replace the `_model_capabilities` exposure with:

```python
    if ctx.capability_registry:
        caps = ctx.capability_registry.all_capabilities()
        if caps:
            status["model_capabilities"] = caps
    elif _model_capabilities:
        status["model_capabilities"] = dict(_model_capabilities)

    if ctx.resource_monitor:
        try:
            res_status = await ctx.resource_monitor.check()
            status["resource_monitor"] = {
                "disk_free_pct": res_status.disk_free_pct,
                "disk_healthy": res_status.disk_healthy,
                "active_requests": res_status.active_requests,
                "provider_error_rates": res_status.provider_error_rates,
            }
        except Exception:
            pass
```

### Step 6: Run full test suite

Run: `python -m pytest tests/unit/ -x -q`
Expected: All pass

### Step 7: Commit

```bash
git add src/gateway/main.py src/gateway/pipeline/orchestrator.py src/gateway/health.py
git commit -m "feat(adaptive): integrate capability registry, resource monitor, and httpx hooks into pipeline"
```

---

## Task 13: Content Analysis Caching

**Files:**
- Modify: `src/gateway/pipeline/response_evaluator.py` (add SHA256-keyed cache)
- Test: `tests/unit/test_analysis_cache.py`

### Step 1: Write failing tests

```python
# tests/unit/test_analysis_cache.py
"""Tests for content analysis caching."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from gateway.pipeline.response_evaluator import analyze_text, _analysis_cache

anyio_backend = ["asyncio"]


@pytest.fixture(autouse=True)
def clear_cache():
    _analysis_cache.clear()
    yield
    _analysis_cache.clear()


@pytest.mark.anyio
async def test_cache_hit():
    analyzer = MagicMock()
    analyzer.analyzer_id = "test"
    analyzer.timeout_ms = 50
    analyzer.analyze = AsyncMock(return_value=MagicMock(
        verdict="pass", confidence=1.0, analyzer_id="test", category="", reason=""))

    # First call — cache miss
    r1 = await analyze_text("hello world", [analyzer])
    assert analyzer.analyze.call_count == 1

    # Second call — cache hit
    r2 = await analyze_text("hello world", [analyzer])
    assert analyzer.analyze.call_count == 1  # not called again
    assert r1 == r2


@pytest.mark.anyio
async def test_different_text_no_cache_hit():
    analyzer = MagicMock()
    analyzer.analyzer_id = "test"
    analyzer.timeout_ms = 50
    analyzer.analyze = AsyncMock(return_value=MagicMock(
        verdict="pass", confidence=1.0, analyzer_id="test", category="", reason=""))

    await analyze_text("hello", [analyzer])
    await analyze_text("world", [analyzer])
    assert analyzer.analyze.call_count == 2
```

### Step 2: Add caching to `analyze_text()`

In `response_evaluator.py`, add a module-level cache dict and wrap the existing logic:

```python
import hashlib

_analysis_cache: dict[str, list[dict]] = {}
_CACHE_MAX = 1000
```

Wrap the existing `analyze_text()` to check/populate cache:

```python
async def analyze_text(text: str, analyzers: list) -> list[dict]:
    if not text or not analyzers:
        return []
    cache_key = hashlib.sha256(text.encode()).hexdigest()[:16]
    cached = _analysis_cache.get(cache_key)
    if cached is not None:
        return cached
    decisions = await _run_analyzers(text, analyzers)
    if len(_analysis_cache) < _CACHE_MAX:
        _analysis_cache[cache_key] = decisions
    return decisions
```

Move the existing analysis logic into `_run_analyzers()`.

### Step 3: Run tests

Run: `python -m pytest tests/unit/test_analysis_cache.py -v`
Expected: All PASS

### Step 4: Run full suite

Run: `python -m pytest tests/unit/ -x -q`
Expected: All pass

### Step 5: Commit

```bash
git add src/gateway/pipeline/response_evaluator.py tests/unit/test_analysis_cache.py
git commit -m "feat(adaptive): content analysis caching with SHA256-keyed bounded cache"
```

---

## Task 14: Update .env.example and CLAUDE.md

**Files:**
- Modify: `.env.example` (add Phase 23 config fields)
- Modify: `CLAUDE.md` (add Phase 23 section)

### Step 1: Add to `.env.example`

```env
# Phase 23: Adaptive Gateway
WALACOR_STARTUP_PROBES_ENABLED=true
WALACOR_PROVIDER_HEALTH_CHECK_ON_STARTUP=true
WALACOR_CAPABILITY_PROBE_TTL_SECONDS=86400
WALACOR_IDENTITY_VALIDATION_ENABLED=true
WALACOR_DISK_MONITOR_ENABLED=true
WALACOR_DISK_MIN_FREE_PERCENT=5.0
WALACOR_RESOURCE_MONITOR_INTERVAL_SECONDS=60
# Enterprise extension points (comma-separated Python dotted class paths)
# WALACOR_CUSTOM_STARTUP_PROBES=mycompany.probes.DatadogProbe
# WALACOR_CUSTOM_REQUEST_CLASSIFIERS=mycompany.classify.CustomClassifier
# WALACOR_CUSTOM_IDENTITY_VALIDATORS=mycompany.auth.CustomValidator
# WALACOR_CUSTOM_RESOURCE_MONITORS=mycompany.monitor.CustomMonitor
```

### Step 2: Add Phase 23 section to CLAUDE.md

Add after the Phase 22 section:

```markdown
## Phase 23: Adaptive Gateway
- `src/gateway/adaptive/` — self-configuring intelligence layer with 5 extensible ABCs
- `interfaces.py`: StartupProbe, RequestClassifier, CapabilityProbe, IdentityValidator, ResourceMonitor
- `startup_probes.py`: ProviderHealthProbe (pings providers), RoutingEndpointProbe (validates routing URLs), DiskSpaceProbe (auto-scales WAL limits), APIVersionProbe (detects Ollama version)
- `request_classifier.py`: DefaultRequestClassifier — body `task` field (priority 1) > user-agent synthetic detection (priority 2) > prompt regex fallback (priority 3)
- `identity_validator.py`: DefaultIdentityValidator — cross-checks X-User-Id against JWT sub claim; JWT wins on mismatch
- `capability_registry.py`: CapabilityRegistry — model capabilities with TTL-based re-probing, per-model timeouts (reasoning=2x, embedding=0.5x)
- `resource_monitor.py`: DefaultResourceMonitor — disk space checks, provider error rate tracking, LiteLLM-style cooldown (>50% failure in 60s → 30s cooldown)
- Content policies: `content_policies` table in control plane, configurable BLOCK/WARN/PASS per category per analyzer; analyzers have `configure(policies)` method for hot-reload
- Enterprise extension: `WALACOR_CUSTOM_*` config fields accept comma-separated Python class paths; `load_custom_class()` utility for importlib loading
- All probes/monitors fail-open; never block traffic due to probe failure
- Content analysis caching: SHA256-keyed bounded cache (max 1000 entries) in response_evaluator
```

### Step 3: Commit

```bash
git add .env.example CLAUDE.md
git commit -m "docs: add Phase 23 Adaptive Gateway to CLAUDE.md and .env.example"
```

---

## Task 15: Final Integration Test

### Step 1: Run full unit test suite

Run: `python -m pytest tests/unit/ -v`
Expected: All pass (existing + ~59 new tests)

### Step 2: Verify health endpoint shows new data

Start gateway locally and check:

```bash
curl -s http://localhost:8000/health | python -m json.tool
```

Expected new fields: `startup_probes`, `resource_monitor`, `model_capabilities` (from registry)

### Step 3: Verify content policy API

```bash
# List default policies
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/v1/control/content-policies | python -m json.tool

# Change PII email to block
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"analyzer_id":"walacor.pii.v1","category":"email_address","action":"block"}' \
  http://localhost:8000/v1/control/content-policies

# Verify change took effect
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/v1/control/content-policies?analyzer_id=walacor.pii.v1
```

### Step 4: Final commit

```bash
git add -A
git commit -m "feat: Phase 23 Adaptive Gateway — complete implementation"
```
