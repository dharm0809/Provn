"""GET /health and GET /metrics endpoints."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.config import get_settings
from gateway.pipeline.context import get_pipeline_context

_start_time = time.time()


async def health_response(request: Request) -> JSONResponse:
    """Return health status with cache and WAL details when governance enabled."""
    settings = get_settings()
    ctx = get_pipeline_context()

    payload = {
        "status": "healthy",
        "gateway_id": settings.gateway_id,
        "tenant_id": settings.gateway_tenant_id,
        "enforcement_mode": settings.enforcement_mode,
        "uptime_seconds": int(time.time() - _start_time),
    }

    attestation_stale = False
    if not ctx.skip_governance and ctx.attestation_cache is not None:
        last_att = ctx.sync_client.last_attestation_sync if ctx.sync_client else None
        if last_att:
            elapsed = (datetime.now(timezone.utc) - last_att).total_seconds()
            attestation_stale = elapsed > settings.attestation_cache_ttl
        payload["attestation_cache"] = {
            "entries": ctx.attestation_cache.entry_count,
            "last_sync": last_att.isoformat() if last_att else None,
            "stale": attestation_stale,
        }
    if not ctx.skip_governance and ctx.policy_cache is not None:
        payload["policy_cache"] = {
            "version": ctx.policy_cache.version,
            "last_sync": ctx.policy_cache.last_sync.isoformat() if ctx.policy_cache.last_sync else None,
            "stale": ctx.policy_cache.is_stale,
        }
    if ctx.walacor_client:
        payload["storage"] = {
            "backend": "walacor",
            "server": settings.walacor_server,
            "executions_etid": settings.walacor_executions_etid,
            "attempts_etid": settings.walacor_attempts_etid,
        }

    if ctx.wal_writer is not None:
        pending = ctx.wal_writer.pending_count()
        disk = ctx.wal_writer.disk_usage_bytes()
        max_bytes = int(settings.wal_max_size_gb * (1024**3)) if settings.wal_max_size_gb else 0
        disk_pct = (disk / max_bytes * 100) if max_bytes else 0
        oldest = ctx.wal_writer.oldest_pending_seconds()
        high_water = settings.wal_high_water_mark
        payload["wal"] = {
            "pending_records": pending,
            "oldest_pending_seconds": oldest,
            "disk_usage_bytes": disk,
            "disk_usage_percent": disk_pct,
        }
        # Capacity / sync gate applies only when full governance is active
        if not ctx.skip_governance:
            if ctx.policy_cache and ctx.policy_cache.is_stale:
                payload["status"] = "fail_closed"
            elif attestation_stale:
                payload["status"] = "fail_closed"
            elif disk_pct >= 100 or pending >= high_water:
                payload["status"] = "fail_closed"
            elif pending > high_water * settings.disk_degraded_threshold or disk_pct >= settings.disk_degraded_threshold * 100:
                payload["status"] = "degraded"

    # Phase 11: token budget snapshot
    if ctx.budget_tracker and settings.token_budget_enabled:
        snapshot = await ctx.budget_tracker.get_snapshot(settings.gateway_tenant_id)
        if snapshot:
            payload["token_budget"] = snapshot

    # Content analyzers count
    if ctx.content_analyzers:
        payload["content_analyzers"] = len(ctx.content_analyzers)

    # Phase 13: session chain
    if ctx.session_chain:
        count = ctx.session_chain.active_session_count()
        # Redis tracker returns -1 as a sentinel (SCAN-by-prefix is too expensive)
        payload["session_chain"] = {"active_sessions": count if count >= 0 else "unavailable"}

    # Model capability registry
    if ctx.capability_registry:
        caps = ctx.capability_registry.all_capabilities()
        if caps:
            payload["model_capabilities"] = caps

    # Phase 23: Resource monitor status
    if ctx.resource_monitor:
        try:
            res_status = await ctx.resource_monitor.check()
            payload["resource_monitor"] = {
                "disk_free_pct": res_status.disk_free_pct,
                "disk_healthy": res_status.disk_healthy,
                "active_requests": res_status.active_requests,
                "provider_error_rates": res_status.provider_error_rates,
            }
        except Exception:
            pass

    if ctx.startup_probe_results:
        payload["startup_probes"] = {
            name: {"healthy": r.healthy, **r.detail}
            for name, r in ctx.startup_probe_results.items()
        }

    return JSONResponse(payload)


async def metrics_response(request: Request) -> Response:
    """Return Prometheus text format. Gauges updated from current context."""
    from gateway.metrics.prometheus import (
        get_metrics_content,
        wal_pending,
        wal_disk_bytes,
        wal_oldest_pending_seconds,
        cache_entries,
        sync_last_success_seconds,
        session_chain_active,
    )
    if not get_settings().metrics_enabled:
        return Response(status_code=404)
    ctx = get_pipeline_context()
    if ctx.wal_writer:
        wal_pending.set(ctx.wal_writer.pending_count())
        wal_disk_bytes.set(ctx.wal_writer.disk_usage_bytes())
        oldest = ctx.wal_writer.oldest_pending_seconds()
        wal_oldest_pending_seconds.set(oldest if oldest is not None else 0)
    if ctx.attestation_cache and ctx.sync_client and ctx.sync_client.last_attestation_sync:
        elapsed = (datetime.now(timezone.utc) - ctx.sync_client.last_attestation_sync).total_seconds()
        sync_last_success_seconds.labels(cache_type="attestation").set(elapsed)
    if ctx.policy_cache and ctx.policy_cache.last_sync:
        elapsed = (datetime.now(timezone.utc) - ctx.policy_cache.last_sync).total_seconds()
        sync_last_success_seconds.labels(cache_type="policy").set(elapsed)
    if ctx.attestation_cache:
        cache_entries.labels(cache_type="attestation").set(ctx.attestation_cache.entry_count)
    if ctx.policy_cache:
        cache_entries.labels(cache_type="policy").set(len(ctx.policy_cache.get_policies()))
    if ctx.session_chain:
        count = ctx.session_chain.active_session_count()
        # -1 = Redis mode (SCAN-by-prefix too expensive); set gauge to -1 so
        # Prometheus operators see an explicit sentinel rather than a stale 0.
        session_chain_active.set(count if count >= 0 else -1)
    return Response(get_metrics_content(), media_type="text/plain; charset=utf-8")
