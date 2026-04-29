"""Unit tests for alert event bus and dispatchers."""

import asyncio
import hashlib
import hmac
import json
import logging

import pytest

from gateway.alerts.bus import AlertBus, AlertEvent
from gateway.alerts import dispatcher as dispatcher_mod
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


# ── #18: WebhookDispatcher HMAC signing ──────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_unsigned_warning_latch():
    """Reset the once-per-process warning latch between tests."""
    dispatcher_mod._unsigned_warning_emitted = False
    yield
    dispatcher_mod._unsigned_warning_emitted = False


class _CapturePost:
    """Stand-in for httpx.AsyncClient that records the last POST."""

    def __init__(self, *_, **__):
        self.last_url = None
        self.last_content = None
        self.last_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, content=None, json=None, headers=None):
        self.last_url = url
        self.last_content = content if content is not None else (
            __import__("json").dumps(json).encode("utf-8") if json is not None else b""
        )
        self.last_headers = headers or {}

        class _Resp:
            status_code = 200
        return _Resp()


@pytest.mark.anyio
async def test_webhook_unsigned_when_no_secret(monkeypatch):
    """No secret => no signature headers, request still fires."""
    monkeypatch.delenv("WALACOR_ALERT_WEBHOOK_SECRET", raising=False)
    capture = _CapturePost()
    monkeypatch.setattr("gateway.alerts.dispatcher.httpx.AsyncClient", lambda *a, **kw: capture)

    d = WebhookDispatcher("https://hook.example.com/x")
    await d.dispatch(_make_event())

    assert capture.last_url == "https://hook.example.com/x"
    assert "X-Walacor-Signature" not in capture.last_headers
    assert "X-Walacor-Timestamp" not in capture.last_headers


@pytest.mark.anyio
async def test_webhook_signs_when_secret_set(monkeypatch):
    """With a secret, signature + timestamp headers attach and verify."""
    monkeypatch.delenv("WALACOR_ALERT_WEBHOOK_SECRET", raising=False)
    capture = _CapturePost()
    monkeypatch.setattr("gateway.alerts.dispatcher.httpx.AsyncClient", lambda *a, **kw: capture)

    secret = "super-secret-key"
    d = WebhookDispatcher("https://hook.example.com/x", signing_secret=secret)
    await d.dispatch(_make_event())

    assert "X-Walacor-Signature" in capture.last_headers
    assert "X-Walacor-Timestamp" in capture.last_headers

    ts = capture.last_headers["X-Walacor-Timestamp"]
    sig = capture.last_headers["X-Walacor-Signature"]
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + capture.last_content,
        hashlib.sha256,
    ).hexdigest()
    assert sig == expected
    # Timestamp is bound into the HMAC: tampering with it must invalidate
    # the signature.
    forged = hmac.new(
        secret.encode("utf-8"),
        b"0." + capture.last_content,
        hashlib.sha256,
    ).hexdigest()
    assert sig != forged


@pytest.mark.anyio
async def test_webhook_secret_picked_up_from_env(monkeypatch):
    """Env var fallback so existing main.py wiring auto-signs once secret is set."""
    monkeypatch.setenv("WALACOR_ALERT_WEBHOOK_SECRET", "env-secret")
    capture = _CapturePost()
    monkeypatch.setattr("gateway.alerts.dispatcher.httpx.AsyncClient", lambda *a, **kw: capture)

    d = WebhookDispatcher("https://hook.example.com/x")
    await d.dispatch(_make_event())

    assert "X-Walacor-Signature" in capture.last_headers


def test_unsigned_webhook_logs_warning(monkeypatch, caplog):
    """Constructing a WebhookDispatcher without a secret logs a one-shot warning."""
    monkeypatch.delenv("WALACOR_ALERT_WEBHOOK_SECRET", raising=False)
    with caplog.at_level(logging.WARNING, logger="gateway.alerts.dispatcher"):
        WebhookDispatcher("https://hook.example.com/x")
        # Second instance must NOT spam another warning.
        WebhookDispatcher("https://hook.example.com/y")
    warnings = [r for r in caplog.records if "WALACOR_ALERT_WEBHOOK_SECRET" in r.getMessage()]
    assert len(warnings) == 1


def test_signed_webhook_no_warning(monkeypatch, caplog):
    """When the secret is set, no unsigned-webhook warning fires."""
    monkeypatch.delenv("WALACOR_ALERT_WEBHOOK_SECRET", raising=False)
    with caplog.at_level(logging.WARNING, logger="gateway.alerts.dispatcher"):
        WebhookDispatcher("https://hook.example.com/x", signing_secret="abc")
    warnings = [r for r in caplog.records if "WALACOR_ALERT_WEBHOOK_SECRET" in r.getMessage()]
    assert warnings == []
