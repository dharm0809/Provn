"""Classify exceptions into short, safe reason strings for operator surfaces.

Full ``str(exc)`` values frequently embed internal hostnames, URLs, user
prompt fragments (via JSONDecodeError text windows), or dict keys (via
KeyError). We never want those leaking into ring buffers readable by any
API-key holder through /v1/connections. Server logs still receive full
detail via ``logger.warning(..., exc_info=True)``.
"""
from __future__ import annotations

_SAFE_REASONS = {
    "TimeoutException":     "timeout",
    "ReadTimeout":          "timeout",
    "WriteTimeout":         "timeout",
    "ConnectTimeout":       "timeout",
    "ConnectError":         "connection_refused",
    "RemoteProtocolError":  "protocol_error",
    "NetworkError":         "network_error",
    "TransportError":       "transport_error",
    "HTTPStatusError":      "http_error",
    "JSONDecodeError":      "decode_error",
    "ValueError":           "validation_error",
    "KeyError":             "validation_error",
    "NotImplementedError":  "not_implemented",
    "CancelledError":       "cancelled",
}


def classify_exception(exc: BaseException) -> str:
    """Return a short, operator-safe classification — no user content, no hostnames."""
    name = type(exc).__name__
    return _SAFE_REASONS.get(name, name)
