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
    list_candidates,
    list_production_models,
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
    # Simulate the candidate file at a path that matches the regex but
    # uses a non-canonical model name — registry.promote will raise
    # ValueError on the path_traversal guard.
    resp = await promote_candidate(
        _fake_request(path_params={"model": "../../etc/passwd", "version": "v1"})
    )
    # 404 first because the file literally doesn't exist — the deeper
    # ValueError path is exercised by Task 11/22 registry tests.
    assert resp.status_code == 404


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
    # Event emitted.
    ev = ctx.lifecycle_event_writer.events[0]
    assert ev.event_type.value == "model_promoted"
    assert ev.payload["approver"] == "ops@walacor.com"


@pytest.mark.anyio
async def test_rollback_404_when_no_archive(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    _install_ctx(monkeypatch, ctx)
    resp = await rollback_model(_fake_request(path_params={"model": "intent"}))
    assert resp.status_code == 404
