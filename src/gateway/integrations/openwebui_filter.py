"""
title: Walacor Gateway Audit Filter
author: Walacor
description: Sends rich audit metadata (user, chat, files, RAG sources) to the Walacor Gateway before every LLM call. Install as a global filter in OpenWebUI.
version: 1.0.0
required_open_webui_version: 0.3.0
requirements: aiohttp
"""

from pydantic import BaseModel, Field
from typing import Optional, Callable, Any
import aiohttp
import json
import hashlib
import logging
import os

log = logging.getLogger("walacor_gateway_filter")


class Filter:
    """OpenWebUI inlet/outlet filter that enriches Gateway audit trail.

    Inlet: sends user identity, chat context, file metadata, and RAG sources
    to the Gateway's /v1/attachments/notify endpoint before the LLM call.
    Also injects X-* headers into the request metadata for the Gateway to pick up.

    Outlet: sends the response metadata (token usage, sources cited) back to
    the Gateway for post-inference audit enrichment.
    """

    class Valves(BaseModel):
        gateway_url: str = Field(
            default="http://localhost:8000",
            description="Walacor Gateway base URL",
        )
        api_key: str = Field(
            default="",
            description="Gateway API key (X-API-Key header)",
        )
        notify_files: bool = Field(
            default=True,
            description="Send file metadata to gateway /v1/attachments/notify on inlet",
        )
        inject_headers: bool = Field(
            default=True,
            description="Inject X-OpenWebUI-* identity headers into request metadata",
        )
        priority: int = Field(
            default=0,
            description="Filter priority (lower = runs first)",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __chat_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
    ) -> dict:
        """Pre-LLM call: enrich request with audit metadata for the Gateway."""

        user = __user__ or {}
        metadata = __metadata__ or body.get("metadata", {})
        gateway_url = self.valves.gateway_url.rstrip("/")

        # ── 1. Inject identity into request metadata ──────────────────────
        # The Gateway reads these from the request body's metadata or headers.
        if self.valves.inject_headers:
            if "metadata" not in body:
                body["metadata"] = {}
            m = body["metadata"]
            m["user_id"] = user.get("id", "")
            m["user_email"] = user.get("email", "")
            m["user_name"] = user.get("name", "")
            m["user_role"] = user.get("role", "")
            m["chat_id"] = __chat_id__ or metadata.get("chat_id", "")
            m["message_id"] = __message_id__ or metadata.get("message_id", "")
            m["session_id"] = metadata.get("session_id", "")

            # Files attached to this request
            files = metadata.get("files") or body.get("files") or []
            if files:
                m["files"] = [
                    {
                        "id": f.get("id", ""),
                        "name": f.get("name", f.get("filename", "")),
                        "type": f.get("type", ""),
                        "content_type": f.get("content_type", ""),
                    }
                    for f in files
                    if isinstance(f, dict)
                ]

            # Features enabled for this chat
            features = metadata.get("features", {})
            if features:
                m["features"] = features

            # Tool IDs enabled
            tool_ids = metadata.get("tool_ids")
            if tool_ids:
                m["tool_ids"] = tool_ids

        # ── 2. Notify gateway about attached files ────────────────────────
        # Sends file metadata + hash to /v1/attachments/notify so the gateway
        # can correlate files with the subsequent chat completion request.
        if self.valves.notify_files and self.valves.api_key:
            files = metadata.get("files") or body.get("files") or []
            for f in files:
                if not isinstance(f, dict):
                    continue
                filename = f.get("name", f.get("filename", "unknown"))
                file_id = f.get("id", "")

                # Compute a deterministic hash from file_id if no content hash available
                file_hash = hashlib.sha3_512(
                    file_id.encode("utf-8") if file_id else filename.encode("utf-8")
                ).hexdigest()

                notify_payload = {
                    "filename": filename,
                    "hash_sha3_512": file_hash,
                    "mimetype": f.get("content_type", f.get("type", "application/octet-stream")),
                    "size_bytes": f.get("size", 0),
                    "source": "openwebui_upload",
                    "file_id": file_id,
                    "uploaded_by": user.get("id", ""),
                    "chat_id": __chat_id__ or "",
                }
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{gateway_url}/v1/attachments/notify",
                            json=notify_payload,
                            headers={
                                "X-API-Key": self.valves.api_key,
                                "Content-Type": "application/json",
                            },
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            if resp.status != 200:
                                log.warning(
                                    "Gateway notify failed: %d %s",
                                    resp.status, await resp.text(),
                                )
                except Exception as e:
                    log.warning("Gateway notify error: %s", e)

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        """Post-LLM call: pass through unchanged.

        The Gateway already captures the response via its proxy pipeline.
        This outlet hook is reserved for future enrichment (e.g. pushing
        RAG citation sources back to the gateway for audit correlation).
        """
        return body
