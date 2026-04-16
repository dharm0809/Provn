"""Session ID resolution from HTTP request headers.

Checks a prioritized list of header names and returns the first non-empty value.
Falls back to a fresh UUID if none match. This allows a single Gateway to serve
multiple UI clients (OpenWebUI, LibreChat, LobeChat, custom) that each use
different session header names.
"""
from __future__ import annotations

import uuid


def resolve_session_id(request, header_names: list[str], body: dict | None = None) -> str:
    """Return the first non-empty value from the given header names, or a new UUID.

    Checks HTTP headers first, then falls back to ``body.metadata.chat_id``
    (injected by the OpenWebUI filter plugin).  This ensures multi-turn
    conversations share a single session ID even when the UI client does
    not set a session header.

    Args:
        request: Any object with a ``headers`` dict-like attribute (lowercased keys).
        header_names: Ordered list of header names to check (case-insensitive).
        body: Parsed request body dict (optional). Checked for ``metadata.chat_id``.

    Returns:
        Session ID string — always non-empty.
    """
    for name in header_names:
        value = request.headers.get(name.lower(), "").strip()
        if value:
            return value
    # Fallback: chat_id from body metadata (OpenWebUI filter plugin)
    if body and isinstance(body.get("metadata"), dict):
        chat_id = body["metadata"].get("chat_id", "").strip()
        if chat_id:
            return chat_id
    return str(uuid.uuid4())
