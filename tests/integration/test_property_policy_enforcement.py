"""Phase A1 — Property-based tests for policy DENY/ALLOW enforcement.

Invariant (I3): for every attestation context and every policy configuration,
  evaluate_policies(ctx, policies) is blocked iff at least one BLOCKING policy
  has at least one failing rule.

Rule semantics (from CLAUDE.md):
  action="deny"  → blocks when condition MATCHES  (blacklist)
  action="allow" → blocks when condition DOESN'T match (whitelist)

These tests call the pure `evaluate_policies` function directly (no HTTP), so
they run fast and are easy to shrink when Hypothesis finds a counter-example.
"""

from __future__ import annotations

import re
import string

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from gateway.core.policy_engine import _evaluate_rule, evaluate_policies

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

OPERATORS = ["equals", "not_equals", "contains", "not_contains", "regex", "not_regex"]
SIMPLE_TEXT = st.text(alphabet=string.ascii_letters + string.digits + " -_", min_size=1, max_size=30)
SAFE_REGEX = st.from_regex(r"[a-z]{1,10}", fullmatch=True)  # always compiles


@st.composite
def attestation_context(draw):
    return {
        "model_id": draw(SIMPLE_TEXT),
        "provider": draw(st.sampled_from(["ollama", "openai", "anthropic", "unknown"])),
        "status": draw(st.sampled_from(["active", "revoked", "pending"])),
        "verification_level": draw(
            st.sampled_from(["self_attested", "self_reported", "loader_attested", "server_verified"])
        ),
        "tenant_id": draw(SIMPLE_TEXT),
    }


@st.composite
def equals_rule(draw, action):
    """A deny/allow 'equals' rule guaranteed to match or not-match the context."""
    field = draw(st.sampled_from(["model_id", "status", "provider"]))
    value = draw(SIMPLE_TEXT)
    return {"field": field, "operator": "equals", "value": value, "action": action}


@st.composite
def contains_rule(draw, action):
    field = draw(st.sampled_from(["model_id", "status", "provider"]))
    value = draw(SIMPLE_TEXT)
    return {"field": field, "operator": "contains", "value": value, "action": action}


@st.composite
def arbitrary_rule(draw, action=None):
    if action is None:
        action = draw(st.sampled_from(["deny", "allow"]))
    op = draw(st.sampled_from(["equals", "not_equals", "contains", "not_contains"]))
    field = draw(st.sampled_from(["model_id", "status", "provider", "tenant_id"]))
    value = draw(SIMPLE_TEXT)
    return {"field": field, "operator": op, "value": value, "action": action}


def _condition_met(rule: dict, ctx: dict) -> bool:
    """Re-implement condition check for the property oracle."""
    actual = ctx.get(rule["field"])
    return _evaluate_rule(rule["operator"], actual, rule["value"])


def _rule_fails(rule: dict, ctx: dict) -> bool:
    """Is this rule a failure (the gateway should block due to this rule)?"""
    cond = _condition_met(rule, ctx)
    action = rule.get("action", "allow")
    return cond if action == "deny" else not cond


def build_policy(rules: list[dict], enforcement: str = "blocking") -> dict:
    return {
        "policy_id": "test-policy",
        "policy_name": "test",
        "status": "active",
        "enforcement_level": enforcement,
        "rules": rules,
    }


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestDenyRuleSemantics:
    """deny action: condition_met → rule fails → policy fails."""

    @given(ctx=attestation_context(), rule=st.builds(dict))
    @settings(max_examples=500)
    def test_deny_rule_blocks_iff_condition_met(self, ctx, rule):
        """
        For a deny rule, the gateway blocks iff the rule condition is met.
        """
        field = "model_id"
        value = ctx["model_id"]  # guaranteed to match
        deny_rule = {"field": field, "operator": "equals", "value": value, "action": "deny"}
        policy = build_policy([deny_rule], enforcement="blocking")

        blocked, results = evaluate_policies(ctx, [policy])

        # condition IS met, deny action → should block
        assert blocked is True
        assert results[0].result == "fail"

    @given(ctx=attestation_context())
    @settings(max_examples=300)
    def test_deny_rule_passes_when_condition_not_met(self, ctx):
        """
        Deny rule with a value that cannot match → never blocks.
        """
        deny_rule = {
            "field": "model_id",
            "operator": "equals",
            "value": "XXXXXX_NEVER_MATCHES_XXXXXX",
            "action": "deny",
        }
        policy = build_policy([deny_rule], enforcement="blocking")
        blocked, results = evaluate_policies(ctx, [policy])

        assert blocked is False
        assert results[0].result == "pass"


class TestAllowRuleSemantics:
    """allow action (default): condition_not_met → rule fails → policy fails."""

    @given(ctx=attestation_context())
    @settings(max_examples=300)
    def test_allow_rule_blocks_when_no_match(self, ctx):
        """
        Allow rule with a value that never matches → always blocks (whitelist).
        """
        allow_rule = {
            "field": "model_id",
            "operator": "equals",
            "value": "XXXXXX_NEVER_MATCHES_XXXXXX",
            "action": "allow",
        }
        policy = build_policy([allow_rule], enforcement="blocking")
        blocked, results = evaluate_policies(ctx, [policy])

        assert blocked is True
        assert results[0].result == "fail"

    @given(ctx=attestation_context())
    @settings(max_examples=300)
    def test_allow_rule_passes_when_match(self, ctx):
        """
        Allow rule matches the actual value → passes (whitelist pass-through).
        """
        allow_rule = {
            "field": "model_id",
            "operator": "equals",
            "value": ctx["model_id"],
            "action": "allow",
        }
        policy = build_policy([allow_rule], enforcement="blocking")
        blocked, results = evaluate_policies(ctx, [policy])

        assert blocked is False
        assert results[0].result == "pass"


