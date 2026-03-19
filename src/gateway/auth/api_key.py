"""Request authentication: validate API key when WALACOR_GATEWAY_API_KEYS is set."""

from __future__ import annotations

import hmac

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.util.request_context import disposition_var


def get_api_key_from_request(request: Request) -> str | None:
    """Extract API key from Authorization: Bearer or X-API-Key header. Returns None if missing."""
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return auth[7:].strip() or None
    return request.headers.get("X-API-Key", "").strip() or None


def _constant_time_key_check(key: str, api_keys_list: list[str]) -> bool:
    """Check if key matches any configured key using constant-time comparison.

    Iterates ALL keys to avoid leaking which index matched via timing.
    """
    result = False
    for valid_key in api_keys_list:
        if hmac.compare_digest(key, valid_key):
            result = True
    return result


def require_api_key_if_configured(request: Request, api_keys_list: list[str]) -> JSONResponse | None:
    """
    If api_keys_list is non-empty, require a valid API key on the request.
    Returns None if auth passes (no keys configured or valid key present).
    Returns JSONResponse(401) if keys are configured but request has no valid key.
    """
    if not api_keys_list:
        return None
    key = get_api_key_from_request(request)
    if key and _constant_time_key_check(key, api_keys_list):
        return None
    disposition_var.set("denied_auth")
    return JSONResponse(
        {"error": "Missing or invalid API key"},
        status_code=401,
    )
