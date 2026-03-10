"""GET /v1/models — OpenAI-compatible model listing."""
import time
from starlette.requests import Request
from starlette.responses import JSONResponse
from gateway.pipeline.context import get_pipeline_context


async def list_models(request: Request) -> JSONResponse:
    ctx = get_pipeline_context()
    models = []

    if ctx.control_store:
        attestations = ctx.control_store.list_attestations()
        for att in attestations:
            if att.get("status") != "active":
                continue
            models.append({
                "id": att["model_id"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": att.get("provider", "unknown"),
            })

    return JSONResponse({
        "object": "list",
        "data": models,
    })
