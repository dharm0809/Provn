"""Phase 25 Phase G: intelligence control-plane API.

Routes under `/v1/control/intelligence/...` expose the intelligence
layer's runtime state (production models, candidates, lifecycle history,
verdict log) plus the actions an operator can take (promote, reject,
rollback, force retrain).

Auth: every path under `/v1/control/...` is already gated by
`api_key_middleware` in main.py, so individual handlers don't repeat
that check. Handlers do however check for the presence of the
dependencies they need (registry, DB, distillation worker) and return
503 when the intelligence layer is not initialized.

Response shape: every endpoint returns JSON. Errors use
`{"error": "human description"}` with an appropriate status code —
matching the existing `control/api.py` convention so dashboard code can
share error handling.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)


# ── Dependency helpers ─────────────────────────────────────────────────────


def _require_registry():
    ctx = get_pipeline_context()
    return ctx.model_registry


def _require_db():
    ctx = get_pipeline_context()
    return ctx.intelligence_db


def _503(reason: str) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=503)


# ── Task 26: read endpoints ────────────────────────────────────────────────


async def list_production_models(request: Request) -> JSONResponse:
    """List production models with last-promotion metadata.

    For each model in `registry.list_production_models()` we look up the
    most recent `model_promoted` event in `lifecycle_events_mirror` and
    surface its payload (candidate_version, approver, shadow_metrics
    summary). Models that exist in production without any promotion
    event (e.g. the initial packaged-baseline seed from Task 12) show
    up with a null `last_promotion` block so the dashboard can render
    "baseline".
    """
    registry = _require_registry()
    if registry is None:
        return _503("model registry not initialized")

    production = registry.list_production_models()
    last_promotions = _last_promotion_per_model(_require_db())

    models = []
    for name in sorted(production):
        prod_path = registry.production_path(name)
        stat = prod_path.stat() if prod_path.exists() else None
        models.append({
            "model_name": name,
            "path": str(prod_path),
            "size_bytes": stat.st_size if stat else 0,
            "mtime": stat.st_mtime if stat else None,
            "generation": registry.get_generation(name),
            "last_promotion": last_promotions.get(name),
        })
    return JSONResponse({"models": models})


async def list_candidates(request: Request) -> JSONResponse:
    """List candidates with shadow status + latest metrics.

    For each candidate we include the `active_shadow` flag (matches the
    registry's per-model marker) and, if a corresponding
    `shadow_validation_complete` row exists in the lifecycle mirror,
    its `passed` flag and metrics summary.
    """
    registry = _require_registry()
    if registry is None:
        return _503("model registry not initialized")

    db = _require_db()
    cands = registry.list_candidates()
    # Map model → active shadow version for quick lookup.
    active: dict[str, str] = {}
    for name in sorted({c.model for c in cands}):
        act = registry.active_candidate(name)
        if act is not None:
            active[name] = act.version

    # Shadow-complete events keyed by (model, version).
    shadow_complete = _latest_shadow_complete_events(db) if db is not None else {}

    rows = []
    for cand in cands:
        path = cand.path
        stat = path.stat() if path.exists() else None
        key = (cand.model, cand.version)
        sc = shadow_complete.get(key) or {}
        rows.append({
            "model_name": cand.model,
            "version": cand.version,
            "path": str(path),
            "size_bytes": stat.st_size if stat else 0,
            "mtime": stat.st_mtime if stat else None,
            "active_shadow": active.get(cand.model) == cand.version,
            "shadow_validation": {
                "completed": bool(sc),
                "passed": sc.get("passed") if sc else None,
                "metrics": sc.get("metrics") if sc else None,
            },
        })
    return JSONResponse({"candidates": rows})


async def model_history(request: Request) -> JSONResponse:
    """Lifecycle-event history for one model.

    Reads `lifecycle_events_mirror` where the payload's `model_name`
    matches the path parameter. Paginated via `?limit=` (default 50,
    max 500) and ordered newest-first.
    """
    model_name = request.path_params.get("model", "")
    db = _require_db()
    if db is None:
        return _503("intelligence db not initialized")

    try:
        limit = max(1, min(500, int(request.query_params.get("limit", "50"))))
    except ValueError:
        limit = 50

    rows = _query_history(db, model_name, limit)
    return JSONResponse({"model_name": model_name, "events": rows})


# ── SQL helpers (pure functions for testability) ───────────────────────────


def _last_promotion_per_model(db) -> dict[str, dict[str, Any]]:
    if db is None:
        return {}
    sql = """
        SELECT payload_json, timestamp, walacor_record_id
        FROM lifecycle_events_mirror
        WHERE event_type = 'model_promoted'
        ORDER BY written_at DESC
    """
    out: dict[str, dict[str, Any]] = {}
    conn = sqlite3.connect(db.path)
    try:
        for payload_json, ts, wal_id in conn.execute(sql):
            try:
                payload = json.loads(payload_json)
            except (ValueError, TypeError):
                continue
            name = payload.get("model_name")
            if not isinstance(name, str) or name in out:
                # Only keep the most recent — the SQL ordered DESC so
                # the first seen per model wins.
                continue
            out[name] = {
                "candidate_version": payload.get("candidate_version"),
                "approver": payload.get("approver"),
                "dataset_hash": payload.get("dataset_hash"),
                "shadow_metrics": payload.get("shadow_metrics"),
                "timestamp": ts,
                "walacor_record_id": wal_id,
            }
    finally:
        conn.close()
    return out


def _latest_shadow_complete_events(db) -> dict[tuple[str, str], dict[str, Any]]:
    sql = """
        SELECT payload_json
        FROM lifecycle_events_mirror
        WHERE event_type = 'shadow_validation_complete'
        ORDER BY written_at DESC
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    conn = sqlite3.connect(db.path)
    try:
        for (payload_json,) in conn.execute(sql):
            try:
                payload = json.loads(payload_json)
            except (ValueError, TypeError):
                continue
            name = payload.get("model_name")
            version = payload.get("candidate_version")
            if not (isinstance(name, str) and isinstance(version, str)):
                continue
            key = (name, version)
            if key in out:
                continue
            out[key] = payload
    finally:
        conn.close()
    return out


# ── Task 27: promote / reject / rollback ──────────────────────────────────


def _caller_identity(request: Request) -> str:
    """Best-effort approver id from the request.

    Priority: `request.state.caller_identity.user_id` (set by jwt_auth
    or header-identity middleware) → `X-User-Id` header → "anonymous".
    """
    ident = getattr(request.state, "caller_identity", None)
    if ident is not None:
        uid = getattr(ident, "user_id", None)
        if isinstance(uid, str) and uid:
            return uid
    hdr = request.headers.get("X-User-Id") if request.headers else None
    if isinstance(hdr, str) and hdr.strip():
        return hdr.strip()
    return "anonymous"


async def promote_candidate(request: Request) -> JSONResponse:
    """Promote a candidate to production.

    Idempotency: if the most recent production promotion event for
    `model` already references `version`, return 409. Otherwise call
    `registry.promote(model, version)`, archive the previous
    production, clear the shadow marker, and emit a `model_promoted`
    lifecycle event with the caller's identity as approver.
    """
    from gateway.intelligence.events import build_promotion_event

    model = request.path_params.get("model", "")
    version = request.path_params.get("version", "")
    ctx = get_pipeline_context()
    registry = ctx.model_registry
    db = ctx.intelligence_db
    if registry is None:
        return _503("model registry not initialized")

    candidate_file = registry.base / "candidates" / f"{model}-{version}.onnx"
    if not candidate_file.exists():
        return JSONResponse(
            {"error": f"candidate {model}-{version} not found"},
            status_code=404,
        )

    # Idempotency: look at `lifecycle_events_mirror` for a prior
    # promotion of this exact version. Only check when the DB is
    # available — without it, we can't tell, so fall through.
    if db is not None:
        last = _last_promotion_per_model(db).get(model)
        if last and last.get("candidate_version") == version:
            return JSONResponse(
                {
                    "error": f"{model} version {version} already promoted",
                    "promoted_at": last.get("timestamp"),
                },
                status_code=409,
            )

    try:
        await registry.promote(model, version)
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"candidate {model}-{version} not found"},
            status_code=404,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    registry.disable_shadow(model)

    approver = _caller_identity(request)
    event = build_promotion_event(
        model_name=model,
        candidate_version=version,
        dataset_hash="",  # filled in by caller when known
        shadow_metrics={},  # manual promotion doesn't re-run the gate
        approver=approver,
    )
    await _write_lifecycle_event(ctx, event)

    return JSONResponse({
        "promoted": True,
        "model_name": model,
        "version": version,
        "approver": approver,
    })


