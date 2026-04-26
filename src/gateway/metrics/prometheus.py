"""Prometheus metrics for the gateway."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, REGISTRY, generate_latest

# Request outcomes
requests_total = Counter(
    "walacor_gateway_requests_total",
    "Total requests by outcome",
    ["provider", "model", "outcome"],
)
# outcome: allowed, blocked_attestation, blocked_policy, blocked_stale, error

# Completeness invariant (Phase 9): every attempt by disposition
gateway_attempts_total = Counter(
    "walacor_gateway_attempts_total",
    "All gateway request attempts by disposition",
    ["disposition"],
)

# Pipeline timing
pipeline_duration = Histogram(
    "walacor_gateway_pipeline_duration_seconds",
    "Pipeline step duration",
    ["step"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
forward_duration = Histogram(
    "walacor_gateway_forward_duration_seconds",
    "Upstream forward duration by provider",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# WAL
wal_pending = Gauge("walacor_gateway_wal_pending", "Number of undelivered WAL records")
wal_oldest_pending_seconds = Gauge("walacor_gateway_wal_oldest_pending_seconds", "Age of oldest undelivered record")
wal_disk_bytes = Gauge("walacor_gateway_wal_disk_bytes", "WAL disk usage in bytes")

# Sync
sync_last_success_seconds = Gauge(
    "walacor_gateway_sync_last_success_seconds",
    "Seconds since last successful sync",
    ["cache_type"],
)
cache_entries = Gauge("walacor_gateway_cache_entries", "Cache entry count", ["cache_type"])

# Delivery
delivery_total = Counter(
    "walacor_gateway_delivery_total",
    "Delivery attempts by result",
    ["result"],
)

# Phase 10: Response policy (G4)
response_policy_total = Counter(
    "walacor_gateway_response_policy_total",
    "Post-inference response policy outcomes",
    ["result"],  # pass | blocked | flagged | skipped
)

# Phase 11: Token budget
token_usage_total = Counter(
    "walacor_gateway_token_usage_total",
    "Total tokens consumed",
    ["tenant_id", "provider", "token_type"],  # token_type: prompt | completion | total
)
budget_exceeded_total = Counter(
    "walacor_gateway_budget_exceeded_total",
    "Requests rejected due to token budget exhaustion",
    ["tenant_id"],
)
budget_failopen_total = Counter(
    "walacor_gateway_budget_failopen_total",
    "Requests allowed due to budget check failure (fail-open)",
)

# Phase 13: Session chain (G5)
session_chain_active = Gauge(
    "walacor_gateway_session_chain_active",
    "Number of active sessions tracked in chain tracker",
)

# Phase 14: Tool-aware gateway
tool_calls_total = Counter(
    "walacor_gateway_tool_calls_total",
    "Total tool interactions captured by provider and strategy",
    ["provider", "tool_type", "source"],  # source: provider | gateway
)
tool_loop_iterations = Histogram(
    "walacor_gateway_tool_loop_iterations",
    "Number of tool-call loop iterations per request (active strategy)",
    ["provider"],
    buckets=(1, 2, 3, 5, 10),
)


# B.4: Semantic cache
cache_hits = Counter(
    "gateway_cache_hits_total",
    "Semantic cache hits (no LLM call made)",
    ["model"],
)
cache_misses = Counter(
    "gateway_cache_misses_total",
    "Semantic cache misses (LLM call required)",
    ["model"],
)

# Phase 26: Rate limiting + alerting
budget_utilization_ratio = Gauge(
    "walacor_gateway_budget_utilization_ratio",
    "Budget utilization ratio 0-1",
    ["tenant_id"],
)
content_blocks_total = Counter(
    "walacor_gateway_content_blocks_total",
    "Content analysis blocks by analyzer",
    ["analyzer"],
)
rate_limit_hits_total = Counter(
    "walacor_gateway_rate_limit_hits_total",
    "Rate limit 429 responses by model",
    ["model"],
)

# intelligence-layer observability.
# `verdict_buffer_size` is intentionally label-less because the buffer
# is a single shared deque across models — a per-model breakdown would
# require either separate buffers or scanning the deque on every Gauge
# read, neither of which is worth the engineering for an observability
# metric. `verdict_buffer_dropped_total` IS labeled by model because
# each drop event happens against a specific verdict and per-model
# drop pressure is the actionable signal.
verdict_buffer_dropped_total = Counter(
    "walacor_gateway_verdict_buffer_dropped_total",
    "Verdicts dropped from the in-memory buffer due to overflow",
    ["model"],
)
verdict_buffer_size = Gauge(
    "walacor_gateway_verdict_buffer_size",
    "Current verdict-buffer occupancy (shared across all models)",
)
intelligence_db_write_failures_total = Counter(
    "walacor_gateway_intelligence_db_write_failures_total",
    "SQLite write failures from the verdict-flush worker",
)
candidate_rejected_total = Counter(
    "walacor_gateway_candidate_rejected_total",
    "Candidate ONNX models rejected by reason",
    ["model", "reason"],
)
model_promoted_total = Counter(
    "walacor_gateway_model_promoted_total",
    "Candidate ONNX models promoted to production",
    ["model"],
)
# Auto- + manual-rollback events. `reason` values: "regression"
# (post-promotion validator), "manual" (operator via /v1/control/...).
model_rollback_total = Counter(
    "walacor_gateway_model_rollback_total",
    "Production model versions rolled back",
    ["model", "reason"],
)
# Fraction of recent verdicts that have a populated `divergence_signal`.
# Drift monitor + post-promotion validator key off this signal as
# ground truth; if coverage is low (< 0.10), their accuracy readings
# are statistically meaningless and they correctly skip — but
# operators need to see WHY no signals are flowing.
intelligence_signal_coverage_ratio = Gauge(
    "walacor_gateway_intelligence_signal_coverage_ratio",
    "Fraction of verdicts with a populated divergence_signal in the last hour",
    ["model"],
)
shadow_inference_errors_total = Counter(
    "walacor_gateway_shadow_inference_errors_total",
    "Shadow-inference errors (recorded as candidate_error rows)",
    ["model"],
)
distillation_run_duration_seconds = Histogram(
    "walacor_gateway_distillation_run_duration_seconds",
    "Time spent training a single distillation candidate",
    ["model"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 180.0, 600.0),
)

# Hot-path ONNX inference timed out and the caller fell back to its
# deterministic / heuristic path. Watch for spikes — sustained timeouts
# usually mean a regressed candidate or a host under CPU pressure.
onnx_inference_timeout_total = Counter(
    "walacor_gateway_onnx_inference_timeout_total",
    "Hot-path ONNX inference exceeded its timeout and fell back",
    ["model"],
)

# teacher-LLM calls from the intent harvester.
# Labels the call outcome so operators can reason about teacher cost
# vs. harvest value: `called` = request made; `failed` = teacher call
# errored or returned an unparseable label (fail-open, no signal
# recorded); `skipped` = sample skipped because `random() >= rate`.
intent_teacher_samples_total = Counter(
    "walacor_gateway_intent_teacher_samples_total",
    "Intent harvester teacher-LLM sample attempts by outcome",
    ["outcome"],
)

# RED method gap fillers
inflight_requests = Gauge(
    "walacor_gateway_inflight_requests",
    "Requests currently being processed",
)
response_status_total = Counter(
    "walacor_gateway_response_status_total",
    "HTTP response status codes by source",
    ["status_code", "source"],  # source: gateway | provider
)
event_loop_lag_seconds = Gauge(
    "walacor_gateway_event_loop_lag_seconds",
    "Asyncio event loop scheduling lag in seconds",
)
forward_duration_by_model = Histogram(
    "walacor_gateway_forward_duration_by_model_seconds",
    "Upstream forward duration by model",
    ["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def get_metrics_content() -> bytes:
    return generate_latest(REGISTRY)
