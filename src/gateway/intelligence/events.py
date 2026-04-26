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
from typing import Any, Literal, TypedDict

# Finite set of rejection stages. Typed so callers catch typos at type-check time
# rather than corrupting the audit stream with misspelled stage labels.
RejectionStage = Literal["load", "sanity", "shadow", "manual"]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _TrainingFingerprintPayload(TypedDict):
    model_name: str
    row_ids: list[int]
    content_hash: str
    dataset_hash: str


class _CandidateCreatedPayload(TypedDict):
    model_name: str
    candidate_version: str
    dataset_hash: str
    training_sample_count: int


class _ShadowValidationPayload(TypedDict):
    model_name: str
    candidate_version: str
    metrics: dict[str, Any]
    passed: bool


class _PromotionPayload(TypedDict):
    model_name: str
    candidate_version: str
    dataset_hash: str
    shadow_metrics: dict[str, Any]
    approver: str


class _RejectionPayload(TypedDict):
    model_name: str
    candidate_version: str
    reason: str
    stage: RejectionStage


class EventType(str, Enum):
    TRAINING_DATASET_FINGERPRINT = "training_dataset_fingerprint"
    CANDIDATE_CREATED = "candidate_created"
    SHADOW_VALIDATION_COMPLETE = "shadow_validation_complete"
    MODEL_PROMOTED = "model_promoted"
    MODEL_REJECTED = "model_rejected"
    # Phase 25 hardening — auto-rollback fires this when the
    # post-promotion validator restores an archived version.
    MODEL_ROLLED_BACK = "model_rolled_back"


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
    payload: _TrainingFingerprintPayload = {
        "model_name": model_name,
        "row_ids": sorted(row_ids),
        "content_hash": content_hash,
        "dataset_hash": _dataset_hash(row_ids, content_hash),
    }
    return LifecycleEvent(event_type=EventType.TRAINING_DATASET_FINGERPRINT, payload=payload)


def build_candidate_created(
    *, model_name: str, candidate_version: str, dataset_hash: str,
    training_sample_count: int,
) -> LifecycleEvent:
    payload: _CandidateCreatedPayload = {
        "model_name": model_name,
        "candidate_version": candidate_version,
        "dataset_hash": dataset_hash,
        "training_sample_count": training_sample_count,
    }
    return LifecycleEvent(event_type=EventType.CANDIDATE_CREATED, payload=payload)


def build_shadow_validation_complete(
    *, model_name: str, candidate_version: str,
    metrics: dict[str, Any], passed: bool,
) -> LifecycleEvent:
    payload: _ShadowValidationPayload = {
        "model_name": model_name,
        "candidate_version": candidate_version,
        "metrics": metrics,
        "passed": passed,
    }
    return LifecycleEvent(event_type=EventType.SHADOW_VALIDATION_COMPLETE, payload=payload)


def build_promotion_event(
    *, model_name: str, candidate_version: str, dataset_hash: str,
    shadow_metrics: dict[str, Any], approver: str,
) -> LifecycleEvent:
    payload: _PromotionPayload = {
        "model_name": model_name,
        "candidate_version": candidate_version,
        "dataset_hash": dataset_hash,
        "shadow_metrics": shadow_metrics,
        "approver": approver,
    }
    return LifecycleEvent(event_type=EventType.MODEL_PROMOTED, payload=payload)


def build_rollback_event(
    *, model_name: str, from_version: str | None, to_archive: str, reason: str,
    delta: float | None = None, sample_count: int | None = None,
) -> LifecycleEvent:
    """Build the lifecycle event for an auto-rollback.

    `from_version` is the version that was just rolled back FROM (the
    candidate that regressed). `to_archive` is the archive filename
    that was restored.
    """
    payload: dict[str, Any] = {
        "model_name": model_name,
        "from_version": from_version,
        "to_archive": to_archive,
        "reason": reason,
    }
    if delta is not None:
        payload["delta"] = delta
    if sample_count is not None:
        payload["sample_count"] = sample_count
    return LifecycleEvent(event_type=EventType.MODEL_ROLLED_BACK, payload=payload)


def build_model_rejected(
    *, model_name: str, candidate_version: str, reason: str, stage: RejectionStage,
) -> LifecycleEvent:
    payload: _RejectionPayload = {
        "model_name": model_name,
        "candidate_version": candidate_version,
        "reason": reason,
        "stage": stage,
    }
    return LifecycleEvent(event_type=EventType.MODEL_REJECTED, payload=payload)
