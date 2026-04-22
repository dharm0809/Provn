"""Readiness runner: execute all checks concurrently with per-check timeout."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext

from gateway.readiness.protocol import CheckResult, Severity

logger = logging.getLogger(__name__)

_READINESS_TTL_S = 15.0
_CHECK_TIMEOUT_S = 5.0

_cache: tuple[float, "ReadinessReport"] | None = None
_cache_lock_obj: asyncio.Lock | None = None  # lazy-initialised inside run_all()

_previous_statuses: dict[str, str] = {}


@dataclass
class ReadinessReport:
    status: str  # ready | degraded | unready
    generated_at: str
    cache_age_s: float
    gateway_id: str
    summary: dict[str, int]
    checks: list[dict[str, Any]]


async def _run_one(check: Any, ctx: "PipelineContext") -> dict[str, Any]:
    start = time.monotonic()
    try:
        result: CheckResult = await asyncio.wait_for(
            check.run(ctx), timeout=_CHECK_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        result = CheckResult(
            status="amber",
            detail=f"check timed out after {_CHECK_TIMEOUT_S:.0f}s",
            elapsed_ms=elapsed,
        )
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        result = CheckResult(
            status="amber",
            detail=f"internal error: {exc}",
            elapsed_ms=elapsed,
        )

    # Drift audit hook
    prev = _previous_statuses.get(check.id)
    if result.status == "red" and check.severity in (Severity.sec, Severity.int):
        from gateway.readiness.drift_audit import maybe_write_drift_record
        maybe_write_drift_record(check.id, result, prev, ctx)
    _previous_statuses[check.id] = result.status

    return {
        "id": check.id,
        "name": check.name,
        "category": check.category.value,
        "severity": check.severity.value,
        "status": result.status,
        "detail": result.detail,
        "remediation": result.remediation,
        "evidence": result.evidence,
        "elapsed_ms": result.elapsed_ms,
    }


def _rollup(checks: list[dict[str, Any]]) -> str:
    """Per §4.5:
      - unready iff any sec/int check is red
      - degraded iff any non-warn check is red/amber
      - ready iff all green, OR only warn-severity ambers
    """
    for c in checks:
        if c["status"] == "red" and c["severity"] in ("sec", "int"):
            return "unready"
    for c in checks:
        if c["status"] in ("red", "amber") and c["severity"] != "warn":
            return "degraded"
    # Only warn-severity ambers (or all green) remain — ready.
    for c in checks:
        if c["status"] == "red":
            # A red warn-severity check still needs to surface as degraded.
            return "degraded"
    return "ready"


async def run_all(
    ctx: "PipelineContext",
    *,
    timeout_s: float = _CHECK_TIMEOUT_S,
    fresh: bool = False,
) -> ReadinessReport:
    global _cache_lock_obj, _cache

    if _cache_lock_obj is None:
        _cache_lock_obj = asyncio.Lock()

    async with _cache_lock_obj:
        now = time.monotonic()
        if not fresh and _cache is not None:
            age = now - _cache[0]
            if age < _READINESS_TTL_S:
                rep = _cache[1]
                import dataclasses
                return dataclasses.replace(rep, cache_age_s=round(age, 1))

        # Import here to populate registry via side-effects
        import gateway.readiness.checks  # noqa: F401

        from gateway.readiness.registry import all_checks
        from gateway.config import get_settings
        import datetime

        settings = get_settings()
        checks = all_checks()

        # Per §4.4: return_exceptions=True so a single bad check cannot DoS
        # the endpoint. _run_one catches TimeoutError / Exception internally
        # and returns a result dict — the outer return_exceptions=True is a
        # belt-and-braces safeguard if _run_one itself raises.
        raw = await asyncio.gather(
            *[_run_one(c, ctx) for c in checks], return_exceptions=True
        )
        results: list[dict[str, Any]] = []
        for idx, item in enumerate(raw):
            if isinstance(item, BaseException):
                c = checks[idx]
                results.append({
                    "id": c.id, "name": c.name,
                    "category": c.category.value, "severity": c.severity.value,
                    "status": "amber",
                    "detail": f"runner error: {item}",
                    "remediation": None, "evidence": {}, "elapsed_ms": 0,
                })
            else:
                results.append(item)

        status = _rollup(results)
        summary: dict[str, int] = {"green": 0, "amber": 0, "red": 0, "total": len(results)}
        for r in results:
            summary[r["status"]] = summary.get(r["status"], 0) + 1

        report = ReadinessReport(
            status=status,
            generated_at=datetime.datetime.utcnow().isoformat() + "Z",
            cache_age_s=0.0,
            gateway_id=settings.gateway_id,
            summary=summary,
            checks=list(results),
        )
        _cache = (now, report)
        return report
