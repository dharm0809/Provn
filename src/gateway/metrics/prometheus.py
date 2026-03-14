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
