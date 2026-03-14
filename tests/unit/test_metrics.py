"""Unit tests for Prometheus RED metrics."""

import pytest
from prometheus_client import REGISTRY

from gateway.metrics.prometheus import (
    inflight_requests,
    response_status_total,
    event_loop_lag_seconds,
    forward_duration_by_model,
)


def test_inflight_requests_inc_dec():
    """Inflight gauge increments and decrements correctly."""
    initial = inflight_requests._value.get()
    inflight_requests.inc()
    assert inflight_requests._value.get() == initial + 1
    inflight_requests.dec()
    assert inflight_requests._value.get() == initial


def test_response_status_counter():
    """Status code counter labels and increments correctly."""
    before = response_status_total.labels(status_code="200", source="gateway")._value.get()
    response_status_total.labels(status_code="200", source="gateway").inc()
    after = response_status_total.labels(status_code="200", source="gateway")._value.get()
    assert after == before + 1


def test_response_status_provider_source():
    """Provider source is tracked separately."""
    response_status_total.labels(status_code="502", source="provider").inc()
    val = response_status_total.labels(status_code="502", source="provider")._value.get()
    assert val >= 1


def test_event_loop_lag_gauge():
    """Event loop lag gauge can be set."""
    event_loop_lag_seconds.set(0.005)
    assert event_loop_lag_seconds._value.get() == 0.005


def test_forward_duration_by_model_histogram():
    """Per-model histogram records observations."""
    forward_duration_by_model.labels(model="gpt-4").observe(0.5)
    # Verify it doesn't raise
    sample = REGISTRY.get_sample_value(
        "walacor_gateway_forward_duration_by_model_seconds_count",
        {"model": "gpt-4"},
    )
    assert sample is not None and sample >= 1


def test_forward_duration_by_model_buckets():
    """Per-model histogram uses correct bucket boundaries."""
    forward_duration_by_model.labels(model="test-bucket").observe(0.3)
    # Check the 0.5 bucket exists
    sample = REGISTRY.get_sample_value(
        "walacor_gateway_forward_duration_by_model_seconds_bucket",
        {"model": "test-bucket", "le": "0.5"},
    )
    assert sample is not None and sample >= 1


def test_metrics_content_includes_new_metrics():
    """get_metrics_content() includes the new metric families."""
    from gateway.metrics.prometheus import get_metrics_content
    content = get_metrics_content().decode()
    assert "walacor_gateway_inflight_requests" in content
    assert "walacor_gateway_response_status_total" in content
    assert "walacor_gateway_event_loop_lag_seconds" in content
    assert "walacor_gateway_forward_duration_by_model_seconds" in content
