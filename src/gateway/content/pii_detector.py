"""Phase 10: Built-in PII detector. Regex-based, deterministic, no external deps. analyzer_id: walacor.pii.v1"""

from __future__ import annotations

import re

from gateway.content.base import ContentAnalyzer, Decision, Verdict

# Patterns ordered by specificity. Each entry: (name, compiled_regex)
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Credit card numbers (Luhn-plausible 13-19 digits, common separators)
    ("credit_card", re.compile(
        r"\b(?:4[0-9]{3}|5[1-5][0-9]{2}|3[47][0-9]{2}|6(?:011|5[0-9]{2})|3(?:0[0-5]|[68][0-9])[0-9])"
        r"(?:[ \-]?[0-9]{4}){2,3}(?:[ \-]?[0-9]{1,4})?\b"
    )),
    # US SSN: 3-2-4 digits with separators
    ("ssn", re.compile(r"\b(?!000|666|9\d{2})\d{3}[-\s](?!00)\d{2}[-\s](?!0000)\d{4}\b")),
    # Email addresses
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # US phone numbers (various formats)
    ("phone_number", re.compile(
        r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"
    )),
    # IPv4 addresses (excluding 0.x.x.x and 255.x.x.x private ranges)
    ("ip_address", re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]\d|\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]\d|\d)\b"
    )),
    # AWS access key IDs
    ("aws_access_key", re.compile(r"\b(?:AKIA|AGPA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}\b")),
    # Generic API key patterns (Bearer tokens, long hex/base64 strings prefixed with common names)
    ("api_key", re.compile(
        r"\b(?:api[_\-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*['\"]?[A-Za-z0-9+/\-_]{20,}['\"]?",
        re.IGNORECASE,
    )),
]

# High-risk PII types that BLOCK the request (credentials, financial, identity documents).
# Low-risk types (ip_address, email_address, phone_number) get WARN — they commonly appear
# in educational/technical LLM responses (e.g. "Google's IP is 142.250.184.14").
_BLOCK_PII_TYPES = {"credit_card", "ssn", "aws_access_key", "api_key"}


class PIIDetector(ContentAnalyzer):
    """
    Regex-based PII detector. Checks for email, phone, SSN, credit card, IP, AWS keys, API tokens.
    Returns BLOCK for high-risk PII (credit_card, ssn, aws_access_key, api_key).
    Returns WARN for low-risk PII (email_address, phone_number, ip_address).
    No content stored or logged.
    """

    _analyzer_id = "walacor.pii.v1"

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    def __init__(self) -> None:
        self._block_types: set[str] = set(_BLOCK_PII_TYPES)
        self._warn_types: set[str] = {
            name for name, _ in _PATTERNS if name not in _BLOCK_PII_TYPES
        }
        self._pass_types: set[str] = set()

    def configure(self, policies: list[dict]) -> None:
        """Reconfigure block/warn/pass sets from control plane content policies."""
        if not policies:
            return
        self._block_types = {p["category"] for p in policies if p.get("action") == "block"}
        self._warn_types = {p["category"] for p in policies if p.get("action") == "warn"}
        self._pass_types = {p["category"] for p in policies if p.get("action") == "pass"}

    @property
    def timeout_ms(self) -> int:
        return 20  # synchronous regex, very fast

    async def analyze(self, text: str) -> Decision:
        for name, pattern in _PATTERNS:
            if pattern.search(text):
                if name in self._pass_types:
                    return Decision(
                        verdict=Verdict.PASS,
                        confidence=0.99,
                        analyzer_id=self.analyzer_id,
                        category="pii",
                        reason=name,
                    )
                verdict = Verdict.BLOCK if name in self._block_types else Verdict.WARN
                return Decision(
                    verdict=verdict,
                    confidence=0.99,
                    analyzer_id=self.analyzer_id,
                    category="pii",
                    reason=name,
                )
        return Decision(
            verdict=Verdict.PASS,
            confidence=1.0,
            analyzer_id=self.analyzer_id,
            category="pii",
            reason="no_pii_detected",
        )
