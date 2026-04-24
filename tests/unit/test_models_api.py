# tests/unit/test_models_api.py
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_models_endpoint_returns_openai_format():
    """GET /v1/models returns OpenAI-compatible model list."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

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
    """When no control store, discovers from providers (not empty list)."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings") as mock_settings:

        mock_ctx.return_value.control_store = None
        mock_settings.return_value.strict_model_allowlist = False
        mock_ctx.return_value.http_client = MagicMock()
        mock_discover.return_value = []  # no providers configured — empty is valid

        scope = {"type": "http", "method": "GET", "path": "/v1/models", "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)

    import json
    body = json.loads(response.body)
    assert body["object"] == "list"
    assert body["data"] == []  # empty because no providers — not a crash


@pytest.mark.anyio
async def test_models_excludes_revoked():
    """Revoked attestations are excluded from model list."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

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


@pytest.mark.anyio
async def test_models_falls_back_to_discovery_when_no_control_store():
    """When control_store is None, discovers models from providers."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    mock_http = MagicMock()

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings") as mock_settings:

        mock_ctx.return_value.control_store = None
        mock_settings.return_value.strict_model_allowlist = False
        mock_ctx.return_value.http_client = mock_http
        mock_discover.return_value = [
            {"model_id": "qwen3:4b", "provider": "ollama"},
            {"model_id": "gemma3:1b", "provider": "ollama"},
        ]

        scope = {"type": "http", "method": "GET", "path": "/v1/models",
                 "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)

    import json
    body = json.loads(response.body)
    assert body["object"] == "list"
    assert len(body["data"]) == 2
    assert body["data"][0]["id"] == "qwen3:4b"
    assert body["data"][0]["owned_by"] == "ollama"


@pytest.mark.anyio
async def test_models_falls_back_to_discovery_when_store_has_no_active():
    """When control_store exists but has zero active attestations, uses discovery."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    mock_store = MagicMock()
    mock_store.list_attestations.return_value = []  # empty — fresh deployment

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings") as mock_settings:

        mock_ctx.return_value.control_store = mock_store
        mock_settings.return_value.strict_model_allowlist = False
        mock_ctx.return_value.http_client = MagicMock()
        mock_discover.return_value = [
            {"model_id": "llama3.2:3b", "provider": "ollama"},
        ]

        scope = {"type": "http", "method": "GET", "path": "/v1/models",
                 "query_string": b"", "headers": []}
        request = Request(scope)
        response = await list_models(request)

    import json
    body = json.loads(response.body)
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == "llama3.2:3b"


@pytest.mark.anyio
async def test_models_strict_allowlist_returns_empty_when_no_attestations():
    """Strict mode: empty attestations → empty list (NO fallback to discovery)."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    mock_store = MagicMock()
    mock_store.list_attestations.return_value = []  # empty

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings") as mock_settings:

        mock_ctx.return_value.control_store = mock_store
        mock_ctx.return_value.http_client = MagicMock()
        mock_settings.return_value.strict_model_allowlist = True
        mock_discover.return_value = [{"model_id": "x", "provider": "ollama"}]

        scope = {"type": "http", "method": "GET", "path": "/v1/models",
                 "query_string": b"", "headers": []}
        response = await list_models(Request(scope))

    import json
    body = json.loads(response.body)
    assert body["data"] == []
    mock_discover.assert_not_called()


@pytest.mark.anyio
async def test_models_cache_serves_without_rediscovery():
    """Second call within TTL returns cached result without calling discovery again."""
    from gateway.models_api import list_models, _invalidate_models_cache
    from starlette.requests import Request

    _invalidate_models_cache()

    with patch("gateway.models_api.get_pipeline_context") as mock_ctx, \
         patch("gateway.models_api.discover_provider_models") as mock_discover, \
         patch("gateway.models_api.get_settings") as mock_settings:

        mock_ctx.return_value.control_store = None
        mock_settings.return_value.strict_model_allowlist = False
        mock_ctx.return_value.http_client = MagicMock()
        mock_discover.return_value = [{"model_id": "qwen3:4b", "provider": "ollama"}]

        scope = {"type": "http", "method": "GET", "path": "/v1/models",
                 "query_string": b"", "headers": []}

        await list_models(Request(scope))
        await list_models(Request(scope))  # second call

    # discovery should only have been called once
    assert mock_discover.call_count == 1
