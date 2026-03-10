"""Error classification and fallback endpoint selection."""

from __future__ import annotations

import random
from fnmatch import fnmatch

from gateway.routing.balancer import Endpoint, LoadBalancer

_CONTEXT_OVERFLOW_PHRASES = [
    "maximum context length",
    "context window",
    "token limit",
    "too many tokens",
    "context_length_exceeded",
]

_CONTENT_POLICY_PHRASES = [
    "content policy",
    "content_filter",
    "content management policy",
    "safety system",
    "flagged as inappropriate",
]


def classify_error(status_code: int, body: str) -> str:
    """Classify error into category."""
    body_lower = body.lower()

    if status_code == 429:
        return "rate_limited"

    if any(phrase in body_lower for phrase in _CONTEXT_OVERFLOW_PHRASES):
        return "context_overflow"

    if any(phrase in body_lower for phrase in _CONTENT_POLICY_PHRASES):
        return "content_policy"

    if status_code >= 500:
        return "server_error"

    return "other"


def select_fallback(
    error_class: str,
    model_id: str,
    balancer: LoadBalancer,
    exclude_url: str = "",
) -> Endpoint | None:
    """Select fallback endpoint based on error class.

    content_policy and other → None (don't retry)
    context_overflow, rate_limited, server_error → next healthy endpoint excluding the failed one
    """
    if error_class in ("content_policy", "other"):
        return None

    for group in balancer._groups:
        if not fnmatch(model_id.lower(), group.pattern.lower()):
            continue
        candidates = [
            ep for ep in group.endpoints
            if ep.healthy and ep.url != exclude_url
        ]
        if not candidates:
            return None
        weights = [ep.weight for ep in candidates]
        return random.choices(candidates, weights=weights, k=1)[0]

    return None
