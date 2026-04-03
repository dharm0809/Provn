"""Auto-install the Walacor Gateway filter plugin into OpenWebUI.

Called at Gateway startup when WALACOR_OPENWEBUI_URL is configured.
Uses the OpenWebUI admin API to create/update the filter function and
enable it as a global filter, so every LLM request flows through the
Gateway's audit pipeline without manual setup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FILTER_ID = "walacor_gateway_audit"
FILTER_NAME = "Walacor Gateway Audit"
_FILTER_SOURCE_PATH = Path(__file__).parent / "openwebui_filter.py"


async def install_openwebui_filter(
    openwebui_url: str,
    openwebui_api_key: str,
    gateway_url: str,
    gateway_api_key: str,
) -> bool:
    """Install or update the Walacor audit filter in OpenWebUI.

    1. Authenticate with OpenWebUI using the admin API key
    2. Check if the filter already exists
    3. Create or update it
    4. Enable as global filter
    5. Set valves (gateway_url, api_key)

    Returns True on success, False on failure (non-fatal, logged).
    """
    base = openwebui_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {openwebui_api_key}",
        "Content-Type": "application/json",
    }

    # Read the filter plugin source
    try:
        filter_source = _FILTER_SOURCE_PATH.read_text()
    except FileNotFoundError:
        logger.error("OpenWebUI filter source not found at %s", _FILTER_SOURCE_PATH)
        return False

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        # ── Check if filter already exists ────────────────────────────
        try:
            resp = await client.get(f"{base}/api/v1/functions/id/{FILTER_ID}", headers=headers)
            exists = resp.status_code == 200
            if exists:
                existing = resp.json()
                existing_content = existing.get("content", "")
                if existing_content.strip() == filter_source.strip():
                    logger.info("OpenWebUI filter '%s' already installed and up-to-date", FILTER_ID)
                    # Still ensure it's global + valves are set
                    await _ensure_global(client, base, headers)
                    await _set_valves(client, base, headers, gateway_url, gateway_api_key)
                    return True
                logger.info("OpenWebUI filter '%s' exists but outdated — updating", FILTER_ID)
        except Exception as e:
            logger.debug("OpenWebUI filter check failed: %s", e)
            exists = False

        # ── Create or update ──────────────────────────────────────────
        payload = {
            "id": FILTER_ID,
            "name": FILTER_NAME,
            "content": filter_source,
            "meta": {
                "description": "Sends audit metadata to Walacor Gateway before every LLM call",
                "manifest": {
                    "title": FILTER_NAME,
                    "author": "Walacor",
                    "version": "1.0.0",
                    "type": "filter",
                },
            },
        }

        try:
            if exists:
                resp = await client.post(
                    f"{base}/api/v1/functions/id/{FILTER_ID}/update",
                    headers=headers,
                    json=payload,
                )
            else:
                resp = await client.post(
                    f"{base}/api/v1/functions/create",
                    headers=headers,
                    json=payload,
                )

            if resp.status_code not in (200, 201):
                logger.warning(
                    "OpenWebUI filter install failed: %d %s",
                    resp.status_code, resp.text[:200],
                )
                return False

            logger.info(
                "OpenWebUI filter '%s' %s successfully",
                FILTER_ID, "updated" if exists else "created",
            )
        except Exception as e:
            logger.warning("OpenWebUI filter install error: %s", e)
            return False

        # ── Enable as global filter ───────────────────────────────────
        await _ensure_global(client, base, headers)

        # ── Set valves ────────────────────────────────────────────────
        await _set_valves(client, base, headers, gateway_url, gateway_api_key)

    return True


async def _ensure_global(
    client: httpx.AsyncClient, base: str, headers: dict
) -> None:
    """Ensure the filter is enabled as a global filter."""
    try:
        # Check current state
        resp = await client.get(f"{base}/api/v1/functions/id/{FILTER_ID}", headers=headers)
        if resp.status_code == 200:
            fn = resp.json()
            if not fn.get("is_global"):
                await client.post(
                    f"{base}/api/v1/functions/id/{FILTER_ID}/toggle/global",
                    headers=headers,
                )
                logger.info("OpenWebUI filter '%s' enabled as global filter", FILTER_ID)
            if not fn.get("is_active"):
                await client.post(
                    f"{base}/api/v1/functions/id/{FILTER_ID}/toggle",
                    headers=headers,
                )
                logger.info("OpenWebUI filter '%s' activated", FILTER_ID)
    except Exception as e:
        logger.debug("OpenWebUI global toggle failed (non-fatal): %s", e)


async def _set_valves(
    client: httpx.AsyncClient, base: str, headers: dict,
    gateway_url: str, gateway_api_key: str,
) -> None:
    """Configure the filter's valves with the gateway connection details."""
    try:
        valves = {
            "gateway_url": gateway_url,
            "api_key": gateway_api_key,
            "notify_files": True,
            "inject_headers": True,
            "priority": 0,
        }
        resp = await client.post(
            f"{base}/api/v1/functions/id/{FILTER_ID}/valves/update",
            headers=headers,
            json=valves,
        )
        if resp.status_code == 200:
            logger.info("OpenWebUI filter valves configured: gateway_url=%s", gateway_url)
        else:
            logger.debug("OpenWebUI valve update: %d %s", resp.status_code, resp.text[:100])
    except Exception as e:
        logger.debug("OpenWebUI valve update failed (non-fatal): %s", e)
