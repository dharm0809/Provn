"""PII Sanitization — strip PII placeholders before LLM call, restore after.

Complements pii_detector.py (which warns/blocks). This module strips
high-risk PII types and provides a restoration mapping so the gateway
can put the originals back in the response, preventing PII from ever
reaching the LLM (HIPAA/GDPR compliance).

Reuses regex patterns from pii_detector.py to stay in sync.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SanitizationResult:
    """Result of a sanitize() call."""

    sanitized_text: str
    mapping: dict[str, str]  # placeholder -> original value
    pii_count: int


# Mirror the patterns from pii_detector._PATTERNS for the types we sanitize.
# Ordered by specificity (same order as pii_detector.py) to avoid overlapping matches.
_SANITIZE_PATTERNS: dict[str, re.Pattern[str]] = {
    # Credit card numbers (Luhn-plausible 13-19 digits, common separators)
    "CREDIT_CARD": re.compile(
        r"\b(?:4[0-9]{3}|5[1-5][0-9]{2}|3[47][0-9]{2}|6(?:011|5[0-9]{2})|3(?:0[0-5]|[68][0-9])[0-9])"
        r"(?:[ \-]?[0-9]{4}){2,3}(?:[ \-]?[0-9]{1,4})?\b"
    ),
    # US SSN: 3-2-4 digits with separators
    "SSN": re.compile(r"\b(?!000|666|9\d{2})\d{3}[-\s](?!00)\d{2}[-\s](?!0000)\d{4}\b"),
    # AWS access key IDs
    "AWS_ACCESS_KEY": re.compile(r"\b(?:AKIA|AGPA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}\b"),
    # Generic API key patterns (Bearer tokens, long credential strings)
    "API_KEY": re.compile(
        r"\b(?:api[_\-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*['\"]?[A-Za-z0-9+/\-_]{20,}['\"]?",
        re.IGNORECASE,
    ),
}

# Types to sanitize by default — mirrors pii_detector._BLOCK_PII_TYPES (high-risk only)
_DEFAULT_SANITIZE_TYPES: frozenset[str] = frozenset({"SSN", "CREDIT_CARD", "AWS_ACCESS_KEY", "API_KEY"})


class PIISanitizer:
    """Replace PII with [PII_TYPE_N] placeholder tokens; track mapping for restoration.

    Usage::

        sanitizer = PIISanitizer()
        result = sanitizer.sanitize("My SSN is 123-45-6789")
        # result.sanitized_text == "My SSN is [PII_SSN_1]"
        # result.mapping == {"[PII_SSN_1]": "123-45-6789"}

        restored = sanitizer.restore("Your SSN is [PII_SSN_1]", result.mapping)
        # restored == "Your SSN is 123-45-6789"
    """

    def __init__(self, sanitize_types: set[str] | None = None) -> None:
        types = sanitize_types if sanitize_types is not None else _DEFAULT_SANITIZE_TYPES
        self._patterns: list[tuple[str, re.Pattern[str]]] = [
            (pii_type, pattern)
            for pii_type, pattern in _SANITIZE_PATTERNS.items()
            if pii_type in types
        ]

    def sanitize(self, text: str) -> SanitizationResult:
        """Replace PII in *text* with ``[PII_TYPE_N]`` placeholders.

        Each distinct match gets an incrementing index per PII type (1-based).
        The mapping dict maps placeholder → original value for later restoration.
        """
        mapping: dict[str, str] = {}
        counter: dict[str, int] = {}
        sanitized = text

        for pii_type, pattern in self._patterns:
            # We iterate over a fresh finditer on the *current* sanitized string
            # so that prior replacements don't confuse offsets.
            new_sanitized = sanitized
            offset = 0
            for match in pattern.finditer(sanitized):
                count = counter.get(pii_type, 0) + 1
                counter[pii_type] = count
                placeholder = f"[PII_{pii_type}_{count}]"
                original = match.group()
                mapping[placeholder] = original
                # Replace the first occurrence of the exact matched string after offset
                # We use str.replace with count=1 since there may be identical values.
                start = match.start() + offset
                end = match.end() + offset
                new_sanitized = new_sanitized[:start] + placeholder + new_sanitized[end:]
                offset += len(placeholder) - len(original)
            sanitized = new_sanitized

        return SanitizationResult(
            sanitized_text=sanitized,
            mapping=mapping,
            pii_count=len(mapping),
        )

    def restore(self, text: str, mapping: dict[str, str]) -> str:
        """Replace ``[PII_TYPE_N]`` placeholders back with their original values.

        Placeholders that do not appear in *text* are silently ignored (the LLM
        may not have echoed every placeholder).
        """
        restored = text
        for placeholder, original in mapping.items():
            restored = restored.replace(placeholder, original)
        return restored


_default_sanitizer: PIISanitizer | None = None


def get_default_sanitizer() -> PIISanitizer:
    """Return (or lazily create) the module-level default PIISanitizer instance."""
    global _default_sanitizer
    if _default_sanitizer is None:
        _default_sanitizer = PIISanitizer()
    return _default_sanitizer
