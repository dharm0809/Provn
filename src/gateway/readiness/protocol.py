"""ReadinessCheck Protocol, CheckResult, and enums."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext


class Category(str, Enum):
    security = "security"
    integrity = "integrity"
    persistence = "persistence"
    dependency = "dependency"
    feature = "feature"
    hygiene = "hygiene"


class Severity(str, Enum):
    sec = "sec"
    int = "int"
    ops = "ops"
    warn = "warn"


@dataclass(frozen=True)
class CheckResult:
    status: Literal["green", "amber", "red"]
    detail: str
    remediation: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0


class ReadinessCheck(Protocol):
    id: str
    name: str
    category: Category
    severity: Severity

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        ...
