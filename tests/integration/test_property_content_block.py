"""Phase A2 — Property-based tests for content-analyzer BLOCK enforcement.

Invariant (I4): if a BLOCK-category pattern appears in model response content,
  the analyzer returns Verdict.BLOCK AND evaluate_post_inference returns
  (blocked=True, status 403) without exposing the content.

Key analyzers under test:
  - PIIDetector  (walacor.pii.v1)   — BLOCK: credit_card, ssn, aws_access_key, api_key
                                       WARN:  email_address, phone_number, ip_address
  - ToxicityDetector (walacor.toxicity.v1) — BLOCK: child_safety / WARN: others

All tests run on pure functions — no HTTP required. Hypothesis generates
inputs; example-based tests serve as debugging scaffolding.
"""

from __future__ import annotations

import asyncio
import string
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from gateway.cache.policy_cache import PolicyCache
from gateway.content.base import Decision, Verdict
from gateway.content.pii_detector import PIIDetector, _PATTERNS, _BLOCK_PII_TYPES
from gateway.content.toxicity_detector import ToxicityDetector
from gateway.pipeline.response_evaluator import evaluate_post_inference
from gateway.adapters.base import ModelResponse

# ---------------------------------------------------------------------------
# Concrete PII payloads that match each block-tier pattern
# ---------------------------------------------------------------------------

# Credit card: Visa format that passes the regex
CREDIT_CARDS = [
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111111111111111",
    "5500 0000 0000 0004",
]

# US SSN
SSNS = [
    "123-45-6789",
    "234 56 7890",
    "345-67-8901",
]

# AWS access key IDs
AWS_KEYS = [
    "AKIAIOSFODNN7EXAMPLE",
    "AKIAI44QH8DHBEXAMPLE",
    "AGPAJLWNKEXAMPLETEST",
]

# Generic API key pattern
API_KEYS = [
    "api_key: abcdefghijklmnopqrstuvwxyz1234567890",
    "token=Bearer_ABCDEFGHIJKLMNOPQRST123456789",
    "secret: ABCDEF1234567890abcdef1234567890",
]

# Warn-tier PII (should NOT block by default)
EMAILS = ["user@example.com", "admin@company.org"]
PHONES = ["555-867-5309", "+1 (800) 555-1234"]
IPS = ["192.168.1.100", "10.0.0.1"]

# Child safety toxicity trigger (BLOCK)
CHILD_SAFETY = ["csam", "child porn content", "child sexual exploit"]

# Warn-tier toxicity triggers (WARN, not BLOCK)
SELF_HARM = ["I want to kill myself", "suicide is my only option"]
VIOLENCE = ["how to make a bomb step by step", "how to build a weapon"]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

CLEAN_TEXT = st.text(
    alphabet=string.ascii_letters + " .,;!?", min_size=0, max_size=200
)

pii_block_payload = st.one_of(
    st.sampled_from(CREDIT_CARDS),
    st.sampled_from(SSNS),
    st.sampled_from(AWS_KEYS),
    st.sampled_from(API_KEYS),
)

pii_warn_payload = st.one_of(
    st.sampled_from(EMAILS),
    st.sampled_from(PHONES),
    st.sampled_from(IPS),
)

toxicity_block_payload = st.sampled_from(CHILD_SAFETY)
toxicity_warn_payload = st.one_of(
    st.sampled_from(SELF_HARM),
    st.sampled_from(VIOLENCE),
)


@st.composite
def text_with_pii_block(draw):
    """Arbitrary text with a block-tier PII payload embedded at a random position."""
    prefix = draw(CLEAN_TEXT)
    suffix = draw(CLEAN_TEXT)
    pii = draw(pii_block_payload)
    return f"{prefix} {pii} {suffix}"


