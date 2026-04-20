"""Anomaly detection for LLM gateway records.

Two layers, both inline (< 2ms per record), zero external dependencies:

Layer 1 — Rule-based checks: deterministic if-statements catching
impossible values, missing fields, broken invariants.

Layer 2 — Statistical baselines (EMA): per-model rolling averages for
latency, token counts, response length. Flags when |z-score| > 3.

Anomaly flags are stored in the execution record metadata as
`anomalies: ["latency_3.2sigma", "empty_response", ...]` and
surfaced in the lineage dashboard.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Finite set of deterministic anomaly codes. Statistical codes (e.g.
# "latency_3.2sigma") are free-form strings appended by _check_stats.
AnomalyCode = Literal[
    "empty_response",
    "token_sum_mismatch",
    "latency_extreme",
    "missing_execution_id",
    "response_after_deny",
]

WarningCode = Literal[
    "prompt_tokens_zero",
    "token_ratio_low",
    "token_ratio_high",
    "latency_suspiciously_fast",
    "missing_session_id",
    "missing_model_id",
    "chain_hash_missing",
    "content_after_error_finish",
]


@dataclass
class AnomalyReport:
    """Result of anomaly detection on one record."""

    anomalies: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_anomalies(self) -> bool:
        return len(self.anomalies) > 0

    def to_list(self) -> list[str]:
        """All flags combined for metadata storage."""
        return self.anomalies + [f"warn:{w}" for w in self.warnings]


# ── Layer 1: Rule-Based Checks ──────────────────────────────────────────────

def _check_rules(record: dict, report: AnomalyReport) -> None:
    """Deterministic rule checks. ~0.5ms."""

    # 1. Empty response with 200 status
    response = record.get("response_content") or ""
    thinking = record.get("thinking_content") or ""
    if not response and not thinking:
        report.anomalies.append("empty_response")

    # 2. Token count sanity
    prompt_tokens = record.get("prompt_tokens", 0) or 0
    completion_tokens = record.get("completion_tokens", 0) or 0
    total_tokens = record.get("total_tokens", 0) or 0

    # prompt_text exists but prompt_tokens = 0
    prompt_text = record.get("prompt_text") or ""
    if prompt_text and prompt_tokens == 0:
        report.warnings.append("prompt_tokens_zero")

    # Total doesn't match sum
    if total_tokens > 0 and prompt_tokens > 0 and completion_tokens > 0:
        expected = prompt_tokens + completion_tokens
        if abs(total_tokens - expected) > max(2, expected * 0.01):
            report.anomalies.append("token_sum_mismatch")

    # Token-to-content ratio check
    if completion_tokens > 0 and response:
        ratio = len(response) / completion_tokens
        if ratio < 0.5:  # Less than 0.5 chars per token — suspicious
            report.warnings.append("token_ratio_low")
        elif ratio > 20:  # More than 20 chars per token — unusual
            report.warnings.append("token_ratio_high")

    # 3. Latency sanity
    latency = record.get("latency_ms", 0) or 0
    if latency > 0:
        if latency < 10 and completion_tokens > 5:
            report.warnings.append("latency_suspiciously_fast")
        if latency > 300000:  # > 5 minutes
            report.anomalies.append("latency_extreme")

    # 4. Missing required fields
    if not record.get("execution_id"):
        report.anomalies.append("missing_execution_id")
    if not record.get("session_id"):
        report.warnings.append("missing_session_id")
    if not record.get("model_id") and not record.get("model_attestation_id"):
        report.warnings.append("missing_model_id")

    # 5. Session chain integrity
    seq = record.get("sequence_number")
    rec_hash = record.get("record_hash")
    if seq is not None and not rec_hash:
        report.warnings.append("chain_hash_missing")

    # 6. Duplicate detection hint
    # (actual dedup needs cross-record context, but we flag missing execution_id)

    # 7. Policy contradiction
    policy_result = record.get("policy_result", "")
    if policy_result == "denied" and response:
        report.warnings.append("response_after_deny")

    # 8. Finish reason vs content
    metadata = record.get("metadata") or {}
    finish = metadata.get("finish_reason") or record.get("finish_reason") or ""
    if finish == "error" and response and len(response) > 50:
        report.warnings.append("content_after_error_finish")


# ── Layer 2: Statistical Baselines (EMA) ────────────────────────────────────

@dataclass
class _ModelStats:
    """Exponential moving average + variance per metric per model."""

    ema: float = 0.0
    emv: float = 0.0  # exponential moving variance
    count: int = 0

    def update(self, value: float, alpha: float = 0.05) -> float:
        """Update EMA/EMV and return z-score."""
        if self.count == 0:
            self.ema = value
            self.emv = 0.0
            self.count = 1
            return 0.0

        self.count += 1
        delta = value - self.ema
        self.ema = alpha * value + (1 - alpha) * self.ema
        self.emv = alpha * (delta ** 2) + (1 - alpha) * self.emv

        std = math.sqrt(self.emv) if self.emv > 0 else 1.0
        z = delta / std if std > 0.001 else 0.0
        return z


class AnomalyDetector:
    """Inline anomaly detector — rules + statistical baselines.

    Call `detect(record)` on every execution record. Returns AnomalyReport
    with flags to store in metadata.

    Usage:
        detector = AnomalyDetector()
        report = detector.detect(record)
        if report.has_anomalies:
            record["metadata"]["anomalies"] = report.to_list()
    """

    Z_THRESHOLD = 3.0  # Standard deviations for flagging

    def __init__(self) -> None:
        # Per-model stats: {model_id: {metric_name: _ModelStats}}
        self._stats: dict[str, dict[str, _ModelStats]] = defaultdict(
            lambda: defaultdict(_ModelStats)
        )
        self._request_counts: dict[str, int] = defaultdict(int)

    def detect(self, record: dict[str, Any]) -> AnomalyReport:
        """Run all anomaly checks on an execution record.

        Returns AnomalyReport with anomaly flags and warnings.
        Total cost: < 2ms.
        """
        report = AnomalyReport()

        # Layer 1: Rules
        _check_rules(record, report)

        # Layer 2: Statistical baselines
        self._check_stats(record, report)

        if report.has_anomalies:
            model = record.get("model_id") or "unknown"
            logger.info(
                "Anomalies detected for model=%s: %s",
                model, ", ".join(report.anomalies),
            )

        return report

    def _check_stats(self, record: dict, report: AnomalyReport) -> None:
        """EMA-based statistical anomaly detection."""
        model = record.get("model_id") or record.get("model_attestation_id") or "unknown"
        stats = self._stats[model]
        self._request_counts[model] += 1

        # Need at least 10 observations before flagging
        if self._request_counts[model] < 10:
            return

        # Check latency
        latency = record.get("latency_ms", 0)
        if latency and latency > 0:
            z = stats["latency"].update(float(latency))
            if abs(z) > self.Z_THRESHOLD:
                report.anomalies.append(f"latency_{z:.1f}sigma")

        # Check completion tokens
        ct = record.get("completion_tokens", 0) or 0
        if ct > 0:
            z = stats["completion_tokens"].update(float(ct))
            if abs(z) > self.Z_THRESHOLD:
                report.warnings.append(f"completion_tokens_{z:.1f}sigma")

        # Check response length
        response = record.get("response_content") or ""
        if response:
            z = stats["response_length"].update(float(len(response)))
            if abs(z) > self.Z_THRESHOLD:
                report.warnings.append(f"response_length_{z:.1f}sigma")

        # Check prompt tokens
        pt = record.get("prompt_tokens", 0) or 0
        if pt > 0:
            z = stats["prompt_tokens"].update(float(pt))
            if abs(z) > self.Z_THRESHOLD:
                report.warnings.append(f"prompt_tokens_{z:.1f}sigma")

    def get_stats(self) -> dict[str, Any]:
        """Return detector stats for health endpoint."""
        return {
            "models_tracked": len(self._stats),
            "total_records_analyzed": sum(self._request_counts.values()),
            "per_model": {
                model: {
                    "records": self._request_counts[model],
                    "latency_ema": round(stats.get("latency", _ModelStats()).ema, 1),
                    "completion_tokens_ema": round(stats.get("completion_tokens", _ModelStats()).ema, 1),
                }
                for model, stats in self._stats.items()
            },
        }
