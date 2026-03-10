"""Unit tests for budget threshold alert hooks."""

import pytest

from gateway.pipeline.budget_tracker import BudgetTracker


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


class _MockAlertBus:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.mark.anyio
async def test_budget_alert_fires_at_70_percent():
    """Alert fires when usage crosses 70% threshold."""
    bus = _MockAlertBus()
    bt = BudgetTracker(alert_bus=bus, alert_thresholds=[70, 90, 100])
    bt.configure("t1", None, "monthly", 1000)
    # Reserve and record 700 tokens (70%)
    await bt.check_and_reserve("t1", None, 700)
    await bt.record_usage("t1", None, 700, estimated=700)
    assert len(bus.events) == 1
    assert "70%" in bus.events[0].message


@pytest.mark.anyio
async def test_budget_alert_fires_at_90_percent():
    """Alert fires when usage crosses 90% threshold."""
    bus = _MockAlertBus()
    bt = BudgetTracker(alert_bus=bus, alert_thresholds=[70, 90, 100])
    bt.configure("t1", None, "monthly", 1000)
    await bt.check_and_reserve("t1", None, 900)
    await bt.record_usage("t1", None, 900, estimated=900)
    # Should fire both 70% and 90%
    assert len(bus.events) == 2


@pytest.mark.anyio
async def test_budget_alert_no_duplicate_at_same_threshold():
    """Crossing the same threshold twice doesn't re-alert."""
    bus = _MockAlertBus()
    bt = BudgetTracker(alert_bus=bus, alert_thresholds=[70, 90, 100])
    bt.configure("t1", None, "monthly", 1000)
    # First batch: 700 tokens
    await bt.check_and_reserve("t1", None, 700)
    await bt.record_usage("t1", None, 700, estimated=700)
    count_after_first = len(bus.events)
    # Second batch: 50 more (still above 70%, below 90%)
    await bt.check_and_reserve("t1", None, 50)
    await bt.record_usage("t1", None, 50, estimated=50)
    # Should not fire again for 70%
    assert len(bus.events) == count_after_first


@pytest.mark.anyio
async def test_no_alerts_without_bus():
    """No crash when alert_bus is None."""
    bt = BudgetTracker()
    bt.configure("t1", None, "monthly", 1000)
    await bt.check_and_reserve("t1", None, 800)
    await bt.record_usage("t1", None, 800, estimated=800)
    # Just verify no exception
