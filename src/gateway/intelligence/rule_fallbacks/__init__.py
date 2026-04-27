"""Deterministic rule-based fallbacks for intelligence models.

Rule fallbacks exist so the gateway has a non-zero, reproducible safety
signal on day 1 — before the bundled ONNX baselines have processed
enough local samples for trailing-window accuracy to be meaningful, and
before the self-learning loop has trained anything specific to this
deployment.

Design constraints (per CLAUDE.md feedback memory `observer_identity`):
  • Rules log alongside ONNX verdicts; they DO NOT enforce. Enforcement
    flows through declarative policies (`core/policy_engine.py`).
  • Rules are pure functions — no I/O, no model loading, no network.
  • Every rule has a stable identifier so dashboards can break down
    "what fired" by category, even before label distributions stabilise.
"""
from gateway.intelligence.rule_fallbacks.safety import (
    RuleVerdict,
    evaluate_safety,
)

__all__ = ["RuleVerdict", "evaluate_safety"]
