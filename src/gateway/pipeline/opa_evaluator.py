"""OPA/Rego policy evaluation via REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def query_opa(
    opa_url: str,
    policy_path: str,
    context: dict[str, Any],
    http_client: httpx.AsyncClient,
) -> tuple[bool, str]:
    """Query OPA for a policy decision.

    Returns (allowed: bool, reason: str).
    Fail-open on errors -- returns (True, "opa_unavailable").
    """
    url = f"{opa_url.rstrip('/')}{policy_path}"
    try:
        resp = await http_client.post(
            url,
            json={"input": context},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result")

        if isinstance(result, bool):
            return result, "opa_allow" if result else "opa_deny"
        if isinstance(result, dict):
            allowed = result.get("allow", result.get("allowed", True))
            reason = result.get("reason", "opa_deny" if not allowed else "opa_allow")
            return bool(allowed), str(reason)

        logger.warning("OPA returned unexpected result type: %s", type(result))
        return True, "opa_unexpected_result"
    except httpx.HTTPStatusError as e:
        logger.error("OPA returned HTTP %s: %s", e.response.status_code, e, exc_info=True)
        from gateway.config import get_settings
        if get_settings().opa_fail_closed:
            return False, "opa_unavailable_blocked"
        return True, "opa_unavailable"
    except Exception as e:
        logger.error("OPA query failed: %s", e, exc_info=True)
        from gateway.config import get_settings
        if get_settings().opa_fail_closed:
            return False, "opa_unavailable_blocked"
        return True, "opa_unavailable"