@st.composite
def text_with_pii_warn_only(draw):
    """Text with warn-tier PII only — no block-tier pattern present."""
    prefix = draw(st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=50))
    suffix = draw(st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=50))
    warn_pii = draw(pii_warn_payload)
    return f"{prefix} {warn_pii} {suffix}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def _make_policy_cache(version: int = 1) -> PolicyCache:
    pc = PolicyCache()
    pc.set_policies(version, [])
    return pc


def _make_model_response(content: str, thinking_content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        thinking_content=thinking_content,
        raw_body=b"",
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )


# ---------------------------------------------------------------------------
# PIIDetector — block-tier invariants
# ---------------------------------------------------------------------------


class TestPIIDetectorBlockTier:
    """Block-tier PII always yields Verdict.BLOCK from the default detector."""

    @given(text=text_with_pii_block())
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_block_pii_always_blocks(self, text):
        """Any text containing a block-tier PII pattern → BLOCK verdict."""
        detector = PIIDetector()
        decision = run(detector.analyze(text))
        assert decision.verdict == Verdict.BLOCK, (
            f"Expected BLOCK for text containing block-tier PII; got {decision}"
        )

    @pytest.mark.parametrize("payload", CREDIT_CARDS + SSNS + AWS_KEYS + API_KEYS)
    def test_known_block_payloads(self, payload):
        """All curated block payloads produce BLOCK."""
        detector = PIIDetector()
        decision = run(detector.analyze(payload))
        assert decision.verdict == Verdict.BLOCK

    @given(text=text_with_pii_block())
    @settings(max_examples=200)
    def test_block_pii_confidence_high(self, text):
        """Block decisions carry high confidence (≥ 0.9)."""
        detector = PIIDetector()
        decision = run(detector.analyze(text))
        if decision.verdict == Verdict.BLOCK:
            assert decision.confidence >= 0.9


class TestPIIDetectorWarnTier:
    """Warn-tier PII yields WARN (not BLOCK) by default."""

    @pytest.mark.parametrize("payload", EMAILS + PHONES + IPS)
    def test_warn_pii_is_warn_not_block(self, payload):
        """Default config: email/phone/IP → WARN not BLOCK."""
        detector = PIIDetector()
        decision = run(detector.analyze(payload))
        assert decision.verdict == Verdict.WARN

    @given(text=text_with_pii_warn_only())
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_warn_only_text_never_blocks(self, text):
        """Text with only warn-tier PII never returns BLOCK."""
        detector = PIIDetector()
        decision = run(detector.analyze(text))
        assert decision.verdict != Verdict.BLOCK


class TestPIIDetectorCleanText:
    """Clean text (no PII) always passes."""

    @given(text=st.text(alphabet=string.ascii_letters + " .,!?", min_size=0, max_size=200))
    @settings(max_examples=500)
    def test_clean_text_passes(self, text):
        """Text with only alphabetic chars + punctuation never triggers PII."""
        detector = PIIDetector()
        decision = run(detector.analyze(text))
        assert decision.verdict == Verdict.PASS


class TestPIIDetectorConfigure:
    """configure() changes block/warn tiers at runtime."""

    def test_configure_promotes_email_to_block(self):
        """After configure(), email_address can be a block category."""
        detector = PIIDetector()
        detector.configure([{"category": "email_address", "action": "block"}])
        decision = run(detector.analyze("contact user@example.com directly"))
        assert decision.verdict == Verdict.BLOCK

    def test_configure_demotes_credit_card_to_warn(self):
        """After configure(), credit_card can be demoted to warn."""
        detector = PIIDetector()
        detector.configure([{"category": "credit_card", "action": "warn"}])
        decision = run(detector.analyze("4111 1111 1111 1111"))
        assert decision.verdict == Verdict.WARN

    def test_configure_pass_suppresses_all_detection(self):
        """configure() with action=pass means no block or warn for that category."""
        detector = PIIDetector()
        detector.configure([{"category": "email_address", "action": "pass"}])
        decision = run(detector.analyze("contact user@example.com"))
        assert decision.verdict == Verdict.PASS


