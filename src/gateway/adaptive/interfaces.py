"""Abstract interfaces and data classes for the adaptive gateway layer."""

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
        name: Human-readable probe identifier (e.g. ``"ollama"``, ``"disk"``).
        healthy: ``True`` if the probed component is operational.
        detail: Arbitrary diagnostic payload (latency, version, etc.).
    """

    name: str
    healthy: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """Result of an identity validation check.

    Attributes:
        valid: ``True`` if the caller identity was verified.
        identity: Resolved user/service identifier (may be ``""`` on failure).
        source: Mechanism that produced the identity (``"jwt"``, ``"api_key"``, etc.).
        warnings: Non-fatal messages (e.g. ``"clock skew detected"``).
    """

    valid: bool
    identity: str = ""
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
    """Probe executed once during gateway startup to verify readiness.

    Contract: ``check()`` must return a :class:`ProbeResult` within a
    reasonable timeout.  A probe that raises is treated as unhealthy.
    """

    @abstractmethod
    async def check(self) -> ProbeResult:
        """Run the probe and return a result."""


class RequestClassifier(ABC):
    """Classifies an inbound request for routing/policy decisions.

    Contract: ``classify()`` receives the raw ASGI request and returns a
    string label (e.g. ``"chat"``, ``"completion"``, ``"embedding"``).
    Implementations must be fast (< 1 ms) and side-effect-free.
    """

    @abstractmethod
    async def classify(self, request: Any) -> str:
        """Return a classification label for the request."""


class CapabilityProbe(ABC):
    """Discovers runtime capabilities of a model or provider.

    Contract: ``probe()`` accepts a model identifier and returns a
    :class:`ProbeResult` describing what the model supports (e.g.
    function-calling, vision, streaming).
    """

    @abstractmethod
    async def probe(self, model_id: str) -> ProbeResult:
        """Probe a model's capabilities and return a result."""


class IdentityValidator(ABC):
    """Validates caller identity beyond simple API-key lookup.

    Contract: ``validate()`` receives the raw request and returns a
    :class:`ValidationResult`.  Must never raise — return
    ``ValidationResult(valid=False, ...)`` on failure.
    """

    @abstractmethod
    async def validate(self, request: Any) -> ValidationResult:
        """Validate the caller's identity and return a result."""


class ResourceMonitor(ABC):
    """Periodically checks runtime resource health.

    Contract: ``check()`` returns a :class:`ResourceStatus` snapshot.
    Implementations should be non-blocking and complete within 100 ms.
    """

    @abstractmethod
    async def check(self) -> ResourceStatus:
        """Return a current resource status snapshot."""
