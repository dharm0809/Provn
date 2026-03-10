# tests/unit/test_models_api.py
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_models_endpoint_returns_openai_format():
    """GET /v1/models returns OpenAI-compatible model list."""
    from gateway.models_api import list_models
    from starlette.requests import Request

    mock_store = MagicMock()
    mock_store.list_attestations.return_value = [
        {"model_id": "qwen3:4b", "provider": "ollama", "status": "active"},
        {"model_id": "gpt-4o", "provider": "openai", "status": "active"},
    ]

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.control_store = mock_store
        mock_ctx.return_value.skip_governance = False

        scope = {"type": "http", "method": "GET", "path": "/v1/models", "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)
        data = response.body  # JSONResponse

    import json
    body = json.loads(data)
    assert body["object"] == "list"
    assert len(body["data"]) == 2
    assert body["data"][0]["id"] == "qwen3:4b"
    assert body["data"][0]["object"] == "model"
    assert body["data"][0]["owned_by"] == "ollama"


@pytest.mark.anyio
async def test_models_endpoint_no_control_store_uses_discovery():
    """When no control store, returns empty list (no crash)."""
    from gateway.models_api import list_models
    from starlette.requests import Request

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.control_store = None
        mock_ctx.return_value.skip_governance = True

        scope = {"type": "http", "method": "GET", "path": "/v1/models", "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)

    import json
    body = json.loads(response.body)
    assert body["object"] == "list"
    assert body["data"] == []


@pytest.mark.anyio
async def test_models_excludes_revoked():
    """Revoked attestations are excluded from model list."""
    from gateway.models_api import list_models
    from starlette.requests import Request

    mock_store = MagicMock()
    mock_store.list_attestations.return_value = [
        {"model_id": "qwen3:4b", "provider": "ollama", "status": "active"},
        {"model_id": "bad-model", "provider": "ollama", "status": "revoked"},
    ]

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx:
        mock_ctx.return_value.control_store = mock_store
        mock_ctx.return_value.skip_governance = False

        scope = {"type": "http", "method": "GET", "path": "/v1/models", "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)

    import json
    body = json.loads(response.body)
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == "qwen3:4b"
