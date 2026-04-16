"""G3 compliance: policy enforcement and versioning. Evidence for ATO."""

from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import MagicMock
from gateway.cache.policy_cache import PolicyCache, PolicyCacheState
from gateway.pipeline.policy_evaluator import evaluate_pre_inference
from gateway.core.policy_engine import evaluate_policies


def test_g3_policy_version_in_evaluation():
    """G3: Policy evaluation returns version; execution records must include policy_version."""
    cache = PolicyCache(staleness_threshold_seconds=900)
    cache.set_policies(17, [
        {
            "policy_id": "pol_20260216_001",
            "policy_name": "Test",
            "status": "active",
            "enforcement_level": "blocking",
            "rules": [],
            "prompt_rules": [],
            "rag_rules": [],
        }
    ])
    blocked, results, version = cache.evaluate({"model_id": "gpt-4", "verification_level": "self_reported"}, "tenant")
    assert version == 17
    assert not blocked


def test_g3_blocking_policy_blocks():
    """G3: When a blocking policy fails, evaluate returns blocked=True."""
    cache = PolicyCache(staleness_threshold_seconds=900)
    # Rule: model_id must equal "allowed-only"; we pass "blocked-model" so it fails
    cache.set_policies(1, [
        {
            "policy_id": "pol_001",
            "policy_name": "Block bad",
            "status": "active",
            "enforcement_level": "blocking",
            "rules": [{"field": "model_id", "operator": "equals", "value": "allowed-only", "case_sensitive": True}],
            "prompt_rules": [],
            "rag_rules": [],
        }
    ])
    blocked, results, _ = cache.evaluate({"model_id": "blocked-model", "verification_level": "self_reported"}, "t")
    assert blocked is True


def test_g3_stale_policy_returns_503():
    """G3 fail-closed: When policy cache is stale, evaluate_pre_inference returns 503 (no request forwarded)."""
    cache = PolicyCache(staleness_threshold_seconds=900)
    cache.set_policies(1, [
        {"policy_id": "p1", "policy_name": "Test", "status": "active", "enforcement_level": "blocking", "rules": [], "prompt_rules": [], "rag_rules": []},
    ])
    # Force staleness: set state with old fetched_at
    cache._state = PolicyCacheState(
        version=1,
        policies=cache.get_policies(),
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=1000),
    )
    assert cache.is_stale is True

    call = MagicMock()
    call.prompt_text = "hello"
    call.metadata = {}
    blocked, version, policy_result, err, failure_reason = evaluate_pre_inference(
        cache, call, "att_001", {"model_id": "gpt-4", "verification_level": "self_reported", "tenant_id": "t1"}
    )
    assert err is not None
    assert err.status_code == 503
    assert blocked is True
    assert policy_result == "fail_closed"
    assert failure_reason is not None
    assert "stale" in failure_reason.lower()
