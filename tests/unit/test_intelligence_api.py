"""Phase 25 Phase G API tests.

Exercises the `/v1/control/intelligence/*` handlers directly (not via
Starlette's TestClient) — each handler is an async function taking a
`Request`, and we fabricate a minimal context + request so we can
assert the JSON payload shape precisely.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.intelligence.api import (
    force_retrain,
    list_candidates,
    list_production_models,
    list_verdicts,
    model_history,
    promote_candidate,
    reject_candidate,
    rollback_model,
)
from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.registry import ModelRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Fixtures + helpers ──────────────────────────────────────────────────────


def _make_ctx(tmp_path: Path, *, with_registry: bool = True, with_db: bool = True):
    registry = None
    db = None
    if with_registry:
        registry = ModelRegistry(base_path=str(tmp_path / "models"))
        registry.ensure_structure()
    if with_db:
        db = IntelligenceDB(str(tmp_path / "intel.db"))
        db.init_schema()
    return SimpleNamespace(model_registry=registry, intelligence_db=db)


def _install_ctx(monkeypatch, ctx):
    from gateway.intelligence import api as intel_api
    monkeypatch.setattr(
        intel_api, "get_pipeline_context", lambda: ctx, raising=True,
    )


def _fake_request(
    *, path_params: dict | None = None,
    query: dict | None = None,
    state: SimpleNamespace | None = None,
    headers: dict | None = None,
):
    return SimpleNamespace(
        path_params=path_params or {},
        query_params=query or {},
        state=state or SimpleNamespace(),
        headers=headers or {},
    )


class _FakeWriter:
    def __init__(self) -> None:
        self.events = []

    async def write_event(self, event):
        self.events.append(event)


def _insert_event(
    db: IntelligenceDB,
    *,
    event_type: str,
    payload: dict,
    walacor_id: str | None = "wal-1",
    status: str = "written",
) -> None:
    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            "INSERT INTO lifecycle_events_mirror "
            "(event_type, payload_json, timestamp, walacor_record_id, "
            "write_status, error_reason, attempts, written_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_type,
                json.dumps(payload, sort_keys=True),
                datetime.now(timezone.utc).isoformat(),
                walacor_id,
                status,
                None,
                1,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── /models ─────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_list_production_models_503_without_registry(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path, with_registry=False)
    _install_ctx(monkeypatch, ctx)
    resp = await list_production_models(_fake_request())
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_list_production_models_empty(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await list_production_models(_fake_request())
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body == {"models": []}


@pytest.mark.anyio
async def test_list_production_models_surfaces_registry_entries(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    (ctx.model_registry.base / "production" / "intent.onnx").write_bytes(b"intent-bytes")
    (ctx.model_registry.base / "production" / "safety.onnx").write_bytes(b"safety-bytes")

    resp = await list_production_models(_fake_request())
    body = json.loads(resp.body)
    names = [m["model_name"] for m in body["models"]]
    assert names == sorted(names)
    assert "intent" in names and "safety" in names
    intent_entry = next(m for m in body["models"] if m["model_name"] == "intent")
    assert intent_entry["size_bytes"] == len(b"intent-bytes")
    assert intent_entry["generation"] == 0
    assert intent_entry["last_promotion"] is None


@pytest.mark.anyio
async def test_list_production_models_flags_loaded_status(monkeypatch, tmp_path):
    """Existing files report status='loaded' with no error."""
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    (ctx.model_registry.base / "production" / "intent.onnx").write_bytes(b"x")

    resp = await list_production_models(_fake_request())
    body = json.loads(resp.body)
    intent = next(m for m in body["models"] if m["model_name"] == "intent")
    assert intent["status"] == "loaded"
    assert intent["error"] is None


@pytest.mark.anyio
async def test_list_production_models_flags_missing_file(monkeypatch, tmp_path):
    """A registered model whose .onnx file went away returns status='missing'."""
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    # Force list_production_models to return a name without a backing file.
    monkeypatch.setattr(
        ctx.model_registry, "list_production_models",
        lambda: ["intent"], raising=True,
    )

    resp = await list_production_models(_fake_request())
    body = json.loads(resp.body)
    intent = next(m for m in body["models"] if m["model_name"] == "intent")
    assert intent["status"] == "missing"
    assert intent["size_bytes"] == 0
    assert "not found" in (intent["error"] or "").lower()


@pytest.mark.anyio
async def test_list_production_models_includes_last_rollback(monkeypatch, tmp_path):
    """A rolled-back model surfaces last_rollback alongside last_promotion."""
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    (ctx.model_registry.base / "production" / "intent.onnx").write_bytes(b"x")

    _insert_event(
        ctx.intelligence_db,
        event_type="model_rolled_back",
        payload={
            "model_name": "intent",
            "from_version": "v9",
            "to_archive": "intent-archived-2026-04-26.onnx",
            "reason": "regression delta=0.150",
            "delta": 0.15,
        },
    )

    resp = await list_production_models(_fake_request())
    body = json.loads(resp.body)
    intent = next(m for m in body["models"] if m["model_name"] == "intent")
    assert intent["last_rollback"] is not None
    assert intent["last_rollback"]["from_version"] == "v9"
    assert intent["last_rollback"]["to_archive"] == "intent-archived-2026-04-26.onnx"


@pytest.mark.anyio
async def test_list_production_models_includes_last_promotion(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    (ctx.model_registry.base / "production" / "intent.onnx").write_bytes(b"x")

    _insert_event(
        ctx.intelligence_db,
        event_type="model_promoted",
        payload={
            "model_name": "intent",
            "candidate_version": "v3",
            "approver": "auto",
            "dataset_hash": "d1",
            "shadow_metrics": {"sample_count": 500},
            "event_type": "model_promoted",
        },
    )

    resp = await list_production_models(_fake_request())
    body = json.loads(resp.body)
    intent = next(m for m in body["models"] if m["model_name"] == "intent")
    assert intent["last_promotion"]["candidate_version"] == "v3"
    assert intent["last_promotion"]["approver"] == "auto"


# ── /candidates ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_list_candidates_empty(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await list_candidates(_fake_request())
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"candidates": []}


@pytest.mark.anyio
async def test_list_candidates_with_shadow_marker_and_metrics(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "intent-v5.onnx").write_bytes(b"bytes")
    reg.enable_shadow("intent", "v5")
    _insert_event(
        ctx.intelligence_db,
        event_type="shadow_validation_complete",
        payload={
            "model_name": "intent",
            "candidate_version": "v5",
            "passed": True,
            "metrics": {"sample_count": 400, "candidate_accuracy": 0.93},
            "event_type": "shadow_validation_complete",
        },
    )

    resp = await list_candidates(_fake_request())
    body = json.loads(resp.body)
    assert len(body["candidates"]) == 1
    c = body["candidates"][0]
    assert c["model_name"] == "intent"
    assert c["version"] == "v5"
    assert c["active_shadow"] is True
    assert c["shadow_validation"]["passed"] is True
    assert c["shadow_validation"]["metrics"]["candidate_accuracy"] == 0.93


@pytest.mark.anyio
async def test_list_candidates_without_shadow_marker(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "safety-v1.onnx").write_bytes(b"bytes")

    resp = await list_candidates(_fake_request())
    body = json.loads(resp.body)
    assert len(body["candidates"]) == 1
    c = body["candidates"][0]
    assert c["active_shadow"] is False
    assert c["shadow_validation"]["completed"] is False


# ── /history/{model} ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_model_history_503_without_db(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path, with_db=False)
    _install_ctx(monkeypatch, ctx)
    resp = await model_history(_fake_request(path_params={"model": "intent"}))
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_model_history_returns_events_newest_first(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    _insert_event(
        ctx.intelligence_db,
        event_type="candidate_created",
        payload={"model_name": "intent", "candidate_version": "v1"},
    )
    _insert_event(
        ctx.intelligence_db,
        event_type="shadow_validation_complete",
        payload={"model_name": "intent", "candidate_version": "v1", "passed": True},
    )
    _insert_event(
        ctx.intelligence_db,
        event_type="candidate_created",
        payload={"model_name": "safety", "candidate_version": "v1"},  # different model
    )

    resp = await model_history(_fake_request(path_params={"model": "intent"}))
    body = json.loads(resp.body)
    assert body["model_name"] == "intent"
    assert len(body["events"]) == 2
    # Newest-first — shadow_complete was inserted after candidate_created.
    assert body["events"][0]["event_type"] == "shadow_validation_complete"
    # Safety event is NOT present.
    assert all(e["payload"]["model_name"] == "intent" for e in body["events"])


@pytest.mark.anyio
async def test_model_history_respects_limit(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    for i in range(10):
        _insert_event(
            ctx.intelligence_db,
            event_type="candidate_created",
            payload={"model_name": "intent", "candidate_version": f"v{i}"},
        )

    resp = await model_history(_fake_request(
        path_params={"model": "intent"},
        query={"limit": "3"},
    ))
    body = json.loads(resp.body)
    assert len(body["events"]) == 3


@pytest.mark.anyio
async def test_model_history_empty_for_unknown_model(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await model_history(_fake_request(path_params={"model": "ghost_model"}))
    body = json.loads(resp.body)
    assert body == {"model_name": "ghost_model", "events": []}


# ── /promote/{model}/{version} ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_promote_404_when_candidate_missing(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await promote_candidate(
        _fake_request(path_params={"model": "intent", "version": "v9"})
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_promote_happy_path_emits_event(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.lifecycle_event_writer = _FakeWriter()
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "intent-v2.onnx").write_bytes(b"v2")

    resp = await promote_candidate(
        _fake_request(
            path_params={"model": "intent", "version": "v2"},
            headers={"X-User-Id": "alice@walacor.com"},
        )
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["promoted"] is True
    assert body["approver"] == "alice@walacor.com"
    # Production now carries the candidate bytes.
    assert (reg.base / "production" / "intent.onnx").read_bytes() == b"v2"
    # Lifecycle event emitted.
    assert len(ctx.lifecycle_event_writer.events) == 1
    ev = ctx.lifecycle_event_writer.events[0]
    assert ev.event_type.value == "model_promoted"
    assert ev.payload["approver"] == "alice@walacor.com"


@pytest.mark.anyio
async def test_promote_409_when_version_already_promoted(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "intent-v2.onnx").write_bytes(b"v2")
    # Seed a prior model_promoted event for (intent, v2).
    _insert_event(
        ctx.intelligence_db,
        event_type="model_promoted",
        payload={
            "model_name": "intent", "candidate_version": "v2",
            "approver": "old", "event_type": "model_promoted",
        },
    )

    resp = await promote_candidate(
        _fake_request(path_params={"model": "intent", "version": "v2"})
    )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_promote_uses_anonymous_when_no_identity(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.lifecycle_event_writer = _FakeWriter()
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "intent-v1.onnx").write_bytes(b"x")

    resp = await promote_candidate(
        _fake_request(path_params={"model": "intent", "version": "v1"})
    )
    body = json.loads(resp.body)
    assert body["approver"] == "anonymous"


@pytest.mark.anyio
async def test_promote_uses_caller_identity_state_over_header(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.lifecycle_event_writer = _FakeWriter()
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "intent-v1.onnx").write_bytes(b"x")

    state = SimpleNamespace(caller_identity=SimpleNamespace(user_id="jwt-user"))
    resp = await promote_candidate(
        _fake_request(
            path_params={"model": "intent", "version": "v1"},
            state=state,
            headers={"X-User-Id": "header-user"},
        )
    )
    body = json.loads(resp.body)
    assert body["approver"] == "jwt-user"


@pytest.mark.anyio
async def test_promote_invalid_model_name_returns_400(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    # Non-canonical model name: rejected up-front (400) before any
    # filesystem lookup. Important because the handler acquires a
    # per-model lock — without validation an attacker could seed
    # `_promote_locks` with arbitrary strings.
    resp = await promote_candidate(
        _fake_request(path_params={"model": "../../etc/passwd", "version": "v1"})
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_promote_concurrent_same_version_yields_one_success_and_409(monkeypatch, tmp_path):
    """Regression for C2: two concurrent POST /promote for the same
    (model, version) must produce exactly one 200 and one 409 — never a
    404 leaked from the loser's FileNotFoundError.

    Uses the real LifecycleEventWriter so the mirror table is populated
    the way it is in production; that's what the idempotency check
    reads.
    """
    import asyncio as _aio
    from gateway.intelligence import api as intel_api
    from gateway.intelligence.walacor_writer import LifecycleEventWriter

    class _NoopWalacor:
        async def write_record(self, record, *, etid=None):
            return {"id": "wr-1"}

    ctx = _make_ctx(tmp_path)
    ctx.lifecycle_event_writer = LifecycleEventWriter(
        ctx.intelligence_db, _NoopWalacor(), etid=9000024,
    )
    _install_ctx(monkeypatch, ctx)
    # Use a fresh lock map so state from prior tests can't interfere.
    monkeypatch.setattr(intel_api, "_promote_locks", {}, raising=True)

    reg = ctx.model_registry
    (reg.base / "candidates" / "intent-v9.onnx").write_bytes(b"v9")

    req_a = _fake_request(
        path_params={"model": "intent", "version": "v9"},
        headers={"X-User-Id": "alice"},
    )
    req_b = _fake_request(
        path_params={"model": "intent", "version": "v9"},
        headers={"X-User-Id": "bob"},
    )

    results = await _aio.gather(
        promote_candidate(req_a),
        promote_candidate(req_b),
    )

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], (
        f"expected [200, 409] from concurrent same-version promote, "
        f"got {statuses}"
    )
    # Exactly one mirror row for this (model, version).
    conn = sqlite3.connect(ctx.intelligence_db.path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM lifecycle_events_mirror "
            "WHERE event_type = 'model_promoted'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected 1 mirror row, got {len(rows)}"
    # Production file carries the candidate bytes.
    assert (reg.base / "production" / "intent.onnx").read_bytes() == b"v9"


# ── /reject/{model}/{version} ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_reject_moves_candidate_to_archive_failed(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.lifecycle_event_writer = _FakeWriter()
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "safety-v3.onnx").write_bytes(b"bytes")

    resp = await reject_candidate(
        _fake_request(
            path_params={"model": "safety", "version": "v3"},
            query={"reason": "failed accuracy gate"},
        )
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["rejected"] is True
    assert body["reason"] == "failed accuracy gate"
    # File moved.
    assert not (reg.base / "candidates" / "safety-v3.onnx").exists()
    assert (reg.base / "archive" / "failed" / "safety-v3.onnx").exists()
    # Event emitted.
    ev = ctx.lifecycle_event_writer.events[0]
    assert ev.event_type.value == "model_rejected"
    assert ev.payload["stage"] == "manual"


@pytest.mark.anyio
async def test_reject_clears_shadow_marker_for_that_version(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.lifecycle_event_writer = _FakeWriter()
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "intent-v7.onnx").write_bytes(b"bytes")
    reg.enable_shadow("intent", "v7")

    await reject_candidate(
        _fake_request(path_params={"model": "intent", "version": "v7"})
    )
    assert reg.active_candidate("intent") is None


@pytest.mark.anyio
async def test_reject_404_for_missing_candidate(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await reject_candidate(
        _fake_request(path_params={"model": "intent", "version": "no_such"})
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_reject_default_reason_when_none_provided(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.lifecycle_event_writer = _FakeWriter()
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    (reg.base / "candidates" / "intent-v1.onnx").write_bytes(b"x")
    resp = await reject_candidate(
        _fake_request(path_params={"model": "intent", "version": "v1"})
    )
    body = json.loads(resp.body)
    assert body["reason"] == "manual_rejection"


# ── /rollback/{model} ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_rollback_restores_most_recent_archive(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.lifecycle_event_writer = _FakeWriter()
    _install_ctx(monkeypatch, ctx)
    reg = ctx.model_registry
    # Pre-populate archive with two older versions.
    (reg.base / "archive" / "intent-archived-20250101T000000.000000Z.onnx").write_bytes(b"old")
    (reg.base / "archive" / "intent-archived-20260101T000000.000000Z.onnx").write_bytes(b"newer")
    # Current production.
    (reg.base / "production" / "intent.onnx").write_bytes(b"current")

    resp = await rollback_model(
        _fake_request(
            path_params={"model": "intent"},
            headers={"X-User-Id": "ops@walacor.com"},
        )
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["rolled_back"] is True
    assert body["restored_from"] == "intent-archived-20260101T000000.000000Z.onnx"
    # Production now holds the rolled-back bytes.
    assert (reg.base / "production" / "intent.onnx").read_bytes() == b"newer"
    # Previous production archived — the count in archive grew by 1.
    archived = list((reg.base / "archive").glob("intent-*.onnx"))
    assert len(archived) == 2  # one consumed, one added (previous prod)
    # Event emitted as a real rollback so _last_rollback_per_model can find it.
    ev = ctx.lifecycle_event_writer.events[0]
    assert ev.event_type.value == "model_rolled_back"
    assert ev.payload["to_archive"] == "intent-archived-20260101T000000.000000Z.onnx"
    assert "manual rollback by ops@walacor.com" in ev.payload["reason"]


@pytest.mark.anyio
async def test_rollback_404_when_no_archive(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await rollback_model(_fake_request(path_params={"model": "intent"}))
    assert resp.status_code == 404


# ── /retrain/{model} ──────────────────────────────────────────────────────

class _FakeWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.completion = __import__("asyncio").Event()

    async def retrain_one(self, model_name):
        self.calls.append(model_name)
        self.completion.set()
        # CycleResult-shaped return for realism, though the API only
        # exposes the job_id.
        from gateway.intelligence.distillation.worker import CycleResult
        r = CycleResult()
        r.trained.append(model_name)
        return r


@pytest.mark.anyio
async def test_retrain_returns_202_and_kicks_worker(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    worker = _FakeWorker()
    ctx.distillation_worker = worker
    _install_ctx(monkeypatch, ctx)

    resp = await force_retrain(
        _fake_request(path_params={"model": "intent"})
    )
    assert resp.status_code == 202
    body = json.loads(resp.body)
    assert body["status"] == "accepted"
    assert body["model_name"] == "intent"
    assert body["job_id"]  # non-empty UUID

    # The kicked task runs on the same loop — give it one scheduler tick
    # so the fake worker records the call.
    import asyncio
    await asyncio.wait_for(worker.completion.wait(), timeout=1.0)
    assert worker.calls == ["intent"]


@pytest.mark.anyio
async def test_retrain_503_when_worker_not_wired(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.distillation_worker = None
    _install_ctx(monkeypatch, ctx)
    resp = await force_retrain(
        _fake_request(path_params={"model": "intent"})
    )
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_retrain_400_for_unknown_model(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.distillation_worker = _FakeWorker()
    _install_ctx(monkeypatch, ctx)
    resp = await force_retrain(
        _fake_request(path_params={"model": "not_a_real_model"})
    )
    assert resp.status_code == 400


# ── /verdicts ──────────────────────────────────────────────────────────────

def _insert_verdict(
    db: IntelligenceDB,
    *,
    model: str,
    prediction: str,
    divergence: str | None = None,
    input_hash: str = "h" * 64,
    request_id: str = "r1",
) -> None:
    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            "confidence, request_id, timestamp, divergence_signal, "
            "divergence_source, training_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, input_hash, "{}", prediction, 0.9, request_id,
                datetime.now(timezone.utc).isoformat(),
                divergence,
                "test" if divergence else None,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.anyio
async def test_verdicts_400_for_unknown_model(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await list_verdicts(_fake_request(query={"model": "banana"}))
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_verdicts_400_when_model_missing(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await list_verdicts(_fake_request(query={}))
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_verdicts_503_without_db(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path, with_db=False)
    _install_ctx(monkeypatch, ctx)
    resp = await list_verdicts(_fake_request(query={"model": "intent"}))
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_verdicts_returns_rows_newest_first(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    for i in range(3):
        _insert_verdict(
            ctx.intelligence_db, model="intent",
            prediction=f"p{i}", input_hash=f"h{i}",
        )
    resp = await list_verdicts(_fake_request(query={"model": "intent"}))
    body = json.loads(resp.body)
    # Most recently inserted row is first.
    assert body["rows"][0]["prediction"] == "p2"
    assert body["rows"][-1]["prediction"] == "p0"
    assert body["divergence_only"] is False
    # Without divergence_only, the top_divergence_types list is empty.
    assert body["top_divergence_types"] == []


@pytest.mark.anyio
async def test_verdicts_divergence_only_filters_and_aggregates(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    db = ctx.intelligence_db
    # 5 web_search divergences, 2 normal, 1 no-divergence.
    for i in range(5):
        _insert_verdict(
            db, model="intent", prediction="normal",
            divergence="web_search", input_hash=f"ws{i}",
        )
    for i in range(2):
        _insert_verdict(
            db, model="intent", prediction="web_search",
            divergence="normal", input_hash=f"n{i}",
        )
    _insert_verdict(db, model="intent", prediction="rag", input_hash="plain")

    resp = await list_verdicts(_fake_request(
        query={"model": "intent", "divergence_only": "true"},
    ))
    body = json.loads(resp.body)
    # Only divergent rows surface.
    assert len(body["rows"]) == 7
    assert all(r["divergence_signal"] is not None for r in body["rows"])
    # Top types sorted by count desc.
    top = body["top_divergence_types"]
    assert [t["signal"] for t in top] == ["web_search", "normal"]
    assert top[0]["count"] == 5
    assert top[1]["count"] == 2


@pytest.mark.anyio
async def test_verdicts_filter_by_model(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    db = ctx.intelligence_db
    _insert_verdict(db, model="intent", prediction="normal")
    _insert_verdict(db, model="safety", prediction="safe")
    resp = await list_verdicts(_fake_request(query={"model": "safety"}))
    body = json.loads(resp.body)
    assert len(body["rows"]) == 1
    assert body["rows"][0]["model_name"] == "safety"


@pytest.mark.anyio
async def test_verdicts_limit_clamped(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    db = ctx.intelligence_db
    for i in range(10):
        _insert_verdict(db, model="intent", prediction=f"p{i}", input_hash=f"h{i}")
    # Limit below 1 → clamped to 1.
    resp1 = await list_verdicts(_fake_request(query={"model": "intent", "limit": "0"}))
    assert len(json.loads(resp1.body)["rows"]) == 1
    # Massive limit → clamped to 1000 (no crash).
    resp2 = await list_verdicts(_fake_request(
        query={"model": "intent", "limit": "99999"},
    ))
    assert len(json.loads(resp2.body)["rows"]) == 10


@pytest.mark.anyio
async def test_retrain_tracks_job_and_reaps_completed(monkeypatch, tmp_path):
    # After the task completes, `_retrain_tasks` should drop its entry
    # on the next call so the dict doesn't grow forever.
    from gateway.intelligence import api as intel_api

    ctx = _make_ctx(tmp_path)
    ctx.distillation_worker = _FakeWorker()
    _install_ctx(monkeypatch, ctx)
    intel_api._retrain_tasks.clear()

    import asyncio
    resp1 = await force_retrain(
        _fake_request(path_params={"model": "intent"})
    )
    job_id1 = json.loads(resp1.body)["job_id"]
    # Wait for the first task to finish.
    await asyncio.wait_for(ctx.distillation_worker.completion.wait(), timeout=1.0)
    # Give the scheduler a tick to mark the task done.
    for _ in range(5):
        await asyncio.sleep(0)

    # Second call reaps the first completed job.
    ctx.distillation_worker.completion.clear()
    resp2 = await force_retrain(
        _fake_request(path_params={"model": "intent"})
    )
    assert job_id1 not in intel_api._retrain_tasks
    job_id2 = json.loads(resp2.body)["job_id"]
    assert job_id2 in intel_api._retrain_tasks
    await asyncio.wait_for(ctx.distillation_worker.completion.wait(), timeout=1.0)


@pytest.mark.anyio
async def test_retrain_serializes_concurrent_requests_for_same_model(monkeypatch, tmp_path):
    """Regression for I2: 10 concurrent POST /retrain/intent must not
    run 10 trainer invocations in parallel — they'd race on the
    candidate filename (ISO-second granularity collision) and thrash
    the trainer. Per-model asyncio.Lock serializes them."""
    import asyncio
    from gateway.intelligence import api as intel_api

    in_flight = 0
    max_in_flight = 0
    in_flight_lock = asyncio.Lock()

    class _SlowWorker:
        async def retrain_one(self, model_name):
            nonlocal in_flight, max_in_flight
            async with in_flight_lock:
                in_flight += 1
                if in_flight > max_in_flight:
                    max_in_flight = in_flight
            try:
                await asyncio.sleep(0.05)
            finally:
                async with in_flight_lock:
                    in_flight -= 1

    ctx = _make_ctx(tmp_path)
    ctx.distillation_worker = _SlowWorker()
    _install_ctx(monkeypatch, ctx)
    intel_api._retrain_tasks.clear()
    # Reset per-model locks so state from prior tests can't taint.
    monkeypatch.setattr(intel_api, "_retrain_model_locks", {}, raising=True)

    # Fire 10 concurrent retrain requests for the same model.
    responses = await asyncio.gather(*[
        force_retrain(_fake_request(path_params={"model": "intent"}))
        for _ in range(10)
    ])
    for r in responses:
        assert r.status_code == 202

    # Drain all queued work.
    tasks = list(intel_api._retrain_tasks.values())
    await asyncio.gather(*tasks, return_exceptions=True)

    assert max_in_flight == 1, (
        f"expected serialized retrains (max=1), got max_in_flight={max_in_flight}"
    )


@pytest.mark.anyio
async def test_retrain_returns_429_when_queue_full(monkeypatch, tmp_path):
    """Regression for I2: cap on tracked retrain jobs prevents
    unbounded dict growth under abuse / bug-loop."""
    import asyncio
    from gateway.intelligence import api as intel_api

    never_finishes = asyncio.Event()

    class _StuckWorker:
        async def retrain_one(self, model_name):
            await never_finishes.wait()

    ctx = _make_ctx(tmp_path)
    ctx.distillation_worker = _StuckWorker()
    _install_ctx(monkeypatch, ctx)
    intel_api._retrain_tasks.clear()
    monkeypatch.setattr(intel_api, "_retrain_model_locks", {}, raising=True)
    # Tighten cap so the test doesn't spawn 100 real tasks.
    monkeypatch.setattr(intel_api, "_RETRAIN_MAX_INFLIGHT", 3, raising=True)

    r1 = await force_retrain(_fake_request(path_params={"model": "intent"}))
    r2 = await force_retrain(_fake_request(path_params={"model": "intent"}))
    r3 = await force_retrain(_fake_request(path_params={"model": "intent"}))
    r4 = await force_retrain(_fake_request(path_params={"model": "intent"}))

    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r3.status_code == 202
    assert r4.status_code == 429, f"expected 429, got {r4.status_code}"

    # Unblock the stuck workers so the test teardown is clean.
    never_finishes.set()
    tasks = list(intel_api._retrain_tasks.values())
    await asyncio.gather(*tasks, return_exceptions=True)
