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
