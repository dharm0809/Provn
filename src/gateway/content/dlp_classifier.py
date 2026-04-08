"""B.8: DLP (Data Loss Prevention) classifier.

Extends PII detection with additional sensitive data categories:
  - FINANCIAL: bank account/routing numbers, SWIFT codes, IBAN
  - HEALTH:    ICD-10 codes, drug names with dosages, NHS/MRN numbers
  - SECRETS:   RSA/EC private keys, connection strings, JWT tokens
  - INFRA:     database URLs, AWS ARNs, internal hostnames

Action per category:
  HEALTH       → BLOCK  (HIPAA)
  SECRETS      → BLOCK  (security)
  FINANCIAL    → WARN
  INFRASTRUCTURE → WARN

Implements ContentAnalyzer (base.py) interface.
"""
from __future__ import annotations

import re
import logging

from gateway.content.base import ContentAnalyzer, Decision, Verdict

logger = logging.getLogger(__name__)

# ── Patterns ────────────────────────────────────────────────────────────────

_FINANCIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # US bank account numbers: 8-17 digits (standalone, not adjacent to more digits)
    ("bank_account", re.compile(r"(?<!\d)\d{8,17}(?!\d)")),
    # ABA routing numbers: 9 digits starting with 0-3 (bank routing)
    ("routing_number", re.compile(r"\b0[0-9]{8}\b")),
    # SWIFT/BIC codes: 8 or 11 character bank identifier codes
    ("swift_code", re.compile(r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b")),
    # IBAN: country code + 2 check digits + BBAN
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7,}(?:[A-Z0-9]{0,3})?\b")),
]

_HEALTH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # ICD-10 codes: letter + 2 digits, optional decimal + 1-4 chars (e.g. E11.9, J45, A01.00)
    ("icd10_code", re.compile(r"\b[A-Z]\d{2}(?:\.\d{1,4})?\b")),
    # Medical Record Numbers
    ("mrn", re.compile(r"\bMRN[\s:]*\d{6,10}\b", re.IGNORECASE)),
    # NHS numbers: 3-3-4 digit pattern — exclude common US phone prefixes (555, 800, 888, etc.)
    ("nhs_number", re.compile(r"\b(?!(?:555|800|888|877|866|900)\b)\d{3}[ \-]\d{3}[ \-]\d{4}\b")),
    # Drug dosages: number + unit (mg, mcg, ml, IU)
    ("drug_dosage", re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|ml|IU)\b", re.IGNORECASE)),
]

_SECRETS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # RSA private key PEM header
    ("rsa_private_key", re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")),
    # Generic PKCS#8 private key PEM header
    ("private_key_pem", re.compile(r"-----BEGIN PRIVATE KEY-----")),
    # Database connection strings (MongoDB, PostgreSQL, MySQL, Redis, MSSQL)
    ("connection_string", re.compile(
        r"(?:mongodb|postgresql|postgres|mysql|redis|mssql)://[^\s\"']{10,}",
        re.IGNORECASE,
    )),
    # JWT tokens: three base64url segments separated by dots
    ("jwt_token", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
    )),
]

_INFRA_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # AWS ARNs
    ("aws_arn", re.compile(r"\barn:aws:[a-z0-9\-]+:[a-z0-9\-]*:\d{12}:[^\s]+")),
    # JDBC/ODBC URLs
    ("db_url", re.compile(r"(?:jdbc|odbc):[a-z]+://[^\s\"']{5,}", re.IGNORECASE)),
    # Internal hostnames (.internal, .local, .corp, .intranet suffixes)
    ("internal_hostname", re.compile(
        r"\b(?:[a-z0-9\-]+\.){2,}(?:internal|local|corp|intranet)\b",
        re.IGNORECASE,
    )),
]

# Map category name → ordered list of (pattern_name, compiled_regex)
_CATEGORY_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "financial": _FINANCIAL_PATTERNS,
    "health": _HEALTH_PATTERNS,
    "secrets": _SECRETS_PATTERNS,
    "infrastructure": _INFRA_PATTERNS,
}

# Default action per category (overridable via configure())
_DEFAULT_ACTIONS: dict[str, str] = {
    "financial": "warn",
    "health": "block",
    "secrets": "block",
    "infrastructure": "warn",
}


class DLPClassifier(ContentAnalyzer):
    """Scans text for sensitive data beyond basic PII.

    Categories: financial, health, secrets, infrastructure.
    HEALTH and SECRETS → BLOCK. FINANCIAL and INFRASTRUCTURE → WARN.
    """

    _analyzer_id = "walacor.dlp.v1"

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    @property
    def timeout_ms(self) -> int:
        return 20  # synchronous regex, very fast

    def __init__(self, enabled_categories: set[str] | None = None) -> None:
        if enabled_categories is None:
            enabled_categories = set(_CATEGORY_PATTERNS.keys())
        # Only keep patterns for enabled categories
        self._enabled: dict[str, list[tuple[str, re.Pattern[str]]]] = {
            cat: patterns
            for cat, patterns in _CATEGORY_PATTERNS.items()
            if cat in enabled_categories
        }
        # Per-category action mapping (mutable for configure())
        self._actions: dict[str, str] = {
            cat: _DEFAULT_ACTIONS.get(cat, "warn")
            for cat in self._enabled
        }

    def configure(self, policies: list[dict]) -> None:
        """Reconfigure per-category actions from control plane content policies."""
        if not policies:
            return
        for policy in policies:
            cat = policy.get("category", "")
            action = policy.get("action", "")
            if cat in self._actions and action in {"block", "warn", "pass"}:
                self._actions[cat] = action

    async def analyze(self, text: str) -> Decision:
        if not text:
            return Decision(
                verdict=Verdict.PASS,
                confidence=1.0,
                analyzer_id=self.analyzer_id,
                category="dlp",
                reason="no_dlp_detected",
            )

        # Scan all enabled categories; return on first BLOCK finding
        first_warn: tuple[str, str] | None = None  # (category, pattern_name)

        for category, patterns in self._enabled.items():
            action = self._actions.get(category, "warn")
            if action == "pass":
                continue
            for name, pattern in patterns:
                if pattern.search(text):
                    if action == "block":
                        return Decision(
                            verdict=Verdict.BLOCK,
                            confidence=0.99,
                            analyzer_id=self.analyzer_id,
                            category=f"dlp.{category}",
                            reason=name,
                        )
                    if action == "warn" and first_warn is None:
                        first_warn = (category, name)

        if first_warn is not None:
            category, name = first_warn
            return Decision(
                verdict=Verdict.WARN,
                confidence=0.99,
                analyzer_id=self.analyzer_id,
                category=f"dlp.{category}",
                reason=name,
            )

        return Decision(
            verdict=Verdict.PASS,
            confidence=1.0,
            analyzer_id=self.analyzer_id,
            category="dlp",
            reason="no_dlp_detected",
        )
