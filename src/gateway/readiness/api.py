"""GET /v1/readiness route handler."""

from __future__ import annotations

import dataclasses
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


async def readiness_handler(request: Request) -> JSONResponse:
    from gateway.config import get_settings
    from gateway.pipeline.context import get_pipeline_context

    settings = get_settings()

    if not settings.readiness_enabled:
        return JSONResponse(
            {"error": "readiness endpoint disabled"},
            status_code=503,
        )

    ctx = get_pipeline_context()
    fresh = request.query_params.get("fresh") == "1"

    from gateway.readiness.runner import run_all
    report = await run_all(ctx, fresh=fresh)

    body = dataclasses.asdict(report)
    return JSONResponse(body)
