"""Auto-rollback when a freshly-promoted candidate regresses on live traffic."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.post_promotion_validator import PostPromotionValidator
from gateway.intelligence.registry import ModelRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed_verdict(db, *, model, prediction, divergence_signal, ts):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            " confidence, request_id, timestamp, divergence_signal, divergence_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model, "h", "{}", prediction, 0.9, None, ts.isoformat(),
             divergence_signal, "test"),
        )


def _seed_promotion(db, *, model, version, promoted_at):
    payload = json.dumps({"model_name": model, "candidate_version": version})
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            "INSERT INTO lifecycle_events_mirror "
            "(event_type, payload_json, timestamp, walacor_record_id, "
            " write_status, error_reason, attempts, written_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("model_promoted", payload, promoted_at.isoformat(), None,
             "written", None, 1, promoted_at.isoformat()),
        )


def _setup_registry(tmp_path) -> ModelRegistry:
    reg = ModelRegistry(base_path=str(tmp_path / "models"))
    reg.ensure_structure()
    return reg


def _seed_archive(reg: ModelRegistry, model: str, *, suffix: str = "v1"):
    """Drop a fake archived ONNX so rollback() has a target to restore."""
    path = reg.base / "archive" / f"{model}-archived-{suffix}.onnx"
    path.write_bytes(b"old-but-trustworthy")
    return path.name


@pytest.fixture
def db(tmp_path):
    d = IntelligenceDB(str(tmp_path / "intel.db"))
    d.init_schema()
    return d


@pytest.mark.anyio
async def test_validator_rolls_back_regressed_candidate(db, tmp_path):
    reg = _setup_registry(tmp_path)
    archive_name = _seed_archive(reg, "intent")
    # Pretend "intent" has a current production file.
    (reg.base / "production" / "intent.onnx").write_bytes(b"current")

    now = datetime.now(timezone.utc)
    promoted_at = now - timedelta(hours=1)
    _seed_promotion(db, model="intent", version="v9", promoted_at=promoted_at)

    # Pre-promotion baseline: 95% over 200 samples
    pre_ts = promoted_at - timedelta(minutes=30)
    for i in range(200):
        sig = "A" if i < 190 else "B"
        _seed_verdict(db, model="intent", prediction="A", divergence_signal=sig, ts=pre_ts)
    # Post-promotion: 80% over 250 samples (15-pt regression)
    post_ts = promoted_at + timedelta(minutes=15)
    for i in range(250):
        sig = "A" if i < 200 else "B"
        _seed_verdict(db, model="intent", prediction="A", divergence_signal=sig, ts=post_ts)

    validator = PostPromotionValidator(
        db, reg, threshold=0.05, min_samples=50, min_coverage=0.30,
        clock=lambda: now,
    )
    results = await validator.check_once()
    assert len(results) == 1
    r = results[0]
    assert r["model"] == "intent"
    assert r["action"] == "rolled_back"
    assert r["from_version"] == "v9"
    assert r["to_archive"] == archive_name
    # Production file should now hold the archive's bytes.
    assert (reg.base / "production" / "intent.onnx").read_bytes() == b"old-but-trustworthy"


@pytest.mark.anyio
async def test_validator_no_action_when_within_threshold(db, tmp_path):
    reg = _setup_registry(tmp_path)
    _seed_archive(reg, "intent")
    (reg.base / "production" / "intent.onnx").write_bytes(b"current")

    now = datetime.now(timezone.utc)
    promoted_at = now - timedelta(hours=1)
    _seed_promotion(db, model="intent", version="v9", promoted_at=promoted_at)

    pre_ts = promoted_at - timedelta(minutes=30)
    post_ts = promoted_at + timedelta(minutes=15)
    for ts in (pre_ts, post_ts):
        for i in range(200):
            sig = "A" if i < 190 else "B"  # 95% both windows
            _seed_verdict(db, model="intent", prediction="A", divergence_signal=sig, ts=ts)

    validator = PostPromotionValidator(
        db, reg, threshold=0.05, min_samples=50, min_coverage=0.30,
        clock=lambda: now,
    )
    results = await validator.check_once()
    assert results == []


@pytest.mark.anyio
async def test_validator_respects_settle_window(db, tmp_path):
    reg = _setup_registry(tmp_path)
    _seed_archive(reg, "intent")
    (reg.base / "production" / "intent.onnx").write_bytes(b"current")

    now = datetime.now(timezone.utc)
    # Promoted 5 minutes ago — settle window default 15min
    promoted_at = now - timedelta(minutes=5)
    _seed_promotion(db, model="intent", version="v9", promoted_at=promoted_at)

    validator = PostPromotionValidator(db, reg, settle_minutes=15, clock=lambda: now)
    results = await validator.check_once()
    assert results == []


@pytest.mark.anyio
async def test_validator_cooldown_blocks_second_rollback(db, tmp_path):
    reg = _setup_registry(tmp_path)
    _seed_archive(reg, "intent", suffix="a")
    _seed_archive(reg, "intent", suffix="b")  # second archive for second rollback if attempted
    (reg.base / "production" / "intent.onnx").write_bytes(b"current")

    now = datetime.now(timezone.utc)
    promoted_at = now - timedelta(hours=1)
    _seed_promotion(db, model="intent", version="v9", promoted_at=promoted_at)

    pre_ts = promoted_at - timedelta(minutes=30)
    post_ts = promoted_at + timedelta(minutes=15)
    for i in range(200):
        _seed_verdict(db, model="intent", prediction="A",
                      divergence_signal="A" if i < 195 else "B", ts=pre_ts)
    for i in range(200):
        _seed_verdict(db, model="intent", prediction="A",
                      divergence_signal="A" if i < 150 else "B", ts=post_ts)

    validator = PostPromotionValidator(
        db, reg, threshold=0.05, min_samples=50, min_coverage=0.30,
        cooldown_h=12, clock=lambda: now,
    )
    first = await validator.check_once()
    assert len(first) == 1 and first[0]["action"] == "rolled_back"

    # Re-run immediately — must be cooldown-suppressed.
    second = await validator.check_once()
    assert second == []


@pytest.mark.anyio
async def test_validator_skips_when_no_archive_present(db, tmp_path):
    """Regression detected but archive dir empty — surfaces rollback_skipped."""
    reg = _setup_registry(tmp_path)
    # No archive seeded.
    (reg.base / "production" / "intent.onnx").write_bytes(b"current")

    now = datetime.now(timezone.utc)
    promoted_at = now - timedelta(hours=1)
    _seed_promotion(db, model="intent", version="v9", promoted_at=promoted_at)

    pre_ts = promoted_at - timedelta(minutes=30)
    post_ts = promoted_at + timedelta(minutes=15)
    for i in range(200):
        _seed_verdict(db, model="intent", prediction="A",
                      divergence_signal="A" if i < 195 else "B", ts=pre_ts)
    for i in range(200):
        _seed_verdict(db, model="intent", prediction="A",
                      divergence_signal="A" if i < 150 else "B", ts=post_ts)

    validator = PostPromotionValidator(
        db, reg, threshold=0.05, min_samples=50, min_coverage=0.30,
        clock=lambda: now,
    )
    results = await validator.check_once()
    assert len(results) == 1
    assert results[0]["action"] == "rollback_skipped"
    assert results[0]["reason"] == "no_archive"
    # Production unchanged
    assert (reg.base / "production" / "intent.onnx").read_bytes() == b"current"
