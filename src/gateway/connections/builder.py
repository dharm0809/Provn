"""Per-tile builders, event merger, and rollup for GET /v1/connections.

Design reference: docs/plans/2026-04-23-connections-page-design.md §Tiles.

Every builder is pure: takes a PipelineContext (or any object with the
attributes it needs), returns the tile dict. Each builder is wrapped
by ``build_snapshot`` in its own try/except; the endpoint therefore
never 5xx's on probe failure — the tile instead goes ``status:"unknown"``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import datetime, timezone
from typing import Any

from gateway.util.time import iso8601_utc


async def _call_reader(fn, *args, **kwargs):
    """Invoke an async-or-sync lineage reader method uniformly.

    WalacorLineageReader exposes async methods; the local SQLite LineageReader
    exposes sync ones. Passing an async callable to asyncio.to_thread runs the
    callable in a thread but leaves the returned coroutine unawaited (→ a
    RuntimeWarning and a silently dropped result).
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)

logger = logging.getLogger(__name__)

TILE_ORDER: tuple[str, ...] = (
    "providers",
    "walacor_delivery",
    "analyzers",
    "tool_loop",
    "model_capabilities",
    "control_plane",
    "auth",
    "readiness",
    "streaming",
    "intelligence_worker",
)


def _tile(
    tile_id: str,
    *,
    status: str,
    headline: str,
    subline: str = "",
    last_change_ts: str | None = None,
    detail: dict | None = None,
) -> dict:
    return {
        "id": tile_id,
        "status": status,
        "headline": headline[:60],
        "subline": subline[:80],
        "last_change_ts": last_change_ts,
        "detail": detail or {},
    }


def _unknown_tile(tile_id: str, exc: Exception) -> dict:
    msg = str(exc)
    return _tile(
        tile_id,
        status="unknown",
        headline="probe failed",
        subline=msg[:80],
        detail={"error": msg},
    )


def _disabled_tile(tile_id: str, reason: str = "disabled") -> dict:
    return _tile(tile_id, status="unknown", headline=reason, subline="", detail={"enabled": False})


# ─────────────────────────────────────────────────────────────────────
# Tile builders
# ─────────────────────────────────────────────────────────────────────


def build_providers_tile(ctx: Any) -> dict:
    rm = getattr(ctx, "resource_monitor", None)
    if rm is None or not hasattr(rm, "snapshot"):
        return _disabled_tile("providers", "resource monitor disabled")
    snap = rm.snapshot() or {}
    providers = snap.get("providers", {}) or {}

    status = "green"
    any_cd = False
    any_amber = False
    worst_rate = 0.0
    for _, p in providers.items():
        rate = float(p.get("error_rate_60s") or 0.0)
        worst_rate = max(worst_rate, rate)
        if p.get("cooldown_until"):
            any_cd = True
        if rate > 0.20:
            any_amber = True
    if any_cd:
        status = "red"
    elif any_amber:
        status = "amber"

    n = len(providers)
    return _tile(
        "providers",
        status=status,
        headline=f"{n} provider(s)" if n else "no providers",
        subline=f"worst error rate {worst_rate:.0%}" if n else "no traffic yet",
        detail={"providers": providers},
    )


def build_walacor_delivery_tile(ctx: Any) -> dict:
    wc = getattr(ctx, "walacor_client", None)
    if wc is None or not hasattr(wc, "delivery_snapshot"):
        return _disabled_tile("walacor_delivery", "walacor disabled")
    snap = wc.delivery_snapshot() or {}
    success_rate = snap.get("success_rate_60s", 1.0)
    last_failure = snap.get("last_failure")
    last_success_ts = snap.get("last_success_ts")
    time_since = snap.get("time_since_last_success_s")

    # Distinguish "wal not attached" from "0 pending" — the subline below
    # only switches to "pending writes n/a" when pending is None, so we
    # must leave it None when there is no writer to probe.
    pending: int | None = None
    wal = getattr(ctx, "wal_writer", None)
    if wal is not None:
        try:
            pending = int(wal.pending_count())
        except Exception:
            logger.warning("wal pending_count failed for connections tile", exc_info=True)
            pending = None

    detail = {
        "success_rate_60s": success_rate,
        "pending_writes": pending,
        "last_failure": last_failure,
        "last_success_ts": last_success_ts,
        "time_since_last_success_s": time_since,
    }

    red = (success_rate < 0.5) or (time_since is not None and time_since > 120)
    amber = (not red) and (success_rate < 0.95)
    status = "red" if red else ("amber" if amber else "green")

    headline = (
        f"delivery {success_rate:.0%}"
        if success_rate < 1.0
        else "delivery healthy"
    )
    subline = f"{pending} pending writes" if pending is not None else "pending writes n/a"
    return _tile(
        "walacor_delivery",
        status=status,
        headline=headline,
        subline=subline,
        last_change_ts=(last_failure or {}).get("ts") if last_failure else last_success_ts,
        detail=detail,
    )


