"""
Walacor Audit Classifier — OpenWebUI Pipeline Plugin.

Install: Upload this file as a Filter Function in OpenWebUI Admin > Functions.

Separates the actual user question from conversation noise (system prompts,
RAG context, history, suggestions) and sends structured audit metadata to
the Gateway. The Gateway stores this alongside raw messages so the lineage
dashboard shows a clean Question → Answer view instead of a 50-message dump.

Works WITH the governance_pipeline.py filter (runs after it via higher priority).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from pydantic import BaseModel, Field


# ── RAG detection patterns ────────────────────────────────────────────────────

_RAG_PATTERNS = [
    r"\[context\]",
    r"\[source[s]?\]",
    r"\[document[s]?\]",
    r"\[retrieved\]",
    r"<context>",
    r"<document>",
    r"here (?:is|are) the (?:relevant |retrieved )?(?:context|document|passage|chunk)",
    r"based on the (?:following|provided) (?:context|document|information)",
    r"use the following (?:context|pieces of context|information)",
    r"---\s*\n.*?\n\s*---",  # markdown-style injected blocks
]
_RAG_RE = re.compile("|".join(_RAG_PATTERNS), re.IGNORECASE)

# ── Suggestion detection patterns ─────────────────────────────────────────────

_SUGGESTION_PATTERNS = [
    r"suggest(?:ed)?\s+(?:follow[- ]?up|question|topic)",
    r"you (?:might|could|may) (?:also )?(?:ask|want to know|be interested)",
    r"related questions?:",
    r"try asking:",
]
_SUGGESTION_RE = re.compile("|".join(_SUGGESTION_PATTERNS), re.IGNORECASE)


def _extract_text(content: Any) -> str:
    """Extract plain text from message content (handles string and multimodal array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
        return " ".join(parts)
    return str(content) if content else ""


def _classify_message(msg: dict) -> str:
    """Classify a single message into a category.

    Returns one of:
      user_question, user_followup, assistant_response,
      system_prompt, rag_context, suggestion, tool_result, unknown
    """
    role = msg.get("role", "")
    text = _extract_text(msg.get("content", ""))

    if role == "system":
        if _RAG_RE.search(text):
            return "rag_context"
        return "system_prompt"

    if role == "tool":
        return "tool_result"

    if role == "assistant":
        if _SUGGESTION_RE.search(text):
            return "suggestion"
        return "assistant_response"

    if role == "user":
        return "user_question"

    return "unknown"


def _content_fingerprint(text: str) -> str:
    """Short hash of content for dedup/correlation without storing full text."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


class Pipeline:
    """OpenWebUI Filter: Walacor Audit Content Classifier."""

    class Valves(BaseModel):
        """Pipeline configuration (editable in OpenWebUI admin)."""
        priority: int = Field(
            default=5,
            description="Filter execution order (higher = runs after governance filter at 0)",
        )
        max_question_length: int = Field(
            default=2000,
            description="Max chars of user question to store in audit metadata",
        )
        max_context_preview: int = Field(
            default=500,
            description="Max chars of system context preview",
        )
        classify_all_messages: bool = Field(
            default=True,
            description="Classify every message (for full audit breakdown)",
        )

    def __init__(self):
        self.name = "Walacor Audit Classifier"
        self.valves = self.Valves()

    async def inlet(
        self,
        body: dict,
        __user__: dict | None = None,
        __metadata__: dict | None = None,
        __task__: str | None = None,
    ) -> dict:
        """Classify messages and inject structured audit metadata."""

        # Skip internal tasks (title generation, tag generation, etc.)
        if __task__ and __task__ != "user_response":
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        # ── Classify each message ─────────────────────────────────────
        classifications = []
        for i, msg in enumerate(messages):
            cat = _classify_message(msg)
            classifications.append({
                "index": i,
                "role": msg.get("role", ""),
                "category": cat,
            })

        # ── Extract the ACTUAL user question (last user message) ──────
        user_msgs = [
            (i, msg) for i, msg in enumerate(messages)
            if msg.get("role") == "user"
        ]
        actual_question = ""
        actual_question_index = -1
        if user_msgs:
            actual_question_index, last_user = user_msgs[-1]
            actual_question = _extract_text(last_user.get("content", ""))
            # Mark the last user message specifically
            classifications[actual_question_index]["category"] = "user_question"
            # Mark earlier user messages as follow-ups (history)
            for idx, _ in user_msgs[:-1]:
                classifications[idx]["category"] = "user_followup"

        # ── Extract system context preview ────────────────────────────
        system_msgs = [msg for msg in messages if msg.get("role") == "system"]
        system_preview = ""
        if system_msgs:
            system_preview = _extract_text(system_msgs[0].get("content", ""))

        # ── Detect RAG context ────────────────────────────────────────
        rag_detected = False
        rag_sources = 0
        for msg in messages:
            text = _extract_text(msg.get("content", ""))
            if _RAG_RE.search(text):
                rag_detected = True
                rag_sources += 1

        # ── Detect files/attachments ──────────────────────────────────
        meta = body.get("metadata", {}) or {}
        files = meta.get("files", [])
        has_images = any(
            isinstance(msg.get("content"), list) and
            any(b.get("type") in ("image_url", "image") for b in msg["content"] if isinstance(b, dict))
            for msg in messages
        )

        # ── Count conversation turns ──────────────────────────────────
        user_count = sum(1 for c in classifications if c["category"] in ("user_question", "user_followup"))
        assistant_count = sum(1 for c in classifications if c["category"] == "assistant_response")
        history_turns = max(0, user_count - 1)  # exclude current question

        # ── Noise calculation ─────────────────────────────────────────
        signal_count = 1  # the actual question
        if assistant_count > 0:
            signal_count += 1  # the response (not yet, but will be)
        noise_count = len(messages) - signal_count

        # ── Build structured audit metadata ───────────────────────────
        audit = {
            # Primary: the actual question (what the user really asked)
            "user_question": actual_question[:self.valves.max_question_length],
            "question_fingerprint": _content_fingerprint(actual_question) if actual_question else None,

            # Context summary
            "system_context_preview": system_preview[:self.valves.max_context_preview],
            "conversation_turns": history_turns,
            "total_messages": len(messages),
            "noise_messages": noise_count,

            # Content flags
            "has_rag_context": rag_detected,
            "rag_source_count": rag_sources,
            "has_files": len(files) > 0,
            "file_count": len(files),
            "has_images": has_images,

            # Classification source
            "classified_by": "openwebui_plugin",
            "classifier_version": "1.0",
        }

        # Optional: full message-by-message classification
        if self.valves.classify_all_messages:
            audit["message_classifications"] = classifications

        # ── Inject into request metadata ──────────────────────────────
        if "metadata" not in body:
            body["metadata"] = {}
        body["metadata"]["walacor_audit"] = audit

        return body

    async def outlet(
        self,
        body: dict,
        __user__: dict | None = None,
        __task__: str | None = None,
    ) -> dict:
        """Post-response: no modifications needed (gateway handles audit)."""
        return body
