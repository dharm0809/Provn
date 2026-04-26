"""End-to-end integration test for the Phase 25 self-learning loop.

Exercises the full sequence:

  1. Train + promote v1 (good model)
  2. Train + promote v2 (regressed)
  3. Inject ~500 production verdicts where v2 underperforms
  4. DriftMonitor.check_once() — fires on intent
  5. PostPromotionValidator.check_once() — rolls back to v1
  6. Verify lifecycle log = [promoted v1, promoted v2, rolled_back v2]
  7. Verify Prometheus counter incremented

The ONNX models are real sklearn-exported binaries (≤2 features, 2
classes — fast to train, small file). `registry.promote()` and
`rollback()` are tested by their actual file moves; drift + validator
read DB-only and never load the model so the contents don't matter
for those, but generating real .onnx files keeps the test honest
about the registry's mechanics.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Skip the entire module when the optional training stack is missing —
# integration deps live behind the `intelligence` extra in pyproject.
sklearn = pytest.importorskip("sklearn")
skl2onnx = pytest.importorskip("skl2onnx")
np = pytest.importorskip("numpy")


# ── helpers ────────────────────────────────────────────────────────────


def _train_tiny_onnx(out_path: Path, *, accuracy_target: float, seed: int) -> None:
    """Train a 2-feature, 2-class logistic-regression and export to ONNX.

    `accuracy_target` is approximate — controls how separable the
    synthetic data is. Output bytes don't matter for this test; the
    file just needs to be a valid ONNX so registry.promote can move
    it without complaint, and so any future test extension that loads
    the model would succeed.
    """
    from sklearn.linear_model import LogisticRegression
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType

    rng = np.random.default_rng(seed)
    n = 400
    # Two clusters separated by `gap`. Bigger gap → easier model.
    gap = max(0.4, accuracy_target * 4.0)
    X0 = rng.normal(loc=[-gap, -gap], scale=1.0, size=(n // 2, 2))
    X1 = rng.normal(loc=[gap, gap], scale=1.0, size=(n // 2, 2))
    X = np.vstack([X0, X1]).astype(np.float32)
    y = np.concatenate([np.zeros(n // 2, dtype=int), np.ones(n // 2, dtype=int)])
    clf = LogisticRegression(max_iter=200).fit(X, y)
    onnx_model = convert_sklearn(
        clf, initial_types=[("X", FloatTensorType([None, 2]))],
        target_opset=15,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(onnx_model.SerializeToString())


def _seed_verdict(db, *, model: str, prediction: str, signal: str | None,
                  ts: datetime, version: str | None) -> None:
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO onnx_verdicts "
            "(model_name, input_hash, input_features_json, prediction, "
            " confidence, request_id, timestamp, divergence_signal, "
            " divergence_source, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (model, "h", "{}", prediction, 0.9, None, ts.isoformat(),
             signal, "test", version),
        )


def _insert_promotion_event(db, *, model: str, version: str, when: datetime) -> None:
    """Stand-in for what LifecycleEventWriter.write_event would do for a real promotion."""
    payload = json.dumps({"model_name": model, "candidate_version": version})
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            "INSERT INTO lifecycle_events_mirror "
            "(event_type, payload_json, timestamp, walacor_record_id, "
            " write_status, error_reason, attempts, written_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("model_promoted", payload, when.isoformat(), None,
             "written", None, 1, when.isoformat()),
        )


def _read_lifecycle_log(db) -> list[tuple[str, str | None]]:
    """Return [(event_type, candidate_version | from_version), ...] in time order."""
    with sqlite3.connect(db.path) as conn:
        rows = conn.execute(
            "SELECT event_type, payload_json, written_at "
            "FROM lifecycle_events_mirror "
            "ORDER BY written_at ASC"
        ).fetchall()
    out: list[tuple[str, str | None]] = []
    for event_type, payload_json, _ts in rows:
        try:
            payload = json.loads(payload_json)
        except (ValueError, TypeError):
            payload = {}
        if event_type == "model_promoted":
            out.append((event_type, payload.get("candidate_version")))
        elif event_type == "model_rolled_back":
            out.append((event_type, payload.get("from_version")))
        else:
            out.append((event_type, None))
    return out


# ── the test ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_full_intelligence_loop_with_regression_triggers_rollback(tmp_path):
    from gateway.intelligence.db import IntelligenceDB
    from gateway.intelligence.drift_monitor import DriftMonitor
    from gateway.intelligence.post_promotion_validator import PostPromotionValidator
    from gateway.intelligence.registry import ModelRegistry
    from gateway.metrics.prometheus import model_rollback_total

    db = IntelligenceDB(str(tmp_path / "intel.db"))
    db.init_schema()
    reg = ModelRegistry(base_path=str(tmp_path / "models"))
    reg.ensure_structure()

    # ── Stage 1 + 2: train v1 + v2, promote both via registry.promote()
    # The trainer outputs a candidate file at candidates/<model>-<ver>.onnx,
    # which promote() renames to production/<model>.onnx.
    cand_v1 = reg.base / "candidates" / "intent-v1.onnx"
    cand_v2 = reg.base / "candidates" / "intent-v2.onnx"
    _train_tiny_onnx(cand_v1, accuracy_target=0.95, seed=1)
    # v2 trained on noisier data so the file IS different (mostly cosmetic
    # for this test — the validator reads SQLite, not the model).
    _train_tiny_onnx(cand_v2, accuracy_target=0.40, seed=2)

    now = datetime.now(timezone.utc)
    t_v1_promoted = now - timedelta(hours=2)
    t_v2_promoted = now - timedelta(minutes=45)

    await reg.promote("intent", "v1")
    _insert_promotion_event(db, model="intent", version="v1", when=t_v1_promoted)
    # The first promote moved cand_v1 onto production. To promote v2,
    # we need a candidate file again — re-train (the registry consumed
    # the v1 file via os.rename).
    _train_tiny_onnx(cand_v2, accuracy_target=0.40, seed=2)
    await reg.promote("intent", "v2")
    _insert_promotion_event(db, model="intent", version="v2", when=t_v2_promoted)

    # ── Stage 3: inject 500 production verdicts. v1 baseline at 95%
    # accuracy in its window; v2 at 75% in its window — clear regression.
    v1_window_ts = t_v1_promoted + timedelta(minutes=10)
    for i in range(500):
        sig = "A" if i < 475 else "B"  # 475/500 = 95%
        _seed_verdict(db, model="intent", prediction="A", signal=sig,
                      ts=v1_window_ts, version="v1")
    v2_window_ts = t_v2_promoted + timedelta(minutes=15)
    for i in range(500):
        sig = "A" if i < 375 else "B"  # 375/500 = 75%
        _seed_verdict(db, model="intent", prediction="A", signal=sig,
                      ts=v2_window_ts, version="v2")

    # ── Stage 4: DriftMonitor must detect the regression on intent.
    # Use a time-window that brackets v2's recent traffic against v1's
    # baseline. drift_window_hours=1 with baseline_window_count=2 means
    # recent = last 1h, baseline = the 2h before that.
    drift = DriftMonitor(
        db,
        window_hours=1,
        baseline_window_count=2,
        threshold=0.05,
        min_samples=100,
        min_coverage=0.30,
        models=["intent"],
        clock=lambda: now,
    )
    drift_signals = await drift.check_once()
    assert len(drift_signals) == 1, f"expected 1 drift signal, got {drift_signals}"
    sig = drift_signals[0]
    assert sig.model == "intent"
    assert sig.delta >= 0.10  # 95% -> 75% is a 20-pt drop

    # ── Stage 5: PostPromotionValidator rolls back v2 → archived v1.
    # Capture the rollback counter BEFORE so we can assert delta=1.
    counter_before = model_rollback_total.labels(
        model="intent", reason="regression",
    )._value.get()

    # Validator needs a lifecycle writer so the rolled_back event
    # lands in lifecycle_events_mirror. The real LifecycleEventWriter
    # always mirrors locally even if the Walacor leg fails — pass a
    # stub Walacor client that always raises so the test doesn't
    # depend on a running Walacor backend, and confirm the mirror row
    # still gets written (status="failed" — irrelevant for our
    # assertions).
    from gateway.intelligence.walacor_writer import LifecycleEventWriter

    class _FailingWalacor:
        async def write_record(self, record, *, etid=None):
            raise RuntimeError("test stub: walacor unavailable")

    writer = LifecycleEventWriter(
        db, _FailingWalacor(), etid=999, max_attempts=1,
        sleep=lambda _s: None,
    )
    validator = PostPromotionValidator(
        db, reg,
        threshold=0.05,
        window_h=24,
        min_samples=100,
        min_coverage=0.30,
        clock=lambda: now,
        lifecycle_writer=writer,
    )
    results = await validator.check_once()
    assert len(results) == 1, f"expected one validator action, got {results}"
    r = results[0]
    assert r["action"] == "rolled_back", r
    assert r["from_version"] == "v2"
    assert r["to_archive"].startswith("intent-archived-")

    # ── Stage 6: lifecycle log shows promote v1 → promote v2 → rolled_back v2
    log = _read_lifecycle_log(db)
    assert log == [
        ("model_promoted", "v1"),
        ("model_promoted", "v2"),
        ("model_rolled_back", "v2"),
    ], log

    # Production file size now matches what the archived v1 had on disk —
    # registry.rollback moved the archive back into production. (We don't
    # compare bytes because promote() moved v1's bytes to archive, then
    # rollback moved them back, so production should equal v1's training
    # output. We assert the file exists and is non-empty.)
    prod_path = reg.production_path("intent")
    assert prod_path.exists()
    assert prod_path.stat().st_size > 0

    # ── Stage 7: Prometheus counter incremented exactly once.
    counter_after = model_rollback_total.labels(
        model="intent", reason="regression",
    )._value.get()
    assert counter_after == counter_before + 1
