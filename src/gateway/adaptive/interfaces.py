"""Abstract interfaces and data classes for the adaptive gateway layer.

Every decision point in the gateway has a documented interface that
enterprises can override without forking. Implement any ABC below
and register via WALACOR_CUSTOM_* config fields.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """Result of a startup or capability probe.

    Attributes:
        name: Human-readable probe identifier (e.g. ``"provider_health"``).
        healthy: ``True`` if the probed component is operational.
        detail: Arbitrary diagnostic payload (latency, version, etc.).
    """

    name: str
    healthy: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """Result of identity cross-validation.

    Attributes:
        valid: ``True`` if identities are consistent (no mismatch).
        identity: Resolved CallerIdentity or None.
        source: Mechanism that produced the identity (``"jwt_verified"``, etc.).
        warnings: Non-fatal messages (e.g. identity mismatch details).
    """

    valid: bool
    identity: Any = None  # CallerIdentity or None
    source: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResourceStatus:
    """Snapshot of runtime resource health.

    Attributes:
        disk_free_pct: Percentage of free disk space on the WAL partition.
        disk_healthy: ``True`` if disk_free_pct is above the configured threshold.
        active_requests: Number of in-flight requests tracked by the gateway.
        provider_error_rates: Per-provider error rate in the recent window
            (e.g. ``{"openai": 0.02}``).
    """

    disk_free_pct: float
    disk_healthy: bool
    active_requests: int
    provider_error_rates: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base classes
# ---------------------------------------------------------------------------


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
    - Return dict of capability_key -> value
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
