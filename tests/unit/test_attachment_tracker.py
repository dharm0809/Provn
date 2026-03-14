"""Unit tests for attachment notification cache."""

from gateway.middleware.attachment_tracker import AttachmentNotificationCache


def test_store_and_retrieve():
    """Store a notification, retrieve by hash."""
    cache = AttachmentNotificationCache(max_size=100, ttl_seconds=3600)
    meta = {
        "filename": "test.pdf",
        "mimetype": "application/pdf",
        "size_bytes": 1000,
        "hash_sha3_512": "abc123",
        "chat_id": "chat-1",
        "user_id": "user-1",
        "user_email": "user@example.com",
        "upload_timestamp": "2026-03-14T00:00:00Z",
    }
    cache.store(meta)
    result = cache.get("abc123")
    assert result is not None
    assert result["filename"] == "test.pdf"
    assert result["user_id"] == "user-1"


def test_get_missing_returns_none():
    """Missing hash returns None."""
    cache = AttachmentNotificationCache(max_size=100, ttl_seconds=3600)
    assert cache.get("nonexistent") is None


def test_max_size_evicts_oldest():
    """Cache evicts oldest entries when max_size exceeded."""
    cache = AttachmentNotificationCache(max_size=2, ttl_seconds=3600)
    cache.store({"hash_sha3_512": "a", "filename": "1.pdf"})
    cache.store({"hash_sha3_512": "b", "filename": "2.pdf"})
    cache.store({"hash_sha3_512": "c", "filename": "3.pdf"})
    assert cache.get("a") is None  # evicted
    assert cache.get("b") is not None
    assert cache.get("c") is not None


def test_ttl_expiry():
    """Entries expire after TTL."""
    cache = AttachmentNotificationCache(max_size=100, ttl_seconds=0)
    cache.store({"hash_sha3_512": "x", "filename": "old.pdf"})
    # TTL=0 means already expired
    assert cache.get("x") is None


def test_store_requires_hash():
    """Store without hash_sha3_512 is silently skipped."""
    cache = AttachmentNotificationCache(max_size=100, ttl_seconds=3600)
    cache.store({"filename": "no_hash.pdf"})
    assert len(cache._entries) == 0


import base64


def test_extract_images_from_messages():
    """Extract base64 images from OpenAI-format message content blocks."""
    from gateway.middleware.attachment_tracker import extract_images_from_messages

    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        ]},
    ]
    images = extract_images_from_messages(messages)
    assert len(images) == 1
    assert images[0]["index"] == 0
    assert images[0]["mimetype"] == "image/png"
    assert isinstance(images[0]["raw_bytes"], bytes)
    assert len(images[0]["hash_sha3_512"]) == 128


def test_extract_images_no_images():
    """Text-only messages return empty list."""
    from gateway.middleware.attachment_tracker import extract_images_from_messages

    messages = [{"role": "user", "content": "Hello world"}]
    assert extract_images_from_messages(messages) == []


def test_extract_images_url_reference_skipped():
    """URL references (not base64) are logged but not extracted."""
    from gateway.middleware.attachment_tracker import extract_images_from_messages

    messages = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}},
        ]},
    ]
    images = extract_images_from_messages(messages)
    assert len(images) == 0


def test_extract_images_multiple():
    """Multiple images across messages are all extracted."""
    from gateway.middleware.attachment_tracker import extract_images_from_messages

    b64 = base64.b64encode(b"fake_png_data").decode()
    messages = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]},
    ]
    images = extract_images_from_messages(messages)
    assert len(images) == 2
    assert images[0]["mimetype"] == "image/png"
    assert images[1]["mimetype"] == "image/jpeg"


def test_extract_openwebui_file_metadata():
    """Extract file metadata from OpenWebUI's metadata.files field."""
    from gateway.middleware.attachment_tracker import extract_openwebui_files

    body = {
        "model": "qwen3:8b",
        "messages": [{"role": "user", "content": "Summarize this doc"}],
        "metadata": {
            "files": [
                {"id": "f1", "filename": "report.pdf", "type": "application/pdf", "size": 50000},
                {"id": "f2", "filename": "data.csv", "type": "text/csv", "size": 1200},
            ]
        },
    }
    files = extract_openwebui_files(body)
    assert len(files) == 2
    assert files[0]["filename"] == "report.pdf"
    assert files[0]["mimetype"] == "application/pdf"
    assert files[0]["size_bytes"] == 50000
    assert files[0]["source"] == "openwebui_upload"


def test_extract_openwebui_files_no_metadata():
    """Body without metadata.files returns empty list."""
    from gateway.middleware.attachment_tracker import extract_openwebui_files

    body = {"model": "qwen3:8b", "messages": []}
    assert extract_openwebui_files(body) == []


def test_extract_openwebui_files_empty_list():
    """Empty files list returns empty."""
    from gateway.middleware.attachment_tracker import extract_openwebui_files

    body = {"metadata": {"files": []}}
    assert extract_openwebui_files(body) == []


import pytest
import json


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_attachment_notify_endpoint(anyio_backend):
    """POST /v1/attachments/notify stores metadata in cache."""
    from gateway.middleware.attachment_tracker import (
        attachment_notify_handler,
        AttachmentNotificationCache,
    )
    from starlette.requests import Request as StarletteRequest

    cache = AttachmentNotificationCache(max_size=100, ttl_seconds=3600)

    body = {
        "filename": "contract.pdf",
        "mimetype": "application/pdf",
        "size_bytes": 245000,
        "hash_sha3_512": "abc" * 42 + "ab",
        "chat_id": "chat-123",
        "user_id": "user-1",
        "user_email": "user@test.com",
        "upload_timestamp": "2026-03-14T12:00:00Z",
    }

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/attachments/notify",
        "headers": [(b"content-type", b"application/json")],
    }
    request = StarletteRequest(scope, receive=None)
    request._body = json.dumps(body).encode()

    response = await attachment_notify_handler(request, cache)

    assert response.status_code == 200
    stored = cache.get("abc" * 42 + "ab")
    assert stored is not None
    assert stored["filename"] == "contract.pdf"
