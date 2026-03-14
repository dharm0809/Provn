"""Unit tests for the PII sanitizer (Stage B.1)."""
from __future__ import annotations

import pytest

from gateway.content.pii_sanitizer import PIISanitizer, SanitizationResult, get_default_sanitizer


# ── Basic sanitize / restore ──────────────────────────────────────────────────

def test_sanitize_ssn():
    s = PIISanitizer(sanitize_types={"SSN"})
    result = s.sanitize("My SSN is 123-45-6789 please help")
    assert "123-45-6789" not in result.sanitized_text
    assert "[PII_SSN_1]" in result.sanitized_text
    assert result.pii_count == 1


def test_sanitize_ssn_spaces():
    s = PIISanitizer(sanitize_types={"SSN"})
    result = s.sanitize("SSN: 123 45 6789")
    assert "123 45 6789" not in result.sanitized_text
    assert result.pii_count == 1


def test_sanitize_credit_card():
    s = PIISanitizer(sanitize_types={"CREDIT_CARD"})
    result = s.sanitize("Card number: 4111111111111111")
    assert "4111111111111111" not in result.sanitized_text
    assert "[PII_CREDIT_CARD_1]" in result.sanitized_text
    assert result.pii_count == 1


def test_sanitize_aws_key():
    s = PIISanitizer(sanitize_types={"AWS_ACCESS_KEY"})
    result = s.sanitize("Key: AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in result.sanitized_text
    assert "[PII_AWS_ACCESS_KEY_1]" in result.sanitized_text
    assert result.pii_count == 1


def test_sanitize_api_key():
    s = PIISanitizer(sanitize_types={"API_KEY"})
    result = s.sanitize("token=abcdefghijklmnopqrstuvwxyz123456")
    assert "abcdefghijklmnopqrstuvwxyz123456" not in result.sanitized_text
    assert result.pii_count == 1


def test_restore():
    s = PIISanitizer(sanitize_types={"SSN"})
    result = s.sanitize("My SSN is 123-45-6789")
    restored = s.restore("Your SSN [PII_SSN_1] is sensitive", result.mapping)
    assert "123-45-6789" in restored
    assert "[PII_SSN_1]" not in restored


def test_roundtrip():
    s = PIISanitizer(sanitize_types={"SSN", "AWS_ACCESS_KEY"})
    original = "My SSN is 123-45-6789 and key is AKIAIOSFODNN7EXAMPLE"
    result = s.sanitize(original)
    assert result.pii_count == 2
    restored = s.restore(result.sanitized_text, result.mapping)
    assert "123-45-6789" in restored
    assert "AKIAIOSFODNN7EXAMPLE" in restored


def test_no_pii():
    s = PIISanitizer()
    result = s.sanitize("Hello, how are you today?")
    assert result.pii_count == 0
    assert result.sanitized_text == "Hello, how are you today?"
    assert result.mapping == {}


def test_multiple_same_type():
    s = PIISanitizer(sanitize_types={"SSN"})
    # Note: SSN regex rejects 9xx area codes per IRS spec; use valid SSNs
    result = s.sanitize("SSN1: 123-45-6789 and SSN2: 456-78-9012")
    assert result.pii_count == 2
    assert "[PII_SSN_1]" in result.sanitized_text
    assert "[PII_SSN_2]" in result.sanitized_text
    # Both originals gone
    assert "123-45-6789" not in result.sanitized_text
    assert "456-78-9012" not in result.sanitized_text


def test_multiple_same_type_roundtrip():
    s = PIISanitizer(sanitize_types={"SSN"})
    text = "First: 123-45-6789, second: 456-78-9012"
    result = s.sanitize(text)
    restored = s.restore(result.sanitized_text, result.mapping)
    assert "123-45-6789" in restored
    assert "456-78-9012" in restored


# ── Partial restore (LLM doesn't echo all placeholders) ───────────────────────

def test_restore_partial_placeholders():
    """LLM may not echo every placeholder — only present ones are restored."""
    s = PIISanitizer(sanitize_types={"SSN"})
    result = s.sanitize("My SSN is 123-45-6789")
    # LLM only references the placeholder once in its own response text
    restored = s.restore("I see you have a sensitive number on file.", result.mapping)
    # Should not raise; text unchanged since placeholder absent
    assert "123-45-6789" not in restored


def test_restore_empty_mapping():
    s = PIISanitizer()
    result = s.sanitize("Hello world")
    # restore with an empty mapping returns text unchanged
    restored = s.restore("Hello world", result.mapping)
    assert restored == "Hello world"


# ── Custom type selection ─────────────────────────────────────────────────────

def test_custom_types_only_ssn():
    s = PIISanitizer(sanitize_types={"SSN"})
    # A credit card in the text should NOT be sanitized when type excluded
    text = "SSN 123-45-6789, card 4111111111111111"
    result = s.sanitize(text)
    assert "[PII_SSN_1]" in result.sanitized_text
    # Credit card pattern not selected
    assert "4111111111111111" in result.sanitized_text


def test_empty_types_set():
    """If no types selected, nothing is sanitized."""
    s = PIISanitizer(sanitize_types=set())
    result = s.sanitize("SSN: 123-45-6789")
    assert result.pii_count == 0
    assert result.sanitized_text == "SSN: 123-45-6789"


# ── Default sanitizer singleton ───────────────────────────────────────────────

def test_get_default_sanitizer_singleton():
    s1 = get_default_sanitizer()
    s2 = get_default_sanitizer()
    assert s1 is s2


def test_default_sanitizer_handles_ssn():
    s = get_default_sanitizer()
    result = s.sanitize("My SSN is 123-45-6789")
    assert result.pii_count == 1
    assert "[PII_SSN_1]" in result.sanitized_text


# ── SanitizationResult fields ─────────────────────────────────────────────────

def test_sanitization_result_fields():
    s = PIISanitizer(sanitize_types={"SSN"})
    result = s.sanitize("SSN: 123-45-6789")
    assert isinstance(result, SanitizationResult)
    assert isinstance(result.sanitized_text, str)
    assert isinstance(result.mapping, dict)
    assert isinstance(result.pii_count, int)
    assert result.pii_count == len(result.mapping)
