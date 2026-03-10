"""Alert dispatchers — webhook, Slack, PagerDuty."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from gateway.alerts.bus import AlertEvent

logger = logging.getLogger(__name__)


class WebhookDispatcher:
    """POST JSON alert to a webhook URL."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def format_payload(self, event: AlertEvent) -> dict:
        return {
            "type": event.type,
            "severity": event.severity,
            "message": event.message,
            "metadata": event.metadata,
            "timestamp": event.timestamp,
        }

    async def dispatch(self, event: AlertEvent):
        payload = self.format_payload(event)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.webhook_url, json=payload)
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
