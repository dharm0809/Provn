"""Mid-stream S4 (child safety) abort via compiled regex.

This module provides a fast, sub-millisecond check for S4 child-safety
patterns in accumulated streaming text.  It is intentionally keyword-based
(no ML model invocation) to keep latency negligible during SSE streaming.
"""

from __future__ import annotations

import re

# Curated S4 child-safety patterns.  These target explicit child exploitation
# terminology while avoiding common programming / parenting vocabulary.
_S4_PATTERNS = re.compile(
    r"|".join([
        r"\bchild\s+exploitation\s+material\b",
        r"\bcsam\b",
        r"\bchild\s+sexual\s+abuse\b",
        r"\bminor\s+exploitation\b",
        r"\bchild\s+pornograph\w*\b",
        r"\bpedophil\w*\s+content\b",
        r"\bunderage\s+sexual\b",
    ]),
    re.IGNORECASE,
)


def check_stream_safety(text: str) -> bool:
    """Return True if accumulated text triggers an S4 safety abort.

    Fast compiled-regex check — no ML model invocation.
    """
    return bool(_S4_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# High-risk PII patterns for windowed streaming detection
# ---------------------------------------------------------------------------
_PII_PATTERNS = re.compile(
    r"|".join([
        r"\b\d{3}-\d{2}-\d{4}\b",          # SSN
        r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",  # Credit card
        r"\bAKIA[0-9A-Z]{16}\b",            # AWS access key
        r"\b(?:sk|pk)[-_](?:live|test)[-_][a-zA-Z0-9]{24,}\b",  # Stripe/API keys
    ]),
    re.IGNORECASE,
)

_PII_CHECK_INTERVAL = 500  # chars


def check_stream_pii(text: str, last_checked_len: int) -> tuple[bool, int]:
    """Check for high-risk PII in text since last check position.

    Returns (pii_found, new_checked_len).
    Only runs when text has grown by _PII_CHECK_INTERVAL since last check.
    """
    if len(text) - last_checked_len < _PII_CHECK_INTERVAL:
        return False, last_checked_len
    # Check only the new portion plus overlap for boundary patterns
    start = max(0, last_checked_len - 50)
    found = bool(_PII_PATTERNS.search(text[start:]))
    return found, len(text)
