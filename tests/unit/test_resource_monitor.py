# tests/unit/test_resource_monitor.py
"""Tests for runtime resource monitoring."""
import pytest
import time
from unittest.mock import patch, MagicMock
from gateway.adaptive.resource_monitor import DefaultResourceMonitor

anyio_backend = ["asyncio"]


@pytest.fixture
def monitor():
    return DefaultResourceMonitor(wal_path="/tmp", min_free_pct=5.0)


@pytest.mark.anyio
async def test_disk_check_healthy(monitor):
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=50_000_000_000)
        status = await monitor.check()
    assert status.disk_healthy is True
    assert status.disk_free_pct == 50.0


@pytest.mark.anyio
async def test_disk_check_unhealthy(monitor):
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=2_000_000_000)
        status = await monitor.check()
    assert status.disk_healthy is False


def test_provider_cooldown_no_errors(monitor):
    assert monitor.get_provider_cooldown("ollama") is None


def test_provider_cooldown_under_threshold(monitor):
    # 2 failures out of 10 = 20% — no cooldown
    for i in range(10):
        monitor.record_provider_result("ollama", success=(i >= 2))
    assert monitor.get_provider_cooldown("ollama") is None


def test_provider_cooldown_over_threshold(monitor):
    # 8 failures out of 10 = 80% — should trigger cooldown
    for i in range(10):
        monitor.record_provider_result("ollama", success=(i >= 8))
    cooldown = monitor.get_provider_cooldown("ollama")
    assert cooldown is not None
    assert cooldown > 0


def test_provider_cooldown_ignores_old_errors(monitor):
    monitor.record_provider_result("ollama", success=False)
    monitor.record_provider_result("ollama", success=True)
    assert monitor.get_provider_cooldown("ollama") is None  # too few samples
