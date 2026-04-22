"""Drift-to-audit hook: write attempt records when sec/int checks flip to red.

Rate-limited to once per check-id per 5 minutes so a flapping check can't
flood the WAL.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext
    from gateway.readiness.protocol import CheckResult

logger = logging.getLogger(__name__)

_RATE_LIMIT_S = 300  # 5 minutes
_last_written: dict[str, float] = {}


def reset_rate_limit() -> None:
    """Clear the per-check rate-limit state. For tests."""
    _last_written.clear()


def maybe_write_drift_record(
    check_id: str,
    result: "CheckResult",
    previous_status: str | None,
    ctx: "PipelineContext",
) -> bool:
    """Write a WAL attempt record when a sec/int check flips to red.

    Returns True when a record was written, False otherwise (rate-limited,
    no WAL writer, already red, status not red). Silently skips on write
    failure — never raises.
    """
    if result.status != "red":
        return False
    if previous_status == "red":
        return False

    now = time.monotonic()
    last = _last_written.get(check_id, 0.0)
    if now - last < _RATE_LIMIT_S:
        return False

    if ctx.wal_writer is None:
        return False

    # Record the attempt BEFORE the write so a failing writer doesn't let the
    # check id flood retries on every run.
    _last_written[check_id] = now

    try:
        metadata = {
            "check_id": check_id,
            "detail": result.detail,
            "previous_status": previous_status or "unknown",
        }
        ctx.wal_writer.write_attempt(
            request_id=f"readiness-{uuid.uuid4()}",
            tenant_id="",
            path="/v1/readiness",
            disposition="readiness_degraded",
            status_code=0,
            reason=json.dumps(metadata),
        )
        logger.warning(
            "Readiness drift detected: check=%s previous=%s detail=%s",
            check_id, previous_status, result.detail,
        )
        return True
    except Exception:
        logger.debug("drift_audit write failed (non-fatal)", exc_info=True)
        return False
