"""Unit tests for Phase 27: pipeline timing data capture."""

import pytest

from gateway.pipeline.hasher import build_execution_record
from gateway.adapters.base import ModelCall, ModelResponse


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _make_call(**overrides):
    defaults = dict(
        model_id="gpt-4", prompt_text="hello", raw_body=b'{}',
        is_streaming=False, metadata={}, provider="openai",
    )
    defaults.update(overrides)
    return ModelCall(**defaults)


def _make_response(**overrides):
    defaults = dict(
        content="world", raw_body=b'{}',
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        provider_request_id="req-1", model_hash=None,
    )
    defaults.update(overrides)
    return ModelResponse(**defaults)


def test_timings_stored_in_execution_record():
    """Timings dict is included in the execution record when provided."""
    timings = {
        "attestation_ms": 1.2,
        "policy_ms": 0.8,
        "forward_ms": 150.3,
        "content_analysis_ms": 5.0,
        "chain_ms": 0.3,
        "write_ms": 2.1,
        "total_ms": 160.0,
    }
    record = build_execution_record(
        call=_make_call(),
        model_response=_make_response(),
        attestation_id="att-1",
        policy_version=1,
        policy_result="pass",
        tenant_id="t1",
        gateway_id="gw-1",
        timings=timings,
    )
    assert record["timings"] == timings


def test_timings_has_all_expected_keys():
    """Timings dict should contain all pipeline step keys when fully populated."""
    expected_keys = {
        "attestation_ms", "policy_ms", "budget_ms", "pre_checks_ms",
        "forward_ms", "content_analysis_ms",
        "chain_ms", "write_ms", "total_ms",
    }
    timings = {k: 1.0 for k in expected_keys}
    record = build_execution_record(
        call=_make_call(),
        model_response=_make_response(),
        attestation_id="att-1",
        policy_version=1,
        policy_result="pass",
        tenant_id="t1",
        gateway_id="gw-1",
        timings=timings,
    )
    assert set(record["timings"].keys()) >= expected_keys


def test_timings_are_positive_numbers():
    """All timing values should be non-negative numbers."""
    timings = {
        "attestation_ms": 1.2,
        "policy_ms": 0.0,
        "forward_ms": 150.3,
        "content_analysis_ms": 5.0,
        "chain_ms": 0.3,
        "write_ms": 2.1,
        "total_ms": 160.0,
    }
    record = build_execution_record(
        call=_make_call(),
        model_response=_make_response(),
        attestation_id="att-1",
        policy_version=1,
        policy_result="pass",
        tenant_id="t1",
        gateway_id="gw-1",
        timings=timings,
    )
    for key, value in record["timings"].items():
        assert isinstance(value, (int, float)), f"{key} is not a number"
        assert value >= 0, f"{key} is negative"


def test_timings_default_none_when_not_provided():
    """Timings should be None when not provided."""
    record = build_execution_record(
        call=_make_call(),
        model_response=_make_response(),
        attestation_id="att-1",
        policy_version=1,
        policy_result="pass",
        tenant_id="t1",
        gateway_id="gw-1",
    )
    assert record["timings"] is None