def build_analyzers_tile(ctx: Any) -> dict:
    analyzers = getattr(ctx, "content_analyzers", None) or []
    out: dict[str, dict] = {}
    worst = "green"
    latest_ts: str | None = None
    for a in analyzers:
        name = getattr(a, "analyzer_id", a.__class__.__name__)
        try:
            snap = a.fail_open_snapshot()
        except Exception as exc:  # per-analyzer probe failure doesn't kill the tile
            snap = {"fail_opens_60s": 0, "last_fail_open": None, "error": str(exc)}
        enabled = getattr(a, "enabled", True)
        snap = {"enabled": bool(enabled), **snap}
        out[name] = snap
        fo = int(snap.get("fail_opens_60s") or 0)
        last = snap.get("last_fail_open") or {}
        if last.get("ts"):
            latest_ts = max(latest_ts or "", last["ts"])
        if enabled and fo >= 5:
            worst = "red"
        elif enabled and fo >= 1 and worst != "red":
            worst = "amber"

    return _tile(
        "analyzers",
        status=worst,
        headline=f"{len(out)} analyzer(s)",
        subline=("fail-opens detected" if worst != "green" else "no recent fail-opens"),
        last_change_ts=latest_ts,
        detail={"analyzers": out},
    )


def build_tool_loop_tile(ctx: Any) -> dict:
    # Module-level accessors — independent of ctx
    from gateway.pipeline.tool_executor import tool_exceptions_snapshot
    snap = tool_exceptions_snapshot() or {}
    exceptions_60s = int(snap.get("exceptions_60s") or 0)
    last_exception = snap.get("last_exception")
    loops_60s = int(snap.get("loops_60s") or 0)
    failure_rate = float(snap.get("failure_rate_60s") or 0.0)
    if not failure_rate and loops_60s:
        failure_rate = exceptions_60s / loops_60s if loops_60s else 0.0

    detail = {
        "exceptions_60s": exceptions_60s,
        "last_exception": last_exception,
        "loops_60s": loops_60s,
        "failure_rate_60s": round(failure_rate, 4),
    }

    if failure_rate > 0.2:
        status = "red"
    elif failure_rate > 0.05 or exceptions_60s > 0:
        status = "amber"
    else:
        status = "green"

    return _tile(
        "tool_loop",
        status=status,
        headline=(
            f"{exceptions_60s} tool exception(s)"
            if exceptions_60s
            else "tool loop healthy"
        ),
        subline=f"{loops_60s} loops/60s",
        last_change_ts=(last_exception or {}).get("ts") if last_exception else None,
        detail=detail,
    )


def build_model_capabilities_tile(ctx: Any) -> dict:
    reg = getattr(ctx, "capability_registry", None)
    models: list[dict] = []
    auto_disabled = 0
    if reg is not None:
        caps = reg.all_capabilities() or {}
        for model_id, cap in (caps or {}).items():
            supports_tools = bool(cap.get("supports_tools")) if isinstance(cap, dict) else bool(cap)
            auto_flag = bool(cap.get("auto_disabled")) if isinstance(cap, dict) else False
            since = cap.get("since") if isinstance(cap, dict) else None
            if auto_flag:
                auto_disabled += 1
            models.append({
                "model_id": model_id,
                "supports_tools": supports_tools,
                "auto_disabled": auto_flag,
                "since": since,
            })

    status = "amber" if auto_disabled > 0 else "green"
    return _tile(
        "model_capabilities",
        status=status,
        headline=f"{len(models)} model(s) tracked",
        subline=(
            f"{auto_disabled} auto-disabled" if auto_disabled else "no auto-disabled models"
        ),
        detail={"models": models, "auto_disabled_count": auto_disabled},
    )


