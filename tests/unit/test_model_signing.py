"""Tests for OpenSSF model signing verification."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.control.signing import verify_model_signature


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_ollama_model_unsigned(anyio_backend):
    verified, details = await verify_model_signature("qwen3:4b", "ollama")
    assert verified is False
    assert details["verification_level"] == "unsigned"


@pytest.mark.anyio
async def test_hf_model_no_client(anyio_backend):
    verified, details = await verify_model_signature(
        "meta-llama/Llama-3", "huggingface"
    )
    assert verified is False
    assert details["verification_level"] == "unsigned"
    assert details["reason"] == "no_http_client"


@pytest.mark.anyio
async def test_hf_model_signed(anyio_backend):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "security": {
            "sigstore_verification": True,
            "signer": "meta-llama",
        }
    }
    mock_client.get.return_value = mock_resp
    verified, details = await verify_model_signature(
        "meta-llama/Llama-3", "huggingface", mock_client
    )
    assert verified is True
    assert details["verification_level"] == "signed"
    assert details["signer"] == "meta-llama"


@pytest.mark.anyio
async def test_hf_model_unsigned(anyio_backend):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {}
    mock_client.get.return_value = mock_resp
    verified, details = await verify_model_signature(
        "some/model", "huggingface", mock_client
    )
    assert verified is False
    assert details["verification_level"] == "unsigned"


@pytest.mark.anyio
async def test_hf_api_error(anyio_backend):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_client.get.return_value = mock_resp
    verified, details = await verify_model_signature(
        "missing/model", "huggingface", mock_client
    )
    assert verified is False
    assert details["verification_level"] == "unsigned"
    assert details["reason"] == "hf_api_error:404"


@pytest.mark.anyio
async def test_failopen_on_exception(anyio_backend):
    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("network error")
    verified, details = await verify_model_signature(
        "some/model", "huggingface", mock_client
    )
    assert verified is False
    assert details["verification_level"] == "auto_attested"


@pytest.mark.anyio
async def test_ollama_model_with_client(anyio_backend):
    """Ollama signing check doesn't use the HTTP client (not yet supported)."""
    mock_client = AsyncMock()
    verified, details = await verify_model_signature(
        "llama3:8b", "ollama", mock_client
    )
    assert verified is False
    assert details["reason"] == "ollama_signing_not_yet_supported"
    # HTTP client should not have been called
    mock_client.get.assert_not_called()


@pytest.mark.anyio
async def test_hf_model_partial_security_field(anyio_backend):
    """HF model has security field but no sigstore_verification."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"security": {"other_field": True}}
    mock_client.get.return_value = mock_resp
    verified, details = await verify_model_signature(
        "some/model", "huggingface", mock_client
    )
    assert verified is False
    assert details["verification_level"] == "unsigned"
    assert details["reason"] == "no_sigstore_attestation"
