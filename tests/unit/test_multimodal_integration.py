"""Integration test: full multimodal audit pipeline."""

import base64
import hashlib
import pytest
from unittest.mock import MagicMock


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_image_extraction_pipeline(anyio_backend):
    """Image extraction and hashing works end-to-end."""
    from gateway.middleware.attachment_tracker import extract_images_from_messages

    # Extract image from messages
    b64_data = base64.b64encode(b"fake_png_data").decode()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "What is in this image?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_data}"}},
    ]}]
    images = extract_images_from_messages(messages)
    assert len(images) == 1
    assert images[0]["mimetype"] == "image/png"
    assert len(images[0]["hash_sha3_512"]) == 128


@pytest.mark.anyio
async def test_notification_correlates_with_request(anyio_backend):
    """Webhook notification matches request image by hash."""
    from gateway.middleware.attachment_tracker import AttachmentNotificationCache, extract_images_from_messages

    # Pre-notify via webhook
    cache = AttachmentNotificationCache(max_size=100, ttl_seconds=3600)
    raw_bytes = b"actual_png_content"
    file_hash = hashlib.sha3_512(raw_bytes).hexdigest()
    cache.store({
        "hash_sha3_512": file_hash,
        "filename": "photo.png",
        "user_id": "user-1",
        "chat_id": "chat-1",
    })

    # Request arrives with same image
    b64_data = base64.b64encode(raw_bytes).decode()
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_data}"}},
    ]}]
    images = extract_images_from_messages(messages)

    # Correlate
    enriched = cache.get(images[0]["hash_sha3_512"])
    assert enriched is not None
    assert enriched["filename"] == "photo.png"
    assert enriched["user_id"] == "user-1"


def test_openwebui_file_metadata_in_record():
    """OpenWebUI file metadata flows into execution record."""
    from gateway.middleware.attachment_tracker import extract_openwebui_files
    from gateway.pipeline.hasher import build_execution_record
    from unittest.mock import MagicMock

    body = {
        "model": "qwen3:8b",
        "messages": [{"role": "user", "content": "Summarize this doc"}],
        "metadata": {
            "files": [
                {"id": "f1", "filename": "report.pdf", "type": "application/pdf", "size": 50000},
            ]
        },
    }
    files = extract_openwebui_files(body)
    assert len(files) == 1

    call = MagicMock()
    call.prompt_text = "Summarize this doc"
    call.model_id = "qwen3:8b"
    call.metadata = {}
    resp = MagicMock()
    resp.content = "Summary here"
    resp.thinking_content = None
    resp.provider_request_id = "req-2"
    resp.model_hash = None
    resp.usage = {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15}

    record = build_execution_record(
        call=call,
        model_response=resp,
        attestation_id="att-1",
        policy_version=1,
        policy_result="pass",
        tenant_id="t1",
        gateway_id="gw-1",
        file_metadata=files,
    )
    assert len(record["file_metadata"]) == 1
    assert record["file_metadata"][0]["filename"] == "report.pdf"
    assert record["file_metadata"][0]["source"] == "openwebui_upload"
