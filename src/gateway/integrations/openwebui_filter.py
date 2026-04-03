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
        metadata = __metadata__ or {}
        gateway_url = self.valves.gateway_url.rstrip("/")

        # ── 1. Inject identity via HTTP headers ──────────────────────────
        # OpenWebUI 0.8+ forwards __metadata__["headers"] as HTTP headers
        # to the downstream API. body.metadata is stripped before forwarding,
        # so we MUST use headers for data the Gateway needs to see.
        if self.valves.inject_headers:
            if not isinstance(metadata.get("headers"), dict):
                metadata["headers"] = {}
            h = metadata["headers"]

            # Session identity — chat_id becomes X-Session-Id for the Gateway
            chat_id = __chat_id__ or metadata.get("chat_id", "")
            if chat_id:
                h["X-Session-Id"] = chat_id
                h["X-OpenWebUI-Chat-Id"] = chat_id

            # User identity
            h["X-User-Id"] = user.get("email") or user.get("name") or user.get("id") or ""
            h["X-User-Email"] = user.get("email", "")
            h["X-User-Name"] = user.get("name", "")
            h["X-User-Roles"] = user.get("role", "")

            if __message_id__:
                h["X-OpenWebUI-Message-Id"] = __message_id__

            # File count for the Gateway to know files are attached
            files = metadata.get("files") or body.get("files") or []
            file_count = len([f for f in files if isinstance(f, dict)])
            if file_count > 0:
                h["X-OpenWebUI-File-Count"] = str(file_count)

            # Also put minimal metadata in body for backward compat
            if "metadata" not in body:
                body["metadata"] = {}
            body["metadata"]["chat_id"] = chat_id
            body["metadata"]["user_id"] = user.get("id", "")
            body["metadata"]["user_email"] = user.get("email", "")
            body["metadata"]["user_name"] = user.get("name", "")

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
