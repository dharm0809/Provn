"""Unit tests for alert event bus and dispatchers."""

import asyncio

import pytest

from gateway.alerts.bus import AlertBus, AlertEvent
from gateway.alerts.dispatcher import WebhookDispatcher, SlackDispatcher, PagerDutyDispatcher


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _make_event(type_="budget_threshold", severity="warning"):
    return AlertEvent(
        type=type_,
        severity=severity,
        message="Budget at 90%",
        metadata={"tenant_id": "t1", "usage_pct": 90},
    )


@pytest.mark.anyio
async def test_emit_and_dispatch():
    """Emit event, verify dispatcher receives it."""
    received = []

    class _TestDispatcher:
        async def dispatch(self, event: AlertEvent):
            received.append(event)

    bus = AlertBus()
    bus.add_dispatcher(_TestDispatcher())
    await bus.emit(_make_event())
    # Process one event
    await bus.process_one()
    assert len(received) == 1
    assert received[0].type == "budget_threshold"


def test_slack_format():
    """Slack dispatcher produces Block Kit payload."""
    d = SlackDispatcher(webhook_url="https://hooks.slack.com/test")
    payload = d.format_payload(_make_event())
    assert "blocks" in payload
    assert any("Budget at 90%" in str(b) for b in payload["blocks"])


def test_pagerduty_format():
    """PagerDuty dispatcher produces Events API v2 payload."""
    d = PagerDutyDispatcher(routing_key="test-key")
    payload = d.format_payload(_make_event(severity="critical"))
    assert payload["routing_key"] == "test-key"
    assert payload["event_action"] == "trigger"
    assert "severity" in payload["payload"]


@pytest.mark.anyio
async def test_queue_full_drops_gracefully():
    """Overfill queue, no crash."""
    bus = AlertBus(maxsize=2)
    # Fill queue
    await bus.emit(_make_event())
    await bus.emit(_make_event())
    # Third should be dropped, not raise
    await bus.emit(_make_event())
    assert bus._queue.qsize() == 2


@pytest.mark.anyio
async def test_dispatcher_failure_no_crash():
    """Webhook failure doesn't crash bus."""
    class _FailDispatcher:
        async def dispatch(self, event):
            raise ConnectionError("webhook down")

    bus = AlertBus()
    bus.add_dispatcher(_FailDispatcher())
    await bus.emit(_make_event())
    # Should not raise
    await bus.process_one()
