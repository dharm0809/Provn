"""Step 2: Pre-inference policy evaluation. Fail-closed when policy cache stale."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse

from gateway.config import get_settings
from gateway.adapters.base import ModelCall
from gateway.cache.policy_cache import PolicyCache
from gateway.util.redact import RedactedString

logger = logging.getLogger(__name__)


@dataclass
class PolicyBlockDetail:
    """Structured explanation for a policy block decision."""

    policy_name: str
    policy_version: int
    blocking_rule: dict[str, Any]
    field: str
    expected: str
    actual: str

    def to_response_body(self) -> dict[str, Any]:
        return {
            "error": "Blocked by policy",
            "reason": (
                f"Policy '{self.policy_name}' v{self.policy_version} blocked: "
                f"field '{self.field}' is '{self.actual}', expected '{self.expected}'"
            ),
            "governance_decision": {
                "policy_name": self.policy_name,
                "policy_version": self.policy_version,
                "blocking_rule_field": self.field,
                "blocking_rule_operator": self.blocking_rule.get("operator", "equals"),
                "expected_value": self.expected,
                "actual_value": self.actual,
            },
        }


def _extract_policy_block(results, version: int) -> tuple[dict[str, Any], str | None]:
    """Build a structured 403 response body + short human reason from policy results.

    Finds the first failing blocking policy result. Returns (body, reason) where
    `reason` is a short string suitable for the Attempts dashboard (e.g.
    "policy 'gpt-only' blocked field model_id: expected 'gpt-*', got 'qwen3:4b'").
    """
    for r in results:
        if r.result != "fail":
            continue
        details = r.details or {}
        field = details.get("failed_field") or details.get("failed_check", "unknown")
        expected = str(details.get("expected", details.get("required", "unknown")))
        actual = str(details.get("actual", "unknown"))
        policy_label = r.policy_name or r.policy_id or "unknown"
        detail = PolicyBlockDetail(
            policy_name=policy_label,
            policy_version=version,
            blocking_rule={"field": field, "operator": "equals"},
            field=field,
            expected=expected,
            actual=actual,
        )
        reason = f"policy {policy_label!r} blocked field {field}: expected {expected!r}, got {actual!r}"
        return detail.to_response_body(), reason

    # Fallback — should not happen but be defensive
    return {"error": "Blocked by policy"}, None


def evaluate_pre_inference(
    policy_cache: PolicyCache,
    call: ModelCall,
    attestation_id: str,
    attestation_context: dict,
) -> tuple[bool, int, str, JSONResponse | None, str | None]:
    """
    Evaluate policies against (attestation + prompt context).
    Returns (blocked, policy_version, policy_result, error_response, failure_reason).
    If policy cache is stale, returns (True, 0, "fail_closed", 503 response, reason).
    """
    if policy_cache.is_stale:
        return (
            True,
            policy_cache.version,
            "fail_closed",
            JSONResponse(
                {"error": "Policy cache stale, control plane unreachable"},
                status_code=503,
            ),
            "policy cache stale, control plane unreachable",
        )

    context = dict(attestation_context)
    context["prompt"] = {"text": RedactedString(call.prompt_text)}

    tenant_id = attestation_context.get("tenant_id") or get_settings().gateway_tenant_id
    blocked, results, version = policy_cache.evaluate(context, tenant_id)
    policy_result = "blocked_by_policy" if blocked else "pass"
    if blocked:
        body, reason = _extract_policy_block(results, version)
        return True, version, policy_result, JSONResponse(body, status_code=403), reason
    return False, version, policy_result, None, None