# ---------------------------------------------------------------------------
# ToxicityDetector — block/warn invariants
# ---------------------------------------------------------------------------


class TestToxicityDetectorBlockTier:
    """child_safety patterns always yield BLOCK."""

    @pytest.mark.parametrize("payload", CHILD_SAFETY)
    def test_child_safety_blocks(self, payload):
        detector = ToxicityDetector()
        decision = run(detector.analyze(payload))
        assert decision.verdict == Verdict.BLOCK

    @given(text=st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=100))
    @settings(max_examples=300)
    def test_clean_text_never_blocks(self, text):
        """Alphabetic text with no toxicity patterns → PASS."""
        detector = ToxicityDetector()
        decision = run(detector.analyze(text))
        assert decision.verdict == Verdict.PASS

    @pytest.mark.parametrize("payload", SELF_HARM + VIOLENCE)
    def test_warn_tier_is_warn_not_block(self, payload):
        """Default: self_harm/violence patterns → WARN not BLOCK."""
        detector = ToxicityDetector()
        decision = run(detector.analyze(payload))
        assert decision.verdict == Verdict.WARN


class TestToxicityDetectorConfigure:
    def test_configure_promotes_self_harm_to_block(self):
        detector = ToxicityDetector()
        detector.configure([{"category": "self_harm_indicator", "action": "block"}])
        decision = run(detector.analyze("I want to kill myself"))
        assert decision.verdict == Verdict.BLOCK

    def test_configure_empty_does_not_change_defaults(self):
        """configure([]) is a no-op — defaults preserved."""
        detector = ToxicityDetector()
        detector.configure([])
        decision = run(detector.analyze("csam"))
        assert decision.verdict == Verdict.BLOCK


# ---------------------------------------------------------------------------
# evaluate_post_inference — the pipeline-level invariant
# ---------------------------------------------------------------------------