def build_control_plane_tile(ctx: Any) -> dict:
    from gateway.config import get_settings
    settings = get_settings()

    if getattr(ctx, "sync_client", None) is not None:
        mode = "remote"
    elif getattr(ctx, "control_store", None) is not None:
        mode = "embedded"
    else:
        mode = "disabled"

    pc = getattr(ctx, "policy_cache", None)
    version = str(pc.version) if pc is not None else ""
    last_sync_ts = None
    age_s: float | None = None
    stale = False
    if pc is not None:
        ls = pc.last_sync
        if ls is not None:
            last_sync_ts = ls.isoformat()
            age_s = (datetime.now(timezone.utc) - ls).total_seconds()
        try:
            stale = bool(pc.is_stale)
        except Exception:
            stale = False

    sync_task = getattr(ctx, "local_sync_task", None) or getattr(ctx, "sync_loop_task", None)
    sync_alive = True
    if sync_task is not None:
        try:
            sync_alive = not sync_task.done()
        except Exception:
            sync_alive = True
    elif mode == "disabled":
        sync_alive = True  # N/A

    attestations_count = 0
    ac = getattr(ctx, "attestation_cache", None)
    if ac is not None:
        try:
            attestations_count = int(ac.entry_count)
        except Exception:
            attestations_count = 0

    policies_count = 0
    if pc is not None:
        try:
            policies_count = len(pc.get_policies() or [])
        except Exception:
            policies_count = 0

    detail = {
        "mode": mode,
        "policy_cache": {
            "version": version,
            "last_sync_ts": last_sync_ts,
            "age_s": round(age_s, 1) if age_s is not None else None,
            "stale": stale,
        },
        "sync_task_alive": sync_alive,
        "attestations_count": attestations_count,
        "policies_count": policies_count,
    }

    interval = getattr(settings, "sync_interval", 60) or 60
    if not sync_alive or stale:
        status = "red"
    elif age_s is not None and age_s > interval * 2:
        status = "amber"
    else:
        status = "green"

    if mode == "disabled":
        # Non-governance deployment — tile is informational
        status = "green" if sync_alive else "amber"

    return _tile(
        "control_plane",
        status=status,
        headline=f"{mode} · {policies_count} policies",
        subline=f"{attestations_count} attestations",
        last_change_ts=last_sync_ts,
        detail=detail,
    )


def build_auth_tile(ctx: Any) -> dict:
    from gateway.config import get_settings
    settings = get_settings()

    auth_mode = getattr(settings, "auth_mode", "api_key")
    jwt_secret = getattr(settings, "jwt_secret", "") or ""
    jwt_jwks_url = getattr(settings, "jwt_jwks_url", "") or ""
    jwt_configured = bool(jwt_secret or jwt_jwks_url)

    jwks_last_fetch_ts: str | None = None
    jwks_last_error: dict | None = None

    # The auto-generated bootstrap key only matters when the operator has NOT
    # configured admin API keys AND the control plane is active — that's the
    # only case where main.py writes the wgk-*.txt file. When admin keys are
    # in use, skipping persistence is the correct, stable configuration; the
    # absent file is not a degradation.
    admin_keys_configured = bool(getattr(settings, "api_keys_list", None))
    bootstrap_applicable = bool(
        getattr(settings, "control_plane_enabled", False)
    ) and not admin_keys_configured

    bootstrap_stable: bool | None
    if bootstrap_applicable:
        try:
            from gateway.auth.bootstrap_key import bootstrap_key_stable
            bootstrap_stable = bool(bootstrap_key_stable(settings.wal_path))
        except Exception:
            bootstrap_stable = None  # probe failed — don't flag as amber
    else:
        bootstrap_stable = None  # not applicable → exclude from status rollup

    detail = {
        "auth_mode": auth_mode,
        "jwt_configured": jwt_configured,
        "admin_api_keys_configured": admin_keys_configured,
        "jwks_last_fetch_ts": jwks_last_fetch_ts,
        "jwks_last_error": jwks_last_error,
        "bootstrap_key_stable": bootstrap_stable,
    }

    now = time.time()
    recent_jwks_err = False
    if jwks_last_error and jwks_last_error.get("ts"):
        try:
            then = datetime.fromisoformat(jwks_last_error["ts"].replace("Z", "+00:00")).timestamp()
            recent_jwks_err = (now - then) <= 60.0
        except Exception:
            recent_jwks_err = False

    if recent_jwks_err:
        status = "red"
    elif bootstrap_stable is False:
        status = "amber"
    else:
        status = "green"

    if bootstrap_stable is False:
        subline = "bootstrap key rotating — persistence failed"
    elif admin_keys_configured:
        subline = "JWT + API-key" if jwt_configured else "API-key"
    else:
        subline = "JWT configured" if jwt_configured else "bootstrap key only"

    return _tile(
        "auth",
        status=status,
        headline=f"auth: {auth_mode}",
        subline=subline,
        detail=detail,
    )


