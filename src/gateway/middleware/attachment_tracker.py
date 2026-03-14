"""Attachment tracking: notification cache + request body image/file extraction."""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


class AttachmentNotificationCache:
    """Bounded TTL cache for file upload notifications from OpenWebUI webhook.

    Stores metadata keyed by SHA3-512 hash. Entries expire after ttl_seconds.
    Evicts oldest entries when max_size is exceeded.
    """

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 3600):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._entries: OrderedDict[str, tuple[dict, float]] = OrderedDict()

    def store(self, meta: dict[str, Any]) -> None:
        file_hash = meta.get("hash_sha3_512")
        if not file_hash:
            return
        now = time.monotonic()
        self._entries[file_hash] = (meta, now)
        self._entries.move_to_end(file_hash)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def get(self, file_hash: str) -> dict[str, Any] | None:
        entry = self._entries.get(file_hash)
        if entry is None:
            return None
        meta, stored_at = entry
        if time.monotonic() - stored_at > self._ttl:
            del self._entries[file_hash]
            return None
        return meta


def extract_images_from_messages(messages: list[dict]) -> list[dict[str, Any]]:
    """Extract base64-encoded images from OpenAI-format message content blocks.

    Returns list of dicts: {index, mimetype, raw_bytes, hash_sha3_512, size_bytes}.
    URL references (non-base64) are skipped.
    """
    images: list[dict[str, Any]] = []
    idx = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in ("image_url", "image"):
                continue
            url_obj = block.get("image_url") or block
            url = url_obj.get("url", "") if isinstance(url_obj, dict) else ""
            if not url.startswith("data:"):
                logger.debug("Skipping non-base64 image URL: %.60s...", url)
                continue
            try:
                header, b64_data = url.split(",", 1)
                mimetype = header.split(";")[0].replace("data:", "")
                raw_bytes = base64.b64decode(b64_data)
                file_hash = hashlib.sha3_512(raw_bytes).hexdigest()
                images.append({
                    "index": idx,
                    "mimetype": mimetype,
                    "raw_bytes": raw_bytes,
                    "size_bytes": len(raw_bytes),
                    "hash_sha3_512": file_hash,
                })
                idx += 1
            except Exception:
                logger.warning("Failed to decode base64 image at index %d", idx, exc_info=True)
    return images


def extract_openwebui_files(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract file metadata from OpenWebUI's metadata.files field.

    Returns list of dicts: {filename, mimetype, size_bytes, source, file_id}.
    """
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return []
    files_list = metadata.get("files")
    if not isinstance(files_list, list):
        return []
    result = []
    for f in files_list:
        if not isinstance(f, dict):
            continue
        result.append({
            "filename": f.get("filename", f.get("name", "unknown")),
            "mimetype": f.get("type", f.get("mime_type", "application/octet-stream")),
            "size_bytes": f.get("size", 0),
            "source": "openwebui_upload",
            "file_id": f.get("id", ""),
        })
    return result