async def reject_candidate(request: Request) -> JSONResponse:
    """Reject a candidate — move its `.onnx` to `archive/failed/`.

    Clears any shadow marker pointing at this version, emits a
    `model_rejected` event with stage="manual" and the caller-provided
    reason (from `?reason=` or JSON body).
    """
    from gateway.intelligence.events import build_model_rejected

    model = request.path_params.get("model", "")
    version = request.path_params.get("version", "")
    ctx = get_pipeline_context()
    registry = ctx.model_registry
    if registry is None:
        return _503("model registry not initialized")

    candidate_file = registry.base / "candidates" / f"{model}-{version}.onnx"
    if not candidate_file.exists():
        return JSONResponse(
            {"error": f"candidate {model}-{version} not found"},
            status_code=404,
        )

    reason = (request.query_params.get("reason") or "").strip() or "manual_rejection"

    # Move to archive/failed — keeps the file around for later review
    # without leaving it in the candidates list.
    failed_dir = registry.base / "archive" / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / candidate_file.name
    try:
        import os
        os.rename(candidate_file, dest)
    except OSError as e:
        return JSONResponse(
            {"error": f"failed to move candidate to archive/failed: {e}"},
            status_code=500,
        )

    # Clear the shadow marker if it points at this version.
    active = registry.active_candidate(model)
    if active is None or active.version == version:
        # `active_candidate` returns None after we moved the file — the
        # stale-marker cleanup in the registry handles the common case,
        # but disable_shadow is still the explicit intent here.
        registry.disable_shadow(model)

    event = build_model_rejected(
        model_name=model,
        candidate_version=version,
        reason=reason,
        stage="manual",
    )
    await _write_lifecycle_event(ctx, event)

    return JSONResponse({
        "rejected": True,
        "model_name": model,
        "version": version,
        "reason": reason,
        "archived_to": str(dest),
    })


