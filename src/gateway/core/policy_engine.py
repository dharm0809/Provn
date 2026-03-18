"""Policy evaluation logic."""

from __future__ import annotations

import re
from typing import Any

from gateway.core.constants import EnforcementLevel
from gateway.core.models.policy import PolicyEvalResult

VERIFICATION_LEVEL_ORDER = {
    "self_reported": 1,
    "loader_attested": 2,
    "server_verified": 3,
    "tee_measured": 4,
    "periodically_reverified": 5,
}


def _verification_level_rank(level: str) -> int:
    return VERIFICATION_LEVEL_ORDER.get(level.lower(), 0)


def _resolve_field(data: dict, field_path: str) -> Any:
    """Resolve dot-separated field path. Unwraps RedactedValue if present."""
    parts = field_path.split(".")
    current: Any = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    if hasattr(current, "value"):  # RedactedValue
        current = current.value
    return current


def _evaluate_rule(operator: str, actual: Any, expected: Any, case_sensitive: bool = True) -> bool:
    """Evaluate a single rule against actual data."""
    if actual is None:
        return False
    actual_str = str(actual)
    expected_str = str(expected)
    if not case_sensitive:
        actual_str = actual_str.lower()
        expected_str = expected_str.lower()
    match operator:
        case "equals":
            return actual_str == expected_str
        case "not_equals":
            return actual_str != expected_str
        case "contains":
            return expected_str in actual_str
        case "not_contains":
            return expected_str not in actual_str
        case "regex":
            return bool(re.search(expected_str, actual_str))
        case "not_regex":
            return not re.search(expected_str, actual_str)
        case "greater_than":
            try:
                return float(actual) > float(expected)
            except (ValueError, TypeError):
                return False
        case "less_than":
            try:
                return float(actual) < float(expected)
            except (ValueError, TypeError):
                return False
        case "in_list":
            if isinstance(expected, list):
                if isinstance(actual, list):
                    expected_strs = [str(x).lower() if not case_sensitive else str(x) for x in expected]
                    for item in actual:
                        item_str = str(item).lower() if not case_sensitive else str(item)
                        if item_str not in expected_strs:
                            return False
                    return True
                return actual_str in [str(x) for x in expected]
            return False
        case _:
            return False


def evaluate_policies(
    attestation_context: dict[str, Any],
    policies: list[dict[str, Any]],
) -> tuple[bool, list[PolicyEvalResult]]:
    """
    Evaluate a set of policies against an attestation (and optional prompt) context.
    Returns (blocked_by_blocking_policy, list of PolicyEvalResult).
    """
    results: list[PolicyEvalResult] = []
    blocking_failures = 0

    for policy_record in policies:
        if policy_record.get("status") != "active":
            continue
        all_pass = True
        rule_details: dict[str, Any] = {}

        min_level = policy_record.get("minimum_verification_level")
        if min_level:
            current_level = attestation_context.get("verification_level", "self_reported")
            current_str = current_level.value if hasattr(current_level, "value") else str(current_level)
            required_str = min_level if isinstance(min_level, str) else getattr(min_level, "value", str(min_level))
            if _verification_level_rank(current_str) < _verification_level_rank(required_str):
                all_pass = False
                rule_details["failed_check"] = "minimum_verification_level"
                rule_details["required"] = required_str
                rule_details["actual"] = current_str

        for rule in policy_record.get("rules", []):
            actual = _resolve_field(attestation_context, rule["field"])
            condition_met = _evaluate_rule(
                rule.get("operator", "equals"),
                actual,
                rule["value"],
                rule.get("case_sensitive", True),
            )
            action = rule.get("action", "allow")
            # deny rules: block when condition MATCHES (blacklist)
            # allow rules: block when condition DOESN'T match (whitelist)
            rule_failed = condition_met if action == "deny" else not condition_met
            if rule_failed:
                all_pass = False
                rule_details["failed_field"] = rule["field"]
                rule_details["expected"] = rule["value"]
                rule_details["actual"] = actual

        for prompt_rule in policy_record.get("prompt_rules", []):
            actual = _resolve_field(attestation_context, prompt_rule["field"])
            passed = _evaluate_rule(
                prompt_rule.get("operator", "equals"),
                actual,
                prompt_rule["value"],
                prompt_rule.get("case_sensitive", False),
            )
            if not passed:
                all_pass = False
                rule_details["failed_check"] = "prompt_rule"
                rule_details["failed_field"] = prompt_rule["field"]
                break

        for rag_rule in policy_record.get("rag_rules", []):
            actual = _resolve_field(attestation_context, rag_rule["field"])
            passed = _evaluate_rule(
                rag_rule.get("operator", "equals"),
                actual,
                rag_rule["value"],
                rag_rule.get("case_sensitive", True),
            )
            if not passed:
                all_pass = False
                rule_details["failed_check"] = "rag_rule"
                rule_details["failed_field"] = rag_rule["field"]
                break

        result_str = "pass" if all_pass else "fail"
        if not all_pass:
            enforcement = policy_record.get("enforcement_level", "blocking")
            if enforcement == EnforcementLevel.BLOCKING.value:
                blocking_failures += 1

        results.append(
            PolicyEvalResult(
                policy_id=policy_record.get("policy_id", ""),
                policy_name=policy_record.get("policy_name", ""),
                result=result_str,
                details=rule_details if not all_pass else None,
            )
        )

    blocked = blocking_failures > 0
    return blocked, results
