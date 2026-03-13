"""Tests for OPA/Rego policy evaluation."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.pipeline.opa_evaluator import query_opa


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.mark.anyio
async def test_opa_allow(anyio_backend):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": True}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {"model_id": "gpt-4"}, mock_client
    )
    assert allowed is True
    assert reason == "opa_allow"


@pytest.mark.anyio
async def test_opa_deny(anyio_backend):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": False}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {}, mock_client
    )
    assert allowed is False
    assert reason == "opa_deny"


@pytest.mark.anyio
async def test_opa_dict_result_allow(anyio_backend):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": {"allow": True, "reason": "model_approved"}}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {}, mock_client
    )
    assert allowed is True
    assert reason == "model_approved"


@pytest.mark.anyio
async def test_opa_dict_result_deny(anyio_backend):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": {"allow": False, "reason": "budget_exceeded"}}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {}, mock_client
    )
    assert allowed is False
    assert reason == "budget_exceeded"


@pytest.mark.anyio
async def test_opa_dict_result_with_allowed_key(anyio_backend):
    """OPA result dict using 'allowed' instead of 'allow' key."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": {"allowed": False, "reason": "not_permitted"}}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {}, mock_client
    )
    assert allowed is False
    assert reason == "not_permitted"


@pytest.mark.anyio
async def test_opa_failopen_on_error(anyio_backend):
    mock_client = AsyncMock()
    mock_client.post.side_effect = Exception("connection refused")
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {}, mock_client
    )
    assert allowed is True
    assert reason == "opa_unavailable"


@pytest.mark.anyio
async def test_opa_failopen_on_http_error(anyio_backend):
    import httpx

    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Internal Server Error", request=MagicMock(), response=mock_resp
    )
    mock_client.post.return_value = mock_resp
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {}, mock_client
    )
    assert allowed is True
    assert reason == "opa_unavailable"


@pytest.mark.anyio
async def test_opa_unexpected_result_type(anyio_backend):
    """OPA returns a non-bool/non-dict result (e.g. a string) -- fail-open."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": "some_string"}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {}, mock_client
    )
    assert allowed is True
    assert reason == "opa_unexpected_result"


@pytest.mark.anyio
async def test_opa_none_result(anyio_backend):
    """OPA returns no 'result' key -- fail-open with unexpected result."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    allowed, reason = await query_opa(
        "http://opa:8181", "/v1/data/allow", {}, mock_client
    )
    # result is None -> not bool, not dict -> unexpected
    assert allowed is True
    assert reason == "opa_unexpected_result"


@pytest.mark.anyio
async def test_opa_url_trailing_slash(anyio_backend):
    """Trailing slash on OPA URL is stripped before concatenation."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": True}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    await query_opa(
        "http://opa:8181/", "/v1/data/allow", {"model_id": "gpt-4"}, mock_client
    )
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "http://opa:8181/v1/data/allow"


@pytest.mark.anyio
async def test_opa_sends_input_payload(anyio_backend):
    """Verify OPA receives the context wrapped in an 'input' key."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": True}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp
    ctx = {"model_id": "gpt-4", "provider": "openai", "status": "active"}
    await query_opa("http://opa:8181", "/v1/data/allow", ctx, mock_client)
    call_args = mock_client.post.call_args
    assert call_args[1]["json"] == {"input": ctx}
