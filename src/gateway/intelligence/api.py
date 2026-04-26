"""intelligence control-plane API.

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

import asyncio
import json
import logging
import sqlite3
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)


# Per-model serialization for promote_candidate. The registry already
# serializes the physical rename via `lock_for(model)`, but the HTTP
# handler reads the mirror table *before* the rename and writes the
# lifecycle event *after*. Without an outer lock two concurrent callers
# for the same (model, version) both pass the empty-mirror check, both
# enter the registry, and the loser raises FileNotFoundError (the winner
# already renamed the candidate) instead of getting a clean 409. Holding
# a per-model asyncio.Lock across the full check→promote→mirror-write
# sequence eliminates the race.
_promote_locks: dict[str, asyncio.Lock] = {}


def _promote_lock(model: str) -> asyncio.Lock:
    lock = _promote_locks.get(model)
    if lock is None:
        lock = asyncio.Lock()
        _promote_locks[model] = lock
    return lock


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
        try:
            stat = prod_path.stat()
            status = "loaded"
            size = stat.st_size
            mtime = stat.st_mtime
            error: str | None = None
        except FileNotFoundError:
            status = "missing"
            size = 0
            mtime = None
            error = f"file not found: {prod_path}"
        except PermissionError as exc:
            status = "unreadable"
            size = 0
            mtime = None
            error = f"permission denied: {exc}"
        except OSError as exc:
            status = "unreadable"
            size = 0
            mtime = None
            error = f"stat failed: {exc}"
        models.append({
            "model_name": name,
            "path": str(prod_path),
            "size_bytes": size,
            "mtime": mtime,
            "generation": registry.get_generation(name),
            "last_promotion": last_promotions.get(name),
            "status": status,
            "error": error,
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

    # Validate model up-front so lock creation can't be abused to seed
    # entries in `_promote_locks` for junk names.
    try:
        from gateway.intelligence.registry import ALLOWED_MODEL_NAMES
        if model not in ALLOWED_MODEL_NAMES:
            return JSONResponse(
                {"error": f"unknown model name {model!r}"},
                status_code=400,
            )
    except Exception:
        return JSONResponse({"error": "registry unavailable"}, status_code=503)

    async with _promote_lock(model):
        candidate_file = registry.base / "candidates" / f"{model}-{version}.onnx"

        # Idempotency check runs inside the lock so a concurrent promoter
        # that already wrote the mirror is observable here.
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

        if not candidate_file.exists():
            return JSONResponse(
                {"error": f"candidate {model}-{version} not found"},
                status_code=404,
            )

        try:
            await registry.promote(model, version)
        except FileNotFoundError:
            # Should not happen — we hold `_promote_lock(model)` and
            # re-checked the candidate existed above. If it does, treat
            # as 404 rather than leaking the exception.
            return JSONResponse(
                {"error": f"candidate {model}-{version} not found"},
                status_code=404,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        registry.disable_shadow(model)

        # Drop cached candidate `InferenceSession`s for this model so RAM
        # isn't leaked across promotions. The just-promoted version is now
        # served from the production session; older candidate sessions are
        # never reused. `current_version=version` keeps the promoted-as-
        # candidate session if anything is mid-iteration, evicts everything
        # else.
        shadow_runner = getattr(ctx, "shadow_runner", None)
        if shadow_runner is not None:
            try:
                shadow_runner.evict_old_sessions(model, current_version=version)
            except Exception:
                logger.debug("shadow session eviction failed", exc_info=True)

        approver = _caller_identity(request)
        event = build_promotion_event(
            model_name=model,
            candidate_version=version,
            dataset_hash="",  # filled in by caller when known
            shadow_metrics={},  # manual promotion doesn't re-run the gate
            approver=approver,
        )
        await _write_lifecycle_event(ctx, event)
        try:
            from gateway.metrics.prometheus import model_promoted_total
            model_promoted_total.labels(model=model).inc()
        except Exception:
            pass

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
    try:
        from gateway.metrics.prometheus import candidate_rejected_total
        candidate_rejected_total.labels(model=model, reason=reason).inc()
    except Exception:
        pass

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
    try:
        from gateway.metrics.prometheus import model_promoted_total
        model_promoted_total.labels(model=model).inc()
    except Exception:
        pass

    return JSONResponse({
        "rolled_back": True,
        "model_name": model,
        "restored_from": target.name,
        "approver": approver,
    })


# ── Task 28: force retrain ──────────────────────────────────────────────


_retrain_tasks: dict[str, Any] = {}
_retrain_model_locks: dict[str, asyncio.Lock] = {}
# Cap on concurrently-tracked retrain jobs. A real operator triggers
# retrains at human pace; anything beyond this is either a bug loop or
# an abuse pattern and we'd rather 429 than hold refs to unbounded tasks.
_RETRAIN_MAX_INFLIGHT = 100


def _retrain_model_lock(model: str) -> asyncio.Lock:
    lock = _retrain_model_locks.get(model)
    if lock is None:
        lock = asyncio.Lock()
        _retrain_model_locks[model] = lock
    return lock


async def _run_retrain_serialized(worker, model: str) -> None:
    """Serialize retrains per model so concurrent /retrain/{m} calls
    don't race on candidate filename timestamps and don't thrash the
    trainer."""
    async with _retrain_model_lock(model):
        await worker.retrain_one(model)


async def force_retrain(request: Request) -> JSONResponse:
    """Kick an immediate training pass for one model.

    Returns 202 with a job_id so the dashboard can poll. The actual
    training runs in a detached `asyncio.Task`; completion shows up on
    `/v1/control/intelligence/candidates` when the worker writes the
    new candidate file.
    """
    from gateway.intelligence.registry import ALLOWED_MODEL_NAMES
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

    # Reap BEFORE the cap check so completed jobs free up slots.
    _reap_retrain_tasks()
    if len(_retrain_tasks) >= _RETRAIN_MAX_INFLIGHT:
        return JSONResponse(
            {
                "error": f"retrain queue full ({_RETRAIN_MAX_INFLIGHT} in flight)",
            },
            status_code=429,
        )

    job_id = str(uuid.uuid4())
    task = asyncio.create_task(
        _run_retrain_serialized(worker, model),
        name=f"retrain-{model}-{job_id}",
    )
    _retrain_tasks[job_id] = task

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


# ── Task 29: verdict log inspector ────────────────────────────────────────


async def list_verdicts(request: Request) -> JSONResponse:
    """Inspect the verdict log.

    Query params:
      `model` — required; one of the canonical model names.
      `divergence_only` — "true"/"1"/"yes" to restrict to rows whose
          harvesters back-wrote a `divergence_signal`.
      `limit` — max rows to return (default 100, clamped to [1, 1000]).

    Response:
      `top_divergence_types` — list of {signal, count} pairs, sorted
          by count desc. Only populated when `divergence_only=true`
          (otherwise the counts would be dominated by rows with
          no signal at all).
      `rows` — raw verdict rows, newest-first.
    """
    from gateway.intelligence.registry import ALLOWED_MODEL_NAMES

    model = (request.query_params.get("model") or "").strip()
    if model not in ALLOWED_MODEL_NAMES:
        return JSONResponse(
            {"error": f"model must be one of {sorted(ALLOWED_MODEL_NAMES)}"},
            status_code=400,
        )

    divergence_only = _truthy(request.query_params.get("divergence_only"))
    try:
        limit = max(1, min(1000, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100

    db = _require_db()
    if db is None:
        return _503("intelligence db not initialized")

    top_types, rows = _query_verdicts(db, model, divergence_only, limit)
    return JSONResponse({
        "model_name": model,
        "divergence_only": divergence_only,
        "limit": limit,
        "top_divergence_types": top_types,
        "rows": rows,
    })


def _truthy(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def _query_verdicts(
    db, model: str, divergence_only: bool, limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (top_divergence_types, rows) for the inspector response."""
    where_clauses = ["model_name = ?"]
    params: list[Any] = [model]
    if divergence_only:
        where_clauses.append("divergence_signal IS NOT NULL")
    where_sql = " AND ".join(where_clauses)

    conn = sqlite3.connect(db.path)
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r) for r in conn.execute(
                f"SELECT id, model_name, input_hash, prediction, confidence, "
                f"request_id, timestamp, divergence_signal, divergence_source "
                f"FROM onnx_verdicts WHERE {where_sql} "
                f"ORDER BY timestamp DESC, id DESC LIMIT ?",
                params + [limit],
            )
        ]
        # Top divergence types — only meaningful when divergence_only=True;
        # otherwise every type's count would be dominated by `NULL` rows.
        top_types: list[dict[str, Any]] = []
        if divergence_only:
            for signal, count in conn.execute(
                "SELECT divergence_signal, COUNT(*) FROM onnx_verdicts "
                "WHERE model_name = ? AND divergence_signal IS NOT NULL "
                "GROUP BY divergence_signal "
                "ORDER BY COUNT(*) DESC, divergence_signal ASC",
                (model,),
            ):
                top_types.append({"signal": signal, "count": int(count)})
    finally:
        conn.close()
    return top_types, rows


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
