"""Cost aggregation endpoint for lineage API."""

from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)


async def lineage_cost_summary(request: Request) -> JSONResponse:
    """GET /v1/lineage/cost?range=24h&group_by=model|user"""
    ctx = get_pipeline_context()
    if ctx.lineage_reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)

    range_key = request.query_params.get("range", "24h")
    group_by = request.query_params.get("group_by", "model")

    try:
        # Local reader is sync, Walacor reader is async — handle both.
        data = ctx.lineage_reader.get_cost_summary(range_key, group_by)
        if asyncio.iscoroutine(data):
            data = await data
        return JSONResponse(data)
    except Exception as e:
        logger.error("lineage_cost_summary error: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