def _map_readiness_status(rollup: str) -> str:
    return {"ready": "green", "degraded": "amber", "unready": "red"}.get(rollup, "unknown")


async def build_readiness_tile(ctx: Any) -> dict:
    from gateway.readiness.runner import run_all
    report = await run_all(ctx)
    checks = report.checks or []
    reds = [
        {"check_id": c.get("id"), "detail": c.get("detail")}
        for c in checks
        if c.get("status") == "red"
    ]
    ambers = [
        {"check_id": c.get("id"), "detail": c.get("detail")}
        for c in checks
        if c.get("status") == "amber"
    ]

    # Rows in last 24h from attempts where disposition="readiness_degraded"
    degraded_rows_24h = 0
    reader = getattr(ctx, "lineage_reader", None)
    if reader is not None:
        try:
            res = await _call_reader(
                reader.get_attempts, limit=200, disposition="readiness_degraded",
            )
            items = (res or {}).get("items") or []
            cutoff = datetime.now(timezone.utc).timestamp() - 86400.0
            for row in items:
                ts_raw = row.get("timestamp")
                try:
                    if isinstance(ts_raw, (int, float)):
                        ts = float(ts_raw)
                    else:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if ts >= cutoff:
                    degraded_rows_24h += 1
        except Exception as exc:
            logger.debug("readiness attempts lookup failed: %s", exc)

    detail = {
        "rollup": report.status,
        "reds": reds,
        "ambers": ambers,
        "degraded_rows_24h": degraded_rows_24h,
    }
    status = _map_readiness_status(report.status)
    return _tile(
        "readiness",
        status=status,
        headline=f"readiness: {report.status}",
        subline=f"{len(reds)} red · {len(ambers)} amber",
        last_change_ts=report.generated_at,
        detail=detail,
    )


def build_streaming_tile(ctx: Any) -> dict:
    from gateway.pipeline.forwarder import stream_interruptions_snapshot
    snap = stream_interruptions_snapshot() or {}
    interruptions = int(snap.get("interruptions_60s") or 0)
    last_interruption = snap.get("last_interruption")
    streams_60s = int(snap.get("streams_60s") or 0)
    rate = float(snap.get("interruption_rate_60s") or 0.0)
    if not rate and streams_60s:
        rate = interruptions / streams_60s

    detail = {
        "interruptions_60s": interruptions,
        "last_interruption": last_interruption,
        "streams_60s": streams_60s,
        "interruption_rate_60s": round(rate, 4),
    }

    if rate > 0.3:
        status = "red"
    elif rate > 0.1 or interruptions > 0:
        status = "amber"
    else:
        status = "green"

    return _tile(
        "streaming",
        status=status,
        headline=(
            f"{interruptions} interruption(s)" if interruptions else "streaming healthy"
        ),
        subline=f"{streams_60s} streams/60s",
        last_change_ts=(last_interruption or {}).get("ts") if last_interruption else None,
        detail=detail,
    )


def _read_intelligence_db_counters(db_path: str) -> tuple[int, Any]:
    """Synchronous SQLite read — runs in a worker thread so the event loop stays free."""
    import sqlite3
    verdict_log_rows = 0
    last_training_at = None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM onnx_verdicts").fetchone()
            verdict_log_rows = int(row[0]) if row else 0
        except Exception:
            verdict_log_rows = 0
        try:
            row = conn.execute("SELECT MAX(created_at) FROM training_snapshots").fetchone()
            last_training_at = row[0] if row and row[0] else None
        except Exception:
            last_training_at = None
    finally:
        conn.close()
    return verdict_log_rows, last_training_at


