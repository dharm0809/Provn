"""
title: Walacor Gateway Event Logger
author: Walacor
version: 1.0.0
required_open_webui_version: 0.5.0
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Filter:
    """Unified OpenWebUI filter that intercepts every event (inlet, stream,
    outlet) and sends structured payloads to the Walacor Gateway event
    logging endpoint.

    Install: Upload this single file as a **Global Filter Function** in
    OpenWebUI Admin > Functions. Enable "Global" so it applies to ALL models.
    """

    class Valves(BaseModel):
        """Admin-editable configuration."""

        priority: int = Field(
            default=0,
            description="Filter execution order (lower = first)",
        )
        gateway_url: str = Field(
            default=os.environ.get("WALACOR_GATEWAY_URL", "http://localhost:8000"),
            description="Walacor Gateway base URL",
        )
        gateway_api_key: str = Field(
            default=os.environ.get("WALACOR_GATEWAY_API_KEY", ""),
            description="Gateway API key for authentication",
        )
        enabled: bool = Field(
            default=True,
            description="Enable event logging to gateway",
        )
        log_stream_chunks: bool = Field(
            default=True,
            description="Buffer individual stream chunks and include them in the outlet event",
        )
        max_response_chars: int = Field(
            default=5000,
            description="Max characters of assistant response to include in outlet event",
        )
        max_user_message_chars: int = Field(
            default=2000,
            description="Max characters of user message to include in inlet event",
        )

    def __init__(self):
        self.name = "Walacor Gateway Event Logger"
        self.valves = self.Valves()
        # Thread-safe buffer for stream chunks, keyed by chat_id
        self._stream_buffers: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        # Fallback chat_id when metadata is unavailable in stream()
        self._current_chat_id: str = ""

    # ── inlet (pre-request) ──────────────────────────────────────────────

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __event_emitter__=None,
    ) -> dict:
        """Fires before the request is sent to the LLM.  Logs the full
        request context — user, messages, files, model, settings."""
        if not self.valves.enabled:
            return body

        user_info = __user__ or {}
        metadata = __metadata__ or {}
        body_meta = body.get("metadata") or {}
        chat_id = metadata.get("chat_id") or body_meta.get("chat_id", "")

        # Store for stream() correlation
        self._current_chat_id = chat_id

        # Clear any stale stream buffer for this chat
        with self._lock:
            self._stream_buffers[chat_id] = []

        messages = body.get("messages") or []
        files_meta = body_meta.get("files") or []

        event = {
            "event_type": "inlet",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chat_id": chat_id,
            "session_id": metadata.get("session_id", ""),
            "message_id": metadata.get("message_id", ""),
            "interface": metadata.get("interface", ""),
            "model": body.get("model", ""),
            "user": {
                "id": user_info.get("id", ""),
                "name": user_info.get("name", ""),
                "email": user_info.get("email", ""),
                "role": user_info.get("role", ""),
            },
            "data": {
                "message_count": len(messages),
                "last_user_message": self._extract_last_user_message(messages),
                "has_system_prompt": any(
                    m.get("role") == "system" for m in messages
                ),
                "has_files": bool(files_meta),
                "file_count": len(files_meta),
                "files": [
                    {
                        "name": f.get("filename", f.get("name", "")),
                        "type": f.get("type", ""),
                        "size": f.get("size", 0),
                    }
                    for f in files_meta
                ],
                "stream": body.get("stream", False),
                "temperature": body.get("temperature"),
                "max_tokens": body.get("max_tokens"),
                "all_messages": [
                    {
                        "role": m.get("role", ""),
                        "content": self._extract_text(
                            m.get("content", "")
                        )[:self.valves.max_user_message_chars],
                    }
                    for m in messages
                ],
            },
        }

        self._send_event(event)
        return body

    # ── stream (per-chunk) ───────────────────────────────────────────────

    def stream(self, event: dict) -> dict:
        """Fires for every SSE chunk during streaming.  Buffers content
        deltas in memory — they are flushed to the gateway in outlet()."""
        if not self.valves.enabled or not self.valves.log_stream_chunks:
            return event

        chunk_content = ""
        choices = event.get("choices") or []
        if choices:
            delta = (choices[0] or {}).get("delta") or {}
            chunk_content = delta.get("content") or ""

        if chunk_content:
            with self._lock:
                chat_id = self._current_chat_id
                if chat_id not in self._stream_buffers:
                    self._stream_buffers[chat_id] = []
                self._stream_buffers[chat_id].append(
                    {
                        "content": chunk_content,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

        return event

    # ── outlet (post-response) ───────────────────────────────────────────

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        """Fires after the LLM response is complete.  Logs the full
        response, governance headers, and any buffered stream chunks."""
        if not self.valves.enabled:
            return body

        user_info = __user__ or {}
        messages = body.get("messages") or []
        body_meta = body.get("metadata") or {}
        chat_id = body_meta.get("chat_id") or self._current_chat_id

        # Drain buffered stream chunks
        with self._lock:
            stream_chunks = self._stream_buffers.pop(chat_id, [])

        # Extract last assistant message and its metadata
        last_assistant = ""
        info: dict = {}
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                last_assistant = self._extract_text(msg.get("content") or "")
                info = msg.get("info") or {}
                break

        # Extract governance headers injected by the gateway
        headers = info.get("headers") or {}
        governance = {
            "execution_id": headers.get("x-walacor-execution-id", ""),
            "attestation_id": headers.get("x-walacor-attestation-id", ""),
            "chain_seq": headers.get("x-walacor-chain-seq", ""),
            "policy_result": headers.get("x-walacor-policy-result", ""),
            "content_analysis": headers.get("x-walacor-content-analysis", ""),
            "budget_remaining": headers.get("x-walacor-budget-remaining", ""),
            "budget_percent": headers.get("x-walacor-budget-percent", ""),
            "model_id": headers.get("x-walacor-model-id", ""),
        }

        event = {
            "event_type": "outlet",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chat_id": chat_id,
            "model": body.get("model", ""),
            "user": {
                "id": user_info.get("id", ""),
                "name": user_info.get("name", ""),
                "email": user_info.get("email", ""),
                "role": user_info.get("role", ""),
            },
            "data": {
                "message_count": len(messages),
                "assistant_response": last_assistant[
                    : self.valves.max_response_chars
                ],
                "response_length": len(last_assistant),
                "stream_chunks_count": len(stream_chunks),
                "stream_chunks": stream_chunks
                if self.valves.log_stream_chunks
                else [],
                "governance": governance,
                "all_messages": [
                    {
                        "role": m.get("role", ""),
                        "content": self._extract_text(
                            m.get("content", "")
                        )[:self.valves.max_response_chars],
                    }
                    for m in messages
                ],
            },
        }

        self._send_event(event)
        return body

    # ── helpers ──────────────────────────────────────────────────────────

    def _send_event(self, event: dict) -> None:
        """POST event to the gateway in a daemon thread (non-blocking)."""

        def _post():
            try:
                import requests as _req

                hdrs = {"Content-Type": "application/json"}
                if self.valves.gateway_api_key:
                    hdrs["X-API-Key"] = self.valves.gateway_api_key
                resp = _req.post(
                    f"{self.valves.gateway_url.rstrip('/')}/v1/openwebui/events",
                    json=event,
                    headers=hdrs,
                    timeout=5,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Gateway event log failed: %d %s",
                        resp.status_code,
                        resp.text[:200],
                    )
            except Exception as e:
                logger.warning("Gateway event log error: %s", e)

        threading.Thread(target=_post, daemon=True).start()

    def _extract_last_user_message(self, messages: list[dict]) -> str:
        """Return the text of the last user message, truncated."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return self._extract_text(msg.get("content", ""))[
                    : self.valves.max_user_message_chars
                ]
        return ""

    @staticmethod
    def _extract_text(content: Any) -> str:
        """Extract plain text from message content (string or multimodal array)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return " ".join(parts)
        return str(content) if content else ""
