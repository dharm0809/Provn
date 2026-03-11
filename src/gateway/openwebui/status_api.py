"""OpenWebUI integration status endpoint — banners, budget, model health."""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)


async def openwebui_status(request: Request) -> JSONResponse:
    """GET /v1/openwebui/status — returns banners, budget, and model status for OpenWebUI Pipeline consumption."""
    ctx = get_pipeline_context()
    settings = get_settings()
    banners: list[dict] = []

    # ── Model status ──
    active_models: list[str] = []
    revoked_models: list[str] = []
    if ctx.control_store:
        try:
            attestations = ctx.control_store.list_attestations(settings.gateway_tenant_id)
            for att in attestations:
                model_id = att.get("model_id", "")
                status = att.get("status", "active")
                if status == "active":
                    active_models.append(model_id)
                else:
                    revoked_models.append(model_id)
                    banners.append({
                        "type": "error",
                        "text": f"Model {model_id} attestation {status} — model unavailable",
                    })
        except Exception as e:
            logger.warning("openwebui_status: failed to list attestations: %s", e)

    # ── Budget ──
    budget_info: dict | None = None
    if ctx.budget_tracker and settings.token_budget_enabled:
        try:
            snapshot = await ctx.budget_tracker.get_snapshot(settings.gateway_tenant_id)
            if snapshot and snapshot.get("max_tokens", 0) > 0:
                pct = snapshot.get("percent_used", 0.0)
                remaining = snapshot["max_tokens"] - snapshot.get("tokens_used", 0)
                budget_info = {
                    "percent_used": pct,
                    "tokens_remaining": max(0, remaining),
                    "tokens_used": snapshot.get("tokens_used", 0),
                    "max_tokens": snapshot["max_tokens"],
                    "period": snapshot.get("period", "monthly"),
                }
                if pct >= 100:
                    banners.append({"type": "error", "text": f"Token budget exhausted — {snapshot['max_tokens']} tokens used this {snapshot.get('period', 'month')}"})
                elif pct >= 90:
                    banners.append({"type": "warning", "text": f"Token budget at {pct:.0f}% — {remaining:,} tokens remaining"})
                elif pct >= 70:
                    banners.append({"type": "info", "text": f"Token budget at {pct:.0f}% — {remaining:,} tokens remaining"})
        except Exception as e:
            logger.warning("openwebui_status: failed to get budget snapshot: %s", e)

    # ── WAL health ──
    if ctx.wal_writer:
        try:
            disk_bytes = ctx.wal_writer.disk_usage_bytes()
            max_bytes = int(settings.wal_max_size_gb * (1024 ** 3))
            if max_bytes > 0 and disk_bytes / max_bytes >= settings.disk_degraded_threshold:
                banners.append({"type": "warning", "text": "Gateway storage nearing capacity — audit log may be truncated"})
        except Exception:
            pass

    return JSONResponse({
        "banners": banners,
        "budget": budget_info,
        "models_status": {
            "active": active_models,
            "revoked": revoked_models,
        },
    })