async def build_intelligence_worker_tile(ctx: Any) -> dict:
    worker = getattr(ctx, "intelligence_worker", None)
    last_training_at = None
    verdict_log_rows = 0
    db = getattr(ctx, "intelligence_db", None)
    if db is not None:
        try:
            verdict_log_rows, last_training_at = await asyncio.to_thread(
                _read_intelligence_db_counters, db.path,
            )
        except Exception:
            pass

    if worker is None:
        return _tile(
            "intelligence_worker",
            status="unknown",
            headline="disabled",
            subline="intelligence worker not configured",
            detail={
                "running": False,
                "queue_depth": 0,
                "oldest_job_age_s": 0.0,
                "last_error": None,
                "last_training_at": last_training_at,
                "verdict_log_rows": verdict_log_rows,
                "enabled": False,
            },
        )

    snap = worker.snapshot() or {}
    running = bool(snap.get("running"))
    queue_depth = int(snap.get("queue_depth") or 0)
    oldest_job_age = float(snap.get("oldest_job_age_s") or 0.0)
    last_error = snap.get("last_error")

    detail = {
        "running": running,
        "queue_depth": queue_depth,
        "oldest_job_age_s": oldest_job_age,
        "last_error": last_error,
        "last_training_at": last_training_at,
        "verdict_log_rows": verdict_log_rows,
    }

    now = time.time()
    recent_err = False
    if last_error and last_error.get("ts"):
        try:
            then = datetime.fromisoformat(
                last_error["ts"].replace("Z", "+00:00")
            ).timestamp()
            recent_err = (now - then) <= 60.0
        except Exception:
            recent_err = False

    if not running or recent_err:
        status = "red"
    elif queue_depth > 100 or oldest_job_age > 60:
        status = "amber"
    else:
        status = "green"

    return _tile(
        "intelligence_worker",
        status=status,
        headline=("intelligence running" if running else "intelligence stopped"),
        subline=f"queue {queue_depth} · {verdict_log_rows} verdicts",
        last_change_ts=(last_error or {}).get("ts") if last_error else last_training_at,
        detail=detail,
    )


# ─────────────────────────────────────────────────────────────────────
# Events merger
# ─────────────────────────────────────────────────────────────────────