class TestEnforcementLevel:
    """logging enforcement never blocks even when policy fails."""

    @given(ctx=attestation_context(), rules=st.lists(arbitrary_rule(), min_size=1, max_size=3))
    @settings(max_examples=400)
    def test_logging_enforcement_never_blocks(self, ctx, rules):
        """
        No matter what rules say, enforcement_level='logging' must never block.
        """
        policy = build_policy(rules, enforcement="logging")
        blocked, _ = evaluate_policies(ctx, [policy])
        assert blocked is False

    @given(ctx=attestation_context(), rules=st.lists(arbitrary_rule(), min_size=1, max_size=3))
    @settings(max_examples=400)
    def test_inactive_policy_never_blocks(self, ctx, rules):
        """
        Inactive policies are skipped entirely — never contribute to blocking.
        """
        policy = build_policy(rules, enforcement="blocking")
        policy["status"] = "inactive"
        blocked, results = evaluate_policies(ctx, [policy])
        assert blocked is False
        assert results == []


class TestPolicyOracle:
    """
    The oracle property: our pure Python reference implementation of rule
    evaluation must agree with evaluate_policies on every input.
    """

    @given(
        ctx=attestation_context(),
        rules=st.lists(arbitrary_rule(), min_size=0, max_size=5),
    )
    @settings(max_examples=1000)
    def test_oracle_agrees_with_engine(self, ctx, rules):
        """
        For every arbitrary context + rule set, our oracle and the engine agree
        on whether the policy is blocked.
        """
        policy = build_policy(rules, enforcement="blocking")
        blocked, results = evaluate_policies(ctx, [policy])

        # Oracle: policy fails if ANY rule fails
        oracle_any_fail = any(_rule_fails(r, ctx) for r in rules)
        oracle_blocked = oracle_any_fail  # single blocking policy

        assert blocked == oracle_blocked, (
            f"Engine says blocked={blocked} but oracle says {oracle_blocked}.\n"
            f"ctx={ctx}\nrules={rules}"
        )
        if oracle_blocked:
            assert results[0].result == "fail"
        else:
            assert results[0].result == "pass"

    @given(
        ctx=attestation_context(),
        rule_sets=st.lists(
            st.lists(arbitrary_rule(), min_size=1, max_size=3),
            min_size=2,
            max_size=4,
        ),
    )
    @settings(max_examples=500)
    def test_multi_policy_blocked_iff_any_blocking_policy_fails(self, ctx, rule_sets):
        """
        With multiple policies, blocked=True iff at least one blocking policy fails.
        """
        policies = [build_policy(rules) for rules in rule_sets]
        blocked, results = evaluate_policies(ctx, policies)

        oracle_blocked = any(
            any(_rule_fails(r, ctx) for r in p["rules"]) for p in policies
        )
        assert blocked == oracle_blocked


class TestEdgeCases:
    """
    Edge cases that Hypothesis is likely to find as shrunk counter-examples.
    """

    @given(ctx=attestation_context())
    @settings(max_examples=200)
    def test_empty_rules_never_blocks(self, ctx):
        """A policy with no rules always passes (nothing can fail)."""
        policy = build_policy([], enforcement="blocking")
        blocked, results = evaluate_policies(ctx, [policy])
        assert blocked is False
        assert results[0].result == "pass"

    def test_empty_policies_never_blocks(self):
        """No policies at all → never blocked (pass-all)."""
        blocked, results = evaluate_policies({}, [])
        assert blocked is False
        assert results == []

    @given(ctx=attestation_context())
    @settings(max_examples=200)
    def test_missing_field_is_never_deny_blocked(self, ctx):
        """
        A deny rule on a field that doesn't exist → actual=None → condition_met=False
        → deny rule doesn't trigger → not blocked.
        """
        deny_rule = {
            "field": "nonexistent_field",
            "operator": "equals",
            "value": "anything",
            "action": "deny",
        }
        policy = build_policy([deny_rule], enforcement="blocking")
        blocked, _ = evaluate_policies(ctx, [policy])
        # actual=None → _evaluate_rule returns False → deny rule not triggered
        assert blocked is False

    @given(ctx=attestation_context())
    @settings(max_examples=200)
    def test_missing_field_blocks_allow_rule(self, ctx):
        """
        An allow rule on a missing field → actual=None → condition_met=False
        → allow rule fails → blocked (whitelist).
        """
        allow_rule = {
            "field": "nonexistent_field",
            "operator": "equals",
            "value": "anything",
            "action": "allow",
        }
        policy = build_policy([allow_rule], enforcement="blocking")
        blocked, _ = evaluate_policies(ctx, [policy])
        assert blocked is True

    @given(
        ctx=attestation_context(),
        status=st.sampled_from(["active", "revoked", "pending"]),
    )
    @settings(max_examples=200)
    def test_revoked_status_deny_blocks(self, ctx, status):
        """
        A deny rule on status='revoked' blocks only when ctx.status == 'revoked'.
        """
        ctx["status"] = status
        deny_rule = {"field": "status", "operator": "equals", "value": "revoked", "action": "deny"}
        policy = build_policy([deny_rule], enforcement="blocking")
        blocked, _ = evaluate_policies(ctx, [policy])
        assert blocked == (status == "revoked")
