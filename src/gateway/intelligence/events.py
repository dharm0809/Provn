"""Lifecycle events for the Phase 25 ONNX self-learning loop.

Records model-registry actions to the Walacor audit chain under a dedicated ETId
(configurable via `walacor_lifecycle_events_etid`, default 9000024). `LifecycleEvent`
is the in-memory representation; `to_record()` produces the payload the Walacor
client submits — the ETId itself travels in the HTTP header, not the payload.

Full write-with-retry plumbing lives in Task 21's walacor_writer.py. This module
only defines types + factory builders.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

# Finite set of rejection stages. Typed so callers catch typos at type-check time
# rather than corrupting the audit stream with misspelled stage labels.
RejectionStage = Literal["load", "sanity", "shadow", "manual"]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventType(str, Enum):
    TRAINING_DATASET_FINGERPRINT = "training_dataset_fingerprint"
    CANDIDATE_CREATED = "candidate_created"
    SHADOW_VALIDATION_COMPLETE = "shadow_validation_complete"
    MODEL_PROMOTED = "model_promoted"
    MODEL_REJECTED = "model_rejected"


@dataclass
class LifecycleEvent:
    event_type: EventType
    payload: dict[str, Any]
    timestamp: str = field(default_factory=_utcnow_iso)

    def to_record(self) -> dict[str, Any]:
        # Payload is spread FIRST so top-level `event_type` and `timestamp`
        # always win — if a caller accidentally includes those keys in payload,
        # the canonical values are preserved rather than silently overridden.
        # ETId travels in the HTTP header, NOT in this payload.
        return {
            **self.payload,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
        }


def _dataset_hash(row_ids: list[int], content_hash: str) -> str:
    # Duplicates in `row_ids` are preserved (via `sorted`, not `set()`).
    # Fingerprints reflect the exact training multiset — two identical rows
    # produce a different hash than one row, because they're different datasets.
    canonical = json.dumps(
        {"row_ids": sorted(row_ids), "content_hash": content_hash}, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_training_fingerprint(
    *, model_name: str, row_ids: list[int], content_hash: str
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.TRAINING_DATASET_FINGERPRINT,
        payload={
            "model_name": model_name,
            "row_ids": sorted(row_ids),
            "content_hash": content_hash,
            "dataset_hash": _dataset_hash(row_ids, content_hash),
        },
    )


def build_candidate_created(
    *, model_name: str, candidate_version: str, dataset_hash: str,
    training_sample_count: int,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.CANDIDATE_CREATED,
        payload={
            "model_name": model_name,
            "candidate_version": candidate_version,
            "dataset_hash": dataset_hash,
            "training_sample_count": training_sample_count,
        },
    )


def build_shadow_validation_complete(
    *, model_name: str, candidate_version: str,
    metrics: dict[str, Any], passed: bool,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.SHADOW_VALIDATION_COMPLETE,
        payload={
            "model_name": model_name,
            "candidate_version": candidate_version,
            "metrics": metrics,
            "passed": passed,
        },
    )


def build_promotion_event(
    *, model_name: str, candidate_version: str, dataset_hash: str,
    shadow_metrics: dict[str, Any], approver: str,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.MODEL_PROMOTED,
        payload={
            "model_name": model_name,
            "candidate_version": candidate_version,
            "dataset_hash": dataset_hash,
            "shadow_metrics": shadow_metrics,
            "approver": approver,
        },
    )


def build_model_rejected(
    *, model_name: str, candidate_version: str, reason: str, stage: RejectionStage,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=EventType.MODEL_REJECTED,
        payload={
            "model_name": model_name,
            "candidate_version": candidate_version,
            "reason": reason,
            "stage": stage,
        },
    )
