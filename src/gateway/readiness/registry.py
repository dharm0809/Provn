"""Registry of all registered readiness checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.readiness.protocol import ReadinessCheck

_REGISTERED: list["ReadinessCheck"] = []


def register(check: "ReadinessCheck") -> "ReadinessCheck":
    """Register a check instance. Returns the check (usable as a decorator target)."""
    _REGISTERED.append(check)
    return check


def all_checks() -> list["ReadinessCheck"]:
    return list(_REGISTERED)