async def rollback_model(request: Request) -> JSONResponse:
    """Rollback `model` to the most recently archived production file.

    Archive files live at `archive/{model}-archived-<ISO>.onnx` (Task
    10 naming). We pick the lexicographically-latest — ISO-8601
    timestamps sort correctly — and call `registry.rollback`.
    """
    from gateway.intelligence.events import build_promotion_event

    model = request.path_params.get("model", "")
    ctx = get_pipeline_context()
    registry = ctx.model_registry
    if registry is None:
        return _503("model registry not initialized")

    archive_dir = registry.base / "archive"
    if not archive_dir.is_dir():
        return JSONResponse(
            {"error": f"no archive directory found"}, status_code=404,
        )
    matches = sorted(
        p for p in archive_dir.iterdir()
        if p.is_file()
        and p.suffix == ".onnx"
        and p.name.startswith(f"{model}-")
    )
    if not matches:
        return JSONResponse(
            {"error": f"no archived versions for {model}"},
            status_code=404,
        )
    target = matches[-1]

    try:
        await registry.rollback(model, target.name)
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"archive file {target.name} vanished"},
            status_code=404,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    approver = _caller_identity(request)
    event = build_promotion_event(
        model_name=model,
        candidate_version=f"rollback:{target.name}",
        dataset_hash="",
        shadow_metrics={"source": "rollback"},
        approver=approver,
    )
    await _write_lifecycle_event(ctx, event)

    return JSONResponse({
        "rolled_back": True,
        "model_name": model,
        "restored_from": target.name,
        "approver": approver,
    })


# ── Task 28: force retrain ──────────────────────────────────────────────


_retrain_tasks: dict[str, Any] = {}


async def force_retrain(request: Request) -> JSONResponse:
    """Kick an immediate training pass for one model.

    Returns 202 with a job_id so the dashboard can poll. The actual
    training runs in a detached `asyncio.Task`; completion shows up on
    `/v1/control/intelligence/candidates` when the worker writes the
    new candidate file.
    """
    from gateway.intelligence.registry import ALLOWED_MODEL_NAMES
    import asyncio
    import uuid

    model = request.path_params.get("model", "")
    if model not in ALLOWED_MODEL_NAMES:
        return JSONResponse(
            {"error": f"unknown model name {model!r}"},
            status_code=400,
        )

    ctx = get_pipeline_context()
    worker = ctx.distillation_worker
    if worker is None:
        return _503("distillation worker not initialized")

    job_id = str(uuid.uuid4())
    task = asyncio.create_task(
        worker.retrain_one(model),
        name=f"retrain-{model}-{job_id}",
    )
    _retrain_tasks[job_id] = task
    # Prune completed tasks so the dict doesn't grow without bound.
    _reap_retrain_tasks()

    return JSONResponse(
        {
            "job_id": job_id,
            "model_name": model,
            "status": "accepted",
        },
        status_code=202,
    )


def _reap_retrain_tasks() -> None:
    """Drop completed retrain tasks from the tracking dict."""
    done = [k for k, t in _retrain_tasks.items() if t.done()]
    for k in done:
        _retrain_tasks.pop(k, None)


async def _write_lifecycle_event(ctx, event) -> None:
    """Best-effort emit via the pipeline writer. Fail-open."""
    writer = getattr(ctx, "lifecycle_event_writer", None)
    if writer is None:
        logger.debug("no lifecycle writer; skipping %s", event.event_type.value)
        return
    try:
        await writer.write_event(event)
    except Exception:
        logger.warning(
            "lifecycle write failed (event=%s)",
            event.event_type.value, exc_info=True,
        )


def _query_history(db, model_name: str, limit: int) -> list[dict[str, Any]]:
    """Return lifecycle events whose payload `model_name` equals `model_name`."""
    sql = """
        SELECT event_type, payload_json, timestamp, walacor_record_id,
               write_status, error_reason, attempts, written_at
        FROM lifecycle_events_mirror
        ORDER BY written_at DESC
    """
    out: list[dict[str, Any]] = []
    conn = sqlite3.connect(db.path)
    try:
        for row in conn.execute(sql):
            (event_type, payload_json, ts, wal_id, status,
             error_reason, attempts, written_at) = row
            try:
                payload = json.loads(payload_json)
            except (ValueError, TypeError):
                continue
            if payload.get("model_name") != model_name:
                continue
            out.append({
                "event_type": event_type,
                "timestamp": ts,
                "written_at": written_at,
                "walacor_record_id": wal_id,
                "write_status": status,
                "error_reason": error_reason,
                "attempts": attempts,
                "payload": payload,
            })
            if len(out) >= limit:
                break
    finally:
        conn.close()
    return out