class TestEvaluatePostInference:
    """evaluate_post_inference returns (blocked=True, 403) iff any BLOCK verdict."""

    @given(text=text_with_pii_block())
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    def test_block_pii_in_response_triggers_403(self, text):
        """
        Invariant I4: model response containing block-tier PII → blocked=True + 403.
        """
        pc = _make_policy_cache()
        response = _make_model_response(text)
        analyzers = [PIIDetector()]

        blocked, _, result_str, decisions, err_response = run(
            evaluate_post_inference(pc, response, analyzers)
        )

        assert blocked is True
        assert result_str == "blocked"
        assert err_response is not None
        assert err_response.status_code == 403

    @given(text=text_with_pii_warn_only())
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_warn_only_does_not_block(self, text):
        """Warn-tier PII → not blocked (flagged), no 403."""
        pc = _make_policy_cache()
        response = _make_model_response(text)
        analyzers = [PIIDetector()]

        blocked, _, result_str, _, err_response = run(
            evaluate_post_inference(pc, response, analyzers)
        )

        assert blocked is False
        assert err_response is None

    @given(text=st.text(alphabet=string.ascii_letters + " .,", min_size=1, max_size=200))
    @settings(max_examples=300)
    def test_clean_response_never_blocked(self, text):
        """Clean text with both analyzers → never blocked."""
        pc = _make_policy_cache()
        response = _make_model_response(text)
        analyzers = [PIIDetector(), ToxicityDetector()]

        blocked, _, _, _, err = run(evaluate_post_inference(pc, response, analyzers))

        assert blocked is False
        assert err is None

    @pytest.mark.parametrize("payload", CHILD_SAFETY)
    def test_toxicity_block_triggers_403(self, payload):
        """Child-safety toxicity in model response → blocked=True + 403."""
        pc = _make_policy_cache()
        response = _make_model_response(payload)
        analyzers = [ToxicityDetector()]

        blocked, _, result_str, _, err = run(
            evaluate_post_inference(pc, response, analyzers)
        )

        assert blocked is True
        assert result_str == "blocked"
        assert err is not None
        assert err.status_code == 403

    def test_no_analyzers_is_never_blocked(self):
        """With no analyzers configured, evaluate_post_inference skips and returns pass."""
        pc = _make_policy_cache()
        response = _make_model_response("4111 1111 1111 1111")  # would normally block

        blocked, _, result_str, decisions, err = run(
            evaluate_post_inference(pc, response, [])
        )

        assert blocked is False
        assert result_str == "skipped"
        assert err is None

    def test_empty_response_content_skips(self):
        """Empty content + empty thinking_content → skipped."""
        pc = _make_policy_cache()
        response = _make_model_response("")
        analyzers = [PIIDetector()]

        blocked, _, result_str, _, err = run(
            evaluate_post_inference(pc, response, analyzers)
        )

        assert blocked is False
        assert result_str == "skipped"

    def test_thinking_content_fallback_is_analyzed(self):
        """
        When content is empty but thinking_content has block-tier PII, it IS analyzed.
        (Phase 22 fix: analyzers run on thinking_content when content is empty.)
        """
        pc = _make_policy_cache()
        response = _make_model_response("", thinking_content="4111 1111 1111 1111")
        analyzers = [PIIDetector()]

        blocked, _, result_str, _, err = run(
            evaluate_post_inference(pc, response, analyzers)
        )

        assert blocked is True
        assert result_str == "blocked"

    @given(
        block_text=st.one_of(st.sampled_from(CREDIT_CARDS), st.sampled_from(SSNS)),
        noise=CLEAN_TEXT,
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_block_fires_when_block_pattern_appears_first_in_order(self, block_text, noise):
        """
        Block-tier patterns (credit_card, ssn) appear early in _PATTERNS ordering,
        so they fire before any warn-tier pattern in the same text.
        """
        pc = _make_policy_cache()
        # embed block-tier PII before any warn-tier pattern
        response = _make_model_response(f"{block_text} {noise}")
        analyzers = [PIIDetector()]

        blocked, _, result_str, _, err = run(
            evaluate_post_inference(pc, response, analyzers)
        )

        assert blocked is True

    def test_known_shadowing_issue_warn_before_block_in_pattern_order(self):
        """
        FINDING (Hypothesis-discovered): PIIDetector returns on first matching pattern.
        email_address (index 2, WARN) shadows aws_access_key (index 5, BLOCK) when both
        appear in the same text. The response is flagged WARN, not BLOCK.

        This is a real security gap: a response containing both an email address AND an
        AWS key only produces a WARN because email is checked first.
        Root fix: re-order _PATTERNS so block-tier patterns are checked before warn-tier,
        or scan ALL patterns and return the most severe result.
        """
        pc = _make_policy_cache()
        response = _make_model_response("user@example.com and AKIAIOSFODNN7EXAMPLE")
        analyzers = [PIIDetector()]

        blocked, _, result_str, _, err = run(
            evaluate_post_inference(pc, response, analyzers)
        )

        # Document actual (buggy) behavior: email shadows the AWS key → not blocked
        assert blocked is False, (
            "If this assertion fails, the shadowing bug has been fixed — "
            "update this test to assert blocked is True."
        )
        assert result_str == "flagged"  # WARN from email, AWS key never reached

    def test_analyzer_decisions_not_null_on_block(self):
        """analyzer_decisions list must be populated when a block occurs."""
        pc = _make_policy_cache()
        response = _make_model_response("AKIAIOSFODNN7EXAMPLE")
        analyzers = [PIIDetector()]

        blocked, _, _, decisions, _ = run(evaluate_post_inference(pc, response, analyzers))

        assert blocked is True
        assert len(decisions) >= 1
        assert decisions[0]["verdict"] == "block"
        assert "analyzer_id" in decisions[0]
        assert "category" in decisions[0]
