"""Alert dispatchers — webhook, Slack, PagerDuty."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from gateway.alerts.bus import AlertEvent

logger = logging.getLogger(__name__)

# Module-level latch so the "webhook configured without HMAC secret" warning
# fires once per process even if multiple WebhookDispatcher instances are
# constructed (e.g. comma-separated URLs in WALACOR_WEBHOOK_URLS).
_unsigned_warning_emitted = False


def _resolve_signing_secret(explicit: str | None) -> str:
    """Pick up the signing secret from the explicit arg or the env var.

    Reading the env directly keeps the dispatcher self-contained — the existing
    main.py wiring (`WebhookDispatcher(url)`) gets HMAC signing for free as
    soon as the operator sets ``WALACOR_ALERT_WEBHOOK_SECRET``.
    """
    if explicit:
        return explicit
    return os.environ.get("WALACOR_ALERT_WEBHOOK_SECRET", "") or ""


class WebhookDispatcher:
    """POST JSON alert to a webhook URL.

    When a signing secret is configured (``signing_secret`` arg or
    ``WALACOR_ALERT_WEBHOOK_SECRET`` env var), attaches an HMAC-SHA256
    signature over ``f"{timestamp}.{payload_json}"`` as the
    ``X-Walacor-Signature`` header along with the ``X-Walacor-Timestamp``
    header (unix seconds). Binding the timestamp into the HMAC prevents
    replay of a captured body with a stale timestamp.

    Without a secret, alerts are sent unsigned (preserves the prior
    behavior); a one-shot warning is logged so operators see the gap.
    """

    def __init__(self, webhook_url: str, signing_secret: str | None = None):
        self.webhook_url = webhook_url
        self.signing_secret = _resolve_signing_secret(signing_secret)

        global _unsigned_warning_emitted
        if not self.signing_secret and not _unsigned_warning_emitted:
            _unsigned_warning_emitted = True
            logger.warning(
                "Alert webhook %s configured without WALACOR_ALERT_WEBHOOK_SECRET — "
                "outbound alerts will be unsigned and forgeable. Set the env var to "
                "enable X-Walacor-Signature HMAC-SHA256.",
                webhook_url,
            )

    def format_payload(self, event: AlertEvent) -> dict:
        return {
            "type": event.type,
            "severity": event.severity,
            "message": event.message,
            "metadata": event.metadata,
            "timestamp": event.timestamp,
        }

    def _sign(self, payload_bytes: bytes, timestamp: str) -> str:
        signed_input = f"{timestamp}.".encode("utf-8") + payload_bytes
        return hmac.new(
            self.signing_secret.encode("utf-8"),
            signed_input,
            hashlib.sha256,
        ).hexdigest()

    async def dispatch(self, event: AlertEvent):
        payload = self.format_payload(event)
        # Serialize once so the body sent over the wire is byte-for-byte the
        # same buffer we sign — receivers can recompute HMAC over the raw
        # request body without having to re-canonicalize JSON.
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.signing_secret:
            ts = str(int(time.time()))
            headers["X-Walacor-Timestamp"] = ts
            headers["X-Walacor-Signature"] = self._sign(payload_bytes, ts)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.webhook_url, content=payload_bytes, headers=headers)
            if resp.status_code >= 400:
                logger.warning("Webhook %s returned %d", self.webhook_url, resp.status_code)


class SlackDispatcher:
    """Format payload as Slack Block Kit message and POST to webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def format_payload(self, event: AlertEvent) -> dict:
        severity_emoji = {"info": ":information_source:", "warning": ":warning:", "critical": ":rotating_light:"}.get(
            event.severity, ":grey_question:"
        )
        return {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"{severity_emoji} Gateway Alert: {event.type}"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": event.message},
                },
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"*Severity:* {event.severity} | *Time:* {event.timestamp}"},
                    ],
                },
            ],
        }

    async def dispatch(self, event: AlertEvent):
        payload = self.format_payload(event)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.webhook_url, json=payload)
            if resp.status_code >= 400:
                logger.warning("Slack webhook returned %d", resp.status_code)


class PagerDutyDispatcher:
    """POST to PagerDuty Events API v2."""

    EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

    def __init__(self, routing_key: str):
        self.routing_key = routing_key

    def format_payload(self, event: AlertEvent) -> dict:
        pd_severity = {"info": "info", "warning": "warning", "critical": "critical"}.get(event.severity, "info")
        return {
            "routing_key": self.routing_key,
            "event_action": "trigger",
            "payload": {
                "summary": f"[Walacor Gateway] {event.message}",
                "severity": pd_severity,
                "source": "walacor-gateway",
                "component": event.type,
                "custom_details": event.metadata,
            },
        }

    async def dispatch(self, event: AlertEvent):
        payload = self.format_payload(event)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.EVENTS_URL, json=payload)
            if resp.status_code >= 400:
                logger.warning("PagerDuty returned %d", resp.status_code)
