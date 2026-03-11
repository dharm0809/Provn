"""Session ID resolution from HTTP request headers.

Checks a prioritized list of header names and returns the first non-empty value.
Falls back to a fresh UUID if none match. This allows a single Gateway to serve
multiple UI clients (OpenWebUI, LibreChat, LobeChat, custom) that each use
different session header names.
"""
from __future__ import annotations

import uuid


def resolve_session_id(request, header_names: list[str]) -> str:
    """Return the first non-empty value from the given header names, or a new UUID.

    Args:
        request: Any object with a ``headers`` dict-like attribute (lowercased keys).
        header_names: Ordered list of header names to check (case-insensitive).

    Returns:
        Session ID string — always non-empty.
    """
    for name in header_names:
        value = request.headers.get(name.lower(), "").strip()
        if value:
            return value
    return str(uuid.uuid4())
