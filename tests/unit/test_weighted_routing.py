"""Unit tests for Phase 29: Weighted routing variant tracking."""

import pytest

from gateway.pipeline.hasher import build_execution_record
from gateway.adapters.base import ModelCall, ModelResponse


def _make_call(model_id="gpt-4"):
    return ModelCall(
        provider="openai", model_id=model_id,
        prompt_text="Hi", raw_body=b'{}',
        is_streaming=False, metadata={},
    )


def _make_response():
    return ModelResponse(content="Hello", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, raw_body=b'{}')


def test_variant_id_set_when_model_group_used():
    """When variant_id is provided, it appears in the execution record."""
    record = build_execution_record(
        call=_make_call(),
        model_response=_make_response(),
        attestation_id="att-1",
        policy_version=1,
        policy_result="allow",
        tenant_id="t1",
        gateway_id="gw-1",
        variant_id="gpt-4@https://api.openai.com",
    )
    assert record["variant_id"] == "gpt-4@https://api.openai.com"


def test_variant_id_none_when_single_endpoint():
    """When no variant_id is provided (single endpoint), field is None."""
    record = build_execution_record(
        call=_make_call(),
        model_response=_make_response(),
        attestation_id="att-1",
        policy_version=1,
        policy_result="allow",
        tenant_id="t1",
        gateway_id="gw-1",
    )
    assert record["variant_id"] is None


def test_variant_distribution_recorded():
    """Variant IDs are recorded faithfully for A/B analysis."""
    variants = ["gpt-4@endpoint-a", "gpt-4@endpoint-b"]
    records = []
    for v in variants:
        r = build_execution_record(
            call=_make_call(),
            model_response=_make_response(),
            attestation_id="att-1",
            policy_version=1,
            policy_result="allow",
            tenant_id="t1",
            gateway_id="gw-1",
            variant_id=v,
        )
        records.append(r)
    assert records[0]["variant_id"] == "gpt-4@endpoint-a"
    assert records[1]["variant_id"] == "gpt-4@endpoint-b"
