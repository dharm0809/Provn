"""Deterministic safety fallback rules.

Pure-function detectors that classify input text into one of:
  • "blocked"    — high-confidence harmful content (secrets, exfil
                   patterns, well-known jailbreak prompts).
  • "warn"       — suspicious but not definitively harmful (PII tokens,
                   profanity, hand-wavy jailbreak structure).
  • "safe"       — nothing matched.

These fire BESIDE the ONNX safety classifier — never instead of it.
The verdict-buffer logs both, and declarative policies decide whether
to act on either signal. Cold-start deployments get usable observer
signal even with a fresh baseline.

This module is intentionally narrow: high-precision regex patterns,
not a general content classifier. False positives are acceptable for
WARN; only obvious harms reach BLOCK.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ─── Patterns ──────────────────────────────────────────────────────────

# Block-tier: anything that names credentials/secrets in a way that
# almost never appears in benign prose.
_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key", re.compile(r"\b[A-Za-z0-9/+=]{40}\b(?=.*aws)", re.IGNORECASE)),
    ("private_key_pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{40,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
)

# Block-tier jailbreak prompts: known incantations that exist solely to
# bypass safety. Conservative list — entries here should be unmistakable.
_BLOCK_JAILBREAKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dan_jailbreak", re.compile(
        r"\b(?:you are|act as|pretend to be)\s+DAN\b|\bDo Anything Now\b",
        re.IGNORECASE,
    )),
    ("dev_mode_jailbreak", re.compile(
        r"\benable(?:d)? developer mode\b|\bdeveloper mode (?:enabled|output)\b",
        re.IGNORECASE,
    )),
    ("ignore_previous", re.compile(
        r"\bignore (?:all )?(?:previous|prior|above) (?:instructions|prompts|rules)\b",
        re.IGNORECASE,
    )),
    ("system_prompt_exfil", re.compile(
        r"\b(?:reveal|print|show|repeat|output)\s+(?:your|the)\s+(?:system\s+prompt|initial\s+instructions|hidden\s+rules)\b",
        re.IGNORECASE,
    )),
)

# Warn-tier PII: emails / phone numbers / credit-card-shaped digits.
# Lower precision than block tier — fine to fire on examples in benign
# educational text, since WARN never enforces.
_WARN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("phone_us", re.compile(r"\b(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")),
    ("credit_card_shape", re.compile(r"\b(?:\d[\s\-]?){13,19}\b")),
    ("ssn_us", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)

# Warn-tier prompt-injection structure: weaker signals than the block
# jailbreaks above — these sometimes appear in benign refactoring or
# meta-discussion of LLMs.
_WARN_INJECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("role_override", re.compile(
        r"\b(?:from now on|henceforth) you (?:are|will be|must)\b",
        re.IGNORECASE,
    )),
    ("policy_bypass_phrase", re.compile(
        r"\b(?:bypass|disable|turn off|override) (?:safety|guardrails?|policies)\b",
        re.IGNORECASE,
    )),
)


@dataclass(frozen=True)
class RuleVerdict:
    """Deterministic safety verdict from the rule-based fallback.

    `prediction` mirrors the ONNX safety output space ("blocked", "warn",
    "safe") so verdict-buffer storage and dashboard rendering can treat
    rule outputs alongside ONNX outputs without a translation layer.
    """
    prediction: str          # "blocked" | "warn" | "safe"
    confidence: float        # rule-based, conservative — see below
    matched_rules: tuple[str, ...]
    source: str = "rule_fallback.safety"

    @property
    def fired(self) -> bool:
        return self.prediction != "safe"


def evaluate_safety(text: str) -> RuleVerdict:
    """Apply rule patterns to `text` and return the strongest verdict.

    Confidence is fixed per tier (0.95 block / 0.6 warn / 1.0 safe-clean).
    These are *rule confidences*, not ML probabilities — they exist so
    the verdict buffer's downstream confidence-aware logic stays
    well-defined when consuming rule outputs.

    Empty / non-string input is treated as safe with full confidence.
    """
    if not isinstance(text, str) or not text:
        return RuleVerdict(prediction="safe", confidence=1.0, matched_rules=())

    matched: list[str] = []

    for name, pattern in _BLOCK_PATTERNS:
        if pattern.search(text):
            matched.append(f"block:{name}")
    for name, pattern in _BLOCK_JAILBREAKS:
        if pattern.search(text):
            matched.append(f"block:{name}")

    if matched:
        return RuleVerdict(
            prediction="blocked",
            confidence=0.95,
            matched_rules=tuple(matched),
        )

    for name, pattern in _WARN_PATTERNS:
        if pattern.search(text):
            matched.append(f"warn:{name}")
    for name, pattern in _WARN_INJECTIONS:
        if pattern.search(text):
            matched.append(f"warn:{name}")

    if matched:
        return RuleVerdict(
            prediction="warn",
            confidence=0.6,
            matched_rules=tuple(matched),
        )

    return RuleVerdict(prediction="safe", confidence=1.0, matched_rules=())
