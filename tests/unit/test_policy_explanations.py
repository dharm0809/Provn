"""Tests for structured policy decision explanations in 403 responses."""


def test_policy_block_includes_explanation():
    from gateway.pipeline.policy_evaluator import PolicyBlockDetail

    detail = PolicyBlockDetail(
        policy_name="require-active",
        policy_version=3,
        blocking_rule={"field": "status", "operator": "equals", "value": "active"},
        field="status",
        expected="active",
        actual="revoked",
    )
    body = detail.to_response_body()
    assert body["error"] == "Blocked by policy"
    assert "require-active" in body["reason"]
    assert body["governance_decision"]["blocking_rule_field"] == "status"
    assert body["governance_decision"]["expected_value"] == "active"
    assert body["governance_decision"]["actual_value"] == "revoked"


def test_content_block_includes_explanation():
    from gateway.pipeline.response_evaluator import ContentBlockDetail

    detail = ContentBlockDetail(
        analyzer_id="walacor.llama_guard.v3",
        category="child_safety",
        confidence=0.95,
        reason="S4",
    )
    body = detail.to_response_body()
    assert body["error"] == "Blocked by content analysis"
    assert body["governance_decision"]["category"] == "child_safety"
    assert body["governance_decision"]["confidence"] == 0.95
    assert body["governance_decision"]["analyzer_id"] == "walacor.llama_guard.v3"


def test_policy_block_body_structure():
    from gateway.pipeline.policy_evaluator import PolicyBlockDetail

    detail = PolicyBlockDetail(
        policy_name="no-revoked",
        policy_version=1,
        blocking_rule={"field": "status", "operator": "not_equals", "value": "revoked"},
        field="status",
        expected="revoked",
        actual="active",
    )
    body = detail.to_response_body()
    # Must have exactly these top-level keys
    assert set(body.keys()) == {"error", "reason", "governance_decision"}
    assert isinstance(body["governance_decision"], dict)


def test_content_block_body_structure():
    from gateway.pipeline.response_evaluator import ContentBlockDetail

    detail = ContentBlockDetail(
        analyzer_id="pii_detector",
        category="credit_card",
        confidence=0.99,
        reason="Credit card number detected",
    )
    body = detail.to_response_body()
    assert set(body.keys()) == {"error", "reason", "governance_decision"}
    assert isinstance(body["governance_decision"], dict)
    assert "pii_detector" in body["reason"]
    assert "credit_card" in body["reason"]
    assert "0.99" in body["reason"]


def test_policy_block_reason_format():
    from gateway.pipeline.policy_evaluator import PolicyBlockDetail

    detail = PolicyBlockDetail(
        policy_name="my-policy",
        policy_version=7,
        blocking_rule={"field": "provider", "operator": "equals", "value": "openai"},
        field="provider",
        expected="openai",
        actual="ollama",
    )
    body = detail.to_response_body()
    reason = body["reason"]
    assert "my-policy" in reason
    assert "v7" in reason
    assert "provider" in reason
    assert "ollama" in reason
    assert "openai" in reason


def test_policy_block_operator_extraction():
    from gateway.pipeline.policy_evaluator import PolicyBlockDetail

    detail = PolicyBlockDetail(
        policy_name="check-level",
        policy_version=2,
        blocking_rule={"field": "verification_level", "operator": "not_equals", "value": "self_reported"},
        field="verification_level",
        expected="self_reported",
        actual="tee_measured",
    )
    body = detail.to_response_body()
    assert body["governance_decision"]["blocking_rule_operator"] == "not_equals"


def test_policy_block_default_operator():
    from gateway.pipeline.policy_evaluator import PolicyBlockDetail

    detail = PolicyBlockDetail(
        policy_name="simple",
        policy_version=1,
        blocking_rule={},  # No operator specified
        field="status",
        expected="active",
        actual="revoked",
    )
    body = detail.to_response_body()
    assert body["governance_decision"]["blocking_rule_operator"] == "equals"


def test_content_block_confidence_formatting():
    from gateway.pipeline.response_evaluator import ContentBlockDetail

    detail = ContentBlockDetail(
        analyzer_id="toxicity",
        category="hate_speech",
        confidence=0.8,
        reason="Toxic language detected",
    )
    body = detail.to_response_body()
    # Confidence should be formatted to 2 decimal places in reason
    assert "0.80" in body["reason"]
    # Raw float preserved in governance_decision
    assert body["governance_decision"]["confidence"] == 0.8


def test_build_policy_block_response_from_results():
    """Test the helper that builds structured response from policy eval results."""
    from gateway.pipeline.policy_evaluator import _extract_policy_block

    # Simulate a PolicyEvalResult-like object
    class FakeResult:
        def __init__(self, policy_id, policy_name, result, details):
            self.policy_id = policy_id
            self.policy_name = policy_name
            self.result = result
            self.details = details

    results = [
        FakeResult("p1", "require-active", "fail", {
            "failed_field": "status",
            "expected": "active",
            "actual": "revoked",
        }),
    ]
    body, reason = _extract_policy_block(results, version=5)
    assert body["error"] == "Blocked by policy"
    assert "require-active" in body["reason"]
    assert body["governance_decision"]["policy_version"] == 5
    assert body["governance_decision"]["blocking_rule_field"] == "status"
    assert body["governance_decision"]["expected_value"] == "active"
    assert body["governance_decision"]["actual_value"] == "revoked"
    assert "require-active" in reason


def test_build_policy_block_response_skips_passing():
    """Passing policy results should be skipped; first failing result used."""
    from gateway.pipeline.policy_evaluator import _extract_policy_block

    class FakeResult:
        def __init__(self, policy_id, policy_name, result, details):
            self.policy_id = policy_id
            self.policy_name = policy_name
            self.result = result
            self.details = details

    results = [
        FakeResult("p1", "allow-all", "pass", None),
        FakeResult("p2", "block-revoked", "fail", {
            "failed_field": "status",
            "expected": "active",
            "actual": "revoked",
        }),
    ]
    body, _reason = _extract_policy_block(results, version=2)
    assert body["governance_decision"]["policy_name"] == "block-revoked"


def test_build_policy_block_response_fallback():
    """When no results have details, fallback body is returned."""
    from gateway.pipeline.policy_evaluator import _extract_policy_block

    body, reason = _extract_policy_block([], version=1)
    assert body == {"error": "Blocked by policy"}
    assert reason is None


def test_build_policy_block_response_verification_level():
    """Test handling of verification level failure details."""
    from gateway.pipeline.policy_evaluator import _extract_policy_block

    class FakeResult:
        def __init__(self, policy_id, policy_name, result, details):
            self.policy_id = policy_id
            self.policy_name = policy_name
            self.result = result
            self.details = details

    results = [
        FakeResult("p1", "require-tee", "fail", {
            "failed_check": "minimum_verification_level",
            "required": "tee_measured",
            "actual": "self_reported",
        }),
    ]
    body, _reason = _extract_policy_block(results, version=3)
    assert body["governance_decision"]["blocking_rule_field"] == "minimum_verification_level"
    assert body["governance_decision"]["expected_value"] == "tee_measured"
    assert body["governance_decision"]["actual_value"] == "self_reported"
