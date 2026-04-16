"""Gateway-side audit classifier — fallback for non-OpenWebUI clients.

Extracts structured audit metadata from the messages array when the
OpenWebUI pipeline plugin hasn't already classified the request.

Called from the orchestrator after request parsing. If body.metadata
already has walacor_audit (from the plugin), this is a no-op.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_RAG_PATTERNS = re.compile(
    r"\[context\]|\[source\]|\[document\]|<context>|<document>"
    r"|use the following context|based on the provided context"
    r"|based on the following"
    r"|here are the relevant|here is the relevant"
    r"|according to the provided|given the following"
    r"|reference material|internal documentation"
    r"|retrieved documents|search results"
    r"|knowledge base|context window"
    r"|\bRAG\b|retrieved context",
    re.IGNORECASE,
)


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content) if content else ""


def classify_request(body: dict) -> dict:
    """Extract structured audit metadata from request body.

    Returns a walacor_audit dict. If the OpenWebUI plugin already
    classified the request (body.metadata.walacor_audit exists),
    returns it as-is.
    """
    meta = body.get("metadata") or {}
    existing = meta.get("walacor_audit")
    if existing and existing.get("classified_by") == "openwebui_plugin":
        return existing

    messages = body.get("messages", [])
    if not messages:
        return {"classified_by": "gateway_fallback", "total_messages": 0}

    # Last user message = actual question
    user_msgs = [(i, m) for i, m in enumerate(messages) if m.get("role") == "user"]
    actual_question = ""
    if user_msgs:
        actual_question = _extract_text(user_msgs[-1][1].get("content", ""))

    # System context
    system_msgs = [m for m in messages if m.get("role") == "system"]
    system_preview = _extract_text(system_msgs[0].get("content", ""))[:500] if system_msgs else ""

    # RAG detection
    rag_detected = any(
        _RAG_PATTERNS.search(_extract_text(m.get("content", "")))
        for m in messages
    )

    # Image detection
    has_images = any(
        isinstance(m.get("content"), list) and
        any(isinstance(b, dict) and b.get("type") in ("image_url", "image")
            for b in m["content"])
        for m in messages
    )

    # Conversation turns (exclude current question)
    history_turns = max(0, len(user_msgs) - 1)

    # Files
    files = meta.get("files", [])

    return {
        "user_question": actual_question[:2000],
        "question_fingerprint": (
            hashlib.sha256(actual_question.encode("utf-8", errors="replace")).hexdigest()[:16]
            if actual_question else None
        ),
        "system_context_preview": system_preview,
        "conversation_turns": history_turns,
        "total_messages": len(messages),
        "noise_messages": max(0, len(messages) - 1),
        "has_rag_context": rag_detected,
        "has_files": len(files) > 0,
        "file_count": len(files),
        "has_images": has_images,
        "classified_by": "gateway_fallback",
        "classifier_version": "1.0",
    }
