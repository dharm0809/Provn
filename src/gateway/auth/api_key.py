"""Request authentication: validate API key when WALACOR_GATEWAY_API_KEYS is set."""

from __future__ import annotations

import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.util.request_context import disposition_var

logger = logging.getLogger(__name__)


def get_api_key_from_request(request: Request) -> str | None:
    """Extract API key from Authorization: Bearer or X-API-Key header. Returns None if missing."""
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return auth[7:].strip() or None
    return request.headers.get("X-API-Key", "").strip() or None


def parse_api_keys_with_tenants(
    raw_entries: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Parse ``WALACOR_GATEWAY_API_KEYS`` entries into (keys, key→tenant map).

    Each entry is either a plain key (``"key1"``) or a colon-bound pair
    (``"key1:tenantA"``). The colon separator is unambiguous because gateway
    API keys are alphanumeric or follow the ``wgk-{hex}`` shape — neither
    contains a literal ``:``.

    Backward-compat: a list of plain keys yields an empty tenant map and the
    existing constant-time check still works on the returned key list.

    Edge cases:
      - ``"key:"`` (empty tenant) — treated as a plain key, no binding, WARN logged.
      - ``":tenant"`` (empty key) — entry dropped, WARN logged.
      - Multiple colons (``"key:t:e:nant"``) — split on FIRST colon; the
        remainder is the tenant id. We do not currently validate tenant-id
        characters, so a tenant id may contain ``:``; that's fine because the
        format is positional, not delimited.
      - Entries are stripped of surrounding whitespace; empty entries are
        skipped silently.

    Returns:
        (list_of_valid_keys, dict_of_key_to_tenant_id)

    Examples:
        >>> parse_api_keys_with_tenants(["k1:tA", "k2:tB", "k3"])
        (['k1', 'k2', 'k3'], {'k1': 'tA', 'k2': 'tB'})
    """
    keys: list[str] = []
    tenant_map: dict[str, str] = {}
    for raw in raw_entries:
        entry = (raw or "").strip()
        if not entry:
            continue
        if ":" in entry:
            key, tenant = entry.split(":", 1)
            key = key.strip()
            tenant = tenant.strip()
            if not key:
                logger.warning(
                    "WALACOR_GATEWAY_API_KEYS entry has empty key portion before ':'; "
                    "skipping entry"
                )
                continue
            if not tenant:
                logger.warning(
                    "WALACOR_GATEWAY_API_KEYS entry %r has empty tenant after ':'; "
                    "treating as unbound key",
                    _redact_key(key),
                )
                keys.append(key)
                continue
            keys.append(key)
            tenant_map[key] = tenant
        else:
            keys.append(entry)
    return keys, tenant_map


def _redact_key(key: str) -> str:
    """Redact a key for log output: keep first 4 chars, mask the rest."""
    if len(key) <= 4:
        return "***"
    return f"{key[:4]}***"


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