def _ts_to_epoch(ts_val: Any) -> float | None:
    if ts_val is None:
        return None
    if isinstance(ts_val, (int, float)):
        return float(ts_val)
    try:
        return datetime.fromisoformat(str(ts_val).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _evt(ts: float, subsystem: str, severity: str, message: str, **attrs: Any) -> dict:
    return {
        "ts": iso8601_utc(ts),
        "subsystem": subsystem,
        "severity": severity,
        "message": (message or "")[:140],
        "session_id": None,
        "execution_id": None,
        "request_id": None,
        "attributes": attrs,
    }


async def build_events(ctx: Any, *, cap: int = 50) -> list[dict]:
    """Merge in-memory deques across subsystems. Sort newest-first, cap 50."""
    events: list[tuple[float, dict]] = []

    # Walacor delivery — failures only
    wc = getattr(ctx, "walacor_client", None)
    if wc is not None:
        try:
            for ts, op, ok, detail in list(getattr(wc, "_delivery_log", []) or []):
                if ok:
                    continue
                events.append((
                    ts,
                    _evt(
                        ts,
                        "walacor_delivery",
                        "red",
                        f"walacor {op} failed: {detail or 'unknown'}",
                        op=op,
                        detail=detail,
                    ),
                ))
        except Exception:
            pass

    # Analyzers
    for a in getattr(ctx, "content_analyzers", None) or []:
        try:
            log = getattr(a, "_fail_open_log", None) or []
            name = getattr(a, "analyzer_id", a.__class__.__name__)
            for ts, reason in list(log):
                events.append((
                    ts,
                    _evt(
                        ts,
                        "analyzers",
                        "amber",
                        f"{name} fail-open: {reason}",
                        analyzer=name,
                        reason=reason,
                    ),
                ))
        except Exception:
            continue

    # Tool loop
    try:
        from gateway.pipeline.tool_executor import _tool_exception_log
        for ts, tool, err in list(_tool_exception_log):
            events.append((
                ts,
                _evt(
                    ts,
                    "tool_loop",
                    "amber",
                    f"{tool} exception: {err}",
                    tool=tool,
                    error=err,
                ),
            ))
    except Exception:
        pass

    # Streaming
    try:
        from gateway.pipeline.forwarder import _stream_interruption_log
        for ts, provider, detail in list(_stream_interruption_log):
            events.append((
                ts,
                _evt(
                    ts,
                    "streaming",
                    "amber",
                    f"{provider} interrupted: {detail}",
                    provider=provider,
                    detail=detail,
                ),
            ))
    except Exception:
        pass

    # Intelligence worker last_error within 60s
    worker = getattr(ctx, "intelligence_worker", None)
    now = time.time()
    if worker is not None:
        try:
            last_err = getattr(worker, "_last_error", None)
            if last_err is not None:
                ts, detail = last_err
                if now - ts <= 60.0:
                    events.append((
                        ts,
                        _evt(
                            ts,
                            "intelligence_worker",
                            "red",
                            f"intelligence worker error: {detail}",
                            detail=detail,
                        ),
                    ))
        except Exception:
            pass

    # ResourceMonitor provider last errors within 60s
    rm = getattr(ctx, "resource_monitor", None)
    if rm is not None:
        try:
            last = getattr(rm, "_last_error", {}) or {}
            for provider, entry in last.items():
                if not entry:
                    continue
                # Tolerate legacy plain-string entries (pre-timestamp fix).
                if isinstance(entry, tuple) and len(entry) == 2:
                    ts, err = entry
                else:
                    ts, err = now, entry
                if not err or now - ts > 60.0:
                    continue
                events.append((
                    ts,
                    _evt(
                        ts,
                        "providers",
                        "amber",
                        f"{provider} last error: {err}",
                        provider=provider,
                        detail=err,
                    ),
                ))
        except Exception:
            pass

    # Readiness drift rows (disposition="readiness_degraded") — newest 20
    reader = getattr(ctx, "lineage_reader", None)
    if reader is not None:
        try:
            res = await _call_reader(
                reader.get_attempts, limit=20, disposition="readiness_degraded",
            )
            for row in (res or {}).get("items") or []:
                ts_epoch = _ts_to_epoch(row.get("timestamp")) or now
                events.append((
                    ts_epoch,
                    _evt(
                        ts_epoch,
                        "readiness",
                        "amber",
                        f"readiness drift: {row.get('reason') or row.get('disposition')}",
                        request_id=row.get("request_id"),
                    ),
                ))
        except Exception:
            pass

    events.sort(key=lambda pair: pair[0], reverse=True)
    return [e for _, e in events[:cap]]


# ─────────────────────────────────────────────────────────────────────
# Rollup + snapshot
# ─────────────────────────────────────────────────────────────────────


def compute_rollup(tiles: list[dict]) -> str:
    statuses = [t.get("status") for t in tiles]
    if any(s == "red" for s in statuses):
        return "red"
    if any(s == "amber" for s in statuses):
        return "amber"
    return "green"


_SYNC_BUILDERS: dict[str, Any] = {
    "providers": build_providers_tile,
    "walacor_delivery": build_walacor_delivery_tile,
    "analyzers": build_analyzers_tile,
    "tool_loop": build_tool_loop_tile,
    "model_capabilities": build_model_capabilities_tile,
    "control_plane": build_control_plane_tile,
    "auth": build_auth_tile,
    "streaming": build_streaming_tile,
}

_ASYNC_BUILDERS: dict[str, Any] = {
    "readiness": build_readiness_tile,
    "intelligence_worker": build_intelligence_worker_tile,
}


async def _safe_build(tile_id: str, ctx: Any) -> dict:
    try:
        if tile_id in _ASYNC_BUILDERS:
            return await _ASYNC_BUILDERS[tile_id](ctx)
        fn = _SYNC_BUILDERS[tile_id]
        return fn(ctx)
    except Exception as exc:
        logger.warning("connections: %s tile probe failed: %s", tile_id, exc)
        return _unknown_tile(tile_id, exc)


async def build_snapshot(ctx: Any) -> dict:
    tiles: list[dict] = []
    for tile_id in TILE_ORDER:
        tile = await _safe_build(tile_id, ctx)
        tiles.append(tile)

    try:
        events = await build_events(ctx)
    except Exception as exc:
        logger.warning("connections: events merger failed: %s", exc)
        events = []

    return {
        "generated_at": iso8601_utc(time.time()),
        "ttl_seconds": 3,
        "overall_status": compute_rollup(tiles),
        "tiles": tiles,
        "events": events,
    }
