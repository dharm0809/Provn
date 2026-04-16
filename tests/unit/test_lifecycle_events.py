from __future__ import annotations

from gateway.intelligence.events import (
    LifecycleEvent,
    EventType,
    build_training_fingerprint,
    build_promotion_event,
    build_candidate_created,
    build_shadow_validation_complete,
    build_model_rejected,
)


def test_event_type_enum():
    assert EventType.TRAINING_DATASET_FINGERPRINT.value == "training_dataset_fingerprint"
    assert EventType.CANDIDATE_CREATED.value == "candidate_created"
    assert EventType.SHADOW_VALIDATION_COMPLETE.value == "shadow_validation_complete"
    assert EventType.MODEL_PROMOTED.value == "model_promoted"
    assert EventType.MODEL_REJECTED.value == "model_rejected"


def test_to_record_does_not_include_etid():
    # ETId lives in the HTTP header, not the payload — to_record must NOT include it.
    ev = build_promotion_event(
        model_name="intent", candidate_version="v3", dataset_hash="deadbeef",
        shadow_metrics={"accuracy": 0.94}, approver="alice@example.com",
    )
    rec = ev.to_record()
    assert "etid" not in rec
    assert rec["event_type"] == "model_promoted"
    assert "timestamp" in rec


def test_build_training_fingerprint_deterministic():
    row_ids = [3, 1, 2]
    ev = build_training_fingerprint(model_name="intent", row_ids=row_ids, content_hash="abc")
    assert ev.event_type == EventType.TRAINING_DATASET_FINGERPRINT
    assert ev.payload["row_ids"] == [1, 2, 3]  # sorted
    assert ev.payload["content_hash"] == "abc"
    assert ev.payload["model_name"] == "intent"
    assert len(ev.payload["dataset_hash"]) == 64  # sha256 hex


def test_training_fingerprint_is_order_independent():
    a = build_training_fingerprint(model_name="intent", row_ids=[3, 1, 2], content_hash="x")
    b = build_training_fingerprint(model_name="intent", row_ids=[2, 3, 1], content_hash="x")
    assert a.payload["dataset_hash"] == b.payload["dataset_hash"]


def test_build_promotion_event():
    ev = build_promotion_event(
        model_name="intent", candidate_version="v3", dataset_hash="deadbeef",
        shadow_metrics={"accuracy": 0.94}, approver="alice@example.com",
    )
    assert ev.event_type == EventType.MODEL_PROMOTED
    assert ev.payload["approver"] == "alice@example.com"
    assert ev.payload["shadow_metrics"]["accuracy"] == 0.94


def test_build_candidate_created():
    ev = build_candidate_created(
        model_name="safety", candidate_version="v7", dataset_hash="abc",
        training_sample_count=842,
    )
    assert ev.event_type == EventType.CANDIDATE_CREATED
    assert ev.payload["training_sample_count"] == 842


def test_build_shadow_validation_complete():
    ev = build_shadow_validation_complete(
        model_name="schema_mapper", candidate_version="v4",
        metrics={"accuracy_delta": 0.03, "disagreement": 0.12, "samples": 1000},
        passed=True,
    )
    assert ev.event_type == EventType.SHADOW_VALIDATION_COMPLETE
    assert ev.payload["passed"] is True
    assert ev.payload["metrics"]["samples"] == 1000


def test_build_model_rejected():
    ev = build_model_rejected(
        model_name="intent", candidate_version="v5",
        reason="accuracy delta below threshold", stage="shadow",
    )
    assert ev.event_type == EventType.MODEL_REJECTED
    assert ev.payload["reason"] == "accuracy delta below threshold"
    assert ev.payload["stage"] == "shadow"


def test_to_record_timestamp_is_iso8601():
    # All other Walacor/WAL records in this codebase use ISO-8601 UTC strings.
    # Lifecycle events must match so dashboard range-queries and cross-ETId
    # joins work consistently.
    ev = build_candidate_created(
        model_name="intent", candidate_version="v1", dataset_hash="h",
        training_sample_count=1,
    )
    assert isinstance(ev.timestamp, str)
    # Format: "2026-04-16T10:23:45.123456+00:00" — parseable round-trip.
    from datetime import datetime
    parsed = datetime.fromisoformat(ev.timestamp)
    assert parsed.tzinfo is not None  # must carry explicit UTC offset


def test_to_record_top_level_fields_override_payload():
    # If a caller accidentally includes "event_type" or "timestamp" keys in
    # payload, `to_record()` must emit the canonical top-level values — the
    # audit stream cannot be corrupted by payload collisions.
    ev = LifecycleEvent(
        event_type=EventType.MODEL_PROMOTED,
        payload={
            "event_type": "ATTACKER_FORGED",
            "timestamp": "1970-01-01T00:00:00+00:00",
            "real_data": "kept",
        },
        timestamp="2026-04-16T12:34:56+00:00",
    )
    rec = ev.to_record()
    assert rec["event_type"] == "model_promoted"  # canonical value wins
    assert rec["timestamp"] == "2026-04-16T12:34:56+00:00"  # canonical timestamp wins
    assert rec["real_data"] == "kept"  # unrelated payload fields pass through
