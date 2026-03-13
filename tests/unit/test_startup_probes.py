# tests/unit/test_startup_probes.py
"""Tests for startup probe implementations."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from gateway.adaptive.startup_probes import (
    ProviderHealthProbe, RoutingEndpointProbe, DiskSpaceProbe,
    APIVersionProbe, run_startup_probes,
)
from gateway.adaptive.interfaces import ProbeResult

anyio_backend = ["asyncio"]


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.provider_ollama_url = "http://localhost:11434"
    s.provider_openai_key = ""
    s.provider_anthropic_key = ""
    s.provider_openai_url = "https://api.openai.com"
    s.model_routes = []
    s.wal_path = "/tmp"
    s.wal_max_size_gb = 10.0
    s.disk_min_free_percent = 5.0
    s.custom_startup_probes = ""
    s.provider_health_check_on_startup = True
    return s


@pytest.mark.anyio
async def test_provider_health_probe_ollama_reachable(mock_settings):
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    client.get = AsyncMock(return_value=resp)
    probe = ProviderHealthProbe()
    result = await probe.check(client, mock_settings)
    assert result.healthy is True
    assert "ollama" in result.detail


@pytest.mark.anyio
async def test_provider_health_probe_ollama_down(mock_settings):
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("Connection refused"))
    probe = ProviderHealthProbe()
    result = await probe.check(client, mock_settings)
    assert result.detail["ollama"]["ok"] is False


@pytest.mark.anyio
async def test_provider_health_probe_no_providers(mock_settings):
    mock_settings.provider_ollama_url = ""
    client = AsyncMock()
    probe = ProviderHealthProbe()
    result = await probe.check(client, mock_settings)
    assert result.healthy is True  # no providers = nothing to check


@pytest.mark.anyio
async def test_routing_endpoint_probe_no_routes(mock_settings):
    probe = RoutingEndpointProbe()
    result = await probe.check(AsyncMock(), mock_settings)
    assert result.healthy is True


@pytest.mark.anyio
async def test_routing_endpoint_probe_bad_url(mock_settings):
    mock_settings.model_routes = [{"pattern": "gpt-*", "url": "http://dead:1234"}]
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("unreachable"))
    probe = RoutingEndpointProbe()
    result = await probe.check(client, mock_settings)
    assert result.healthy is False


@pytest.mark.anyio
async def test_disk_space_probe_healthy():
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=50_000_000_000, used=50_000_000_000)
        probe = DiskSpaceProbe()
        settings = MagicMock(wal_path="/tmp", wal_max_size_gb=10.0, disk_min_free_percent=5.0)
        result = await probe.check(AsyncMock(), settings)
        assert result.healthy is True
        assert result.detail["free_pct"] == 50.0


@pytest.mark.anyio
async def test_disk_space_probe_low_disk():
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=2_000_000_000, used=98_000_000_000)
        probe = DiskSpaceProbe()
        settings = MagicMock(wal_path="/tmp", wal_max_size_gb=10.0, disk_min_free_percent=5.0)
        result = await probe.check(AsyncMock(), settings)
        assert result.healthy is False
        assert result.detail["free_pct"] == 2.0


@pytest.mark.anyio
async def test_disk_space_auto_scale_caps_at_configured_max():
    with patch("shutil.disk_usage") as mock_du:
        # 500GB free — auto_max should cap at configured 10GB
        mock_du.return_value = MagicMock(total=1_000_000_000_000, free=500_000_000_000, used=500_000_000_000)
        probe = DiskSpaceProbe()
        settings = MagicMock(wal_path="/tmp", wal_max_size_gb=10.0, disk_min_free_percent=5.0)
        result = await probe.check(AsyncMock(), settings)
        assert result.detail["auto_max_gb"] == 10.0


@pytest.mark.anyio
async def test_api_version_probe_ollama(mock_settings):
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"version": "0.6.2"})
    client.get = AsyncMock(return_value=resp)
    probe = APIVersionProbe()
    result = await probe.check(client, mock_settings)
    assert result.detail.get("ollama_version") == "0.6.2"


@pytest.mark.anyio
async def test_run_startup_probes_concurrent(mock_settings):
    client = AsyncMock()
    resp = MagicMock(status_code=200)
    resp.json = MagicMock(return_value={"version": "0.6.2"})
    client.get = AsyncMock(return_value=resp)
    with patch("shutil.disk_usage") as mock_du:
        mock_du.return_value = MagicMock(total=100_000_000_000, free=50_000_000_000, used=50_000_000_000)
        results = await run_startup_probes(client, mock_settings)
    assert "provider_health" in results
    assert "disk_space" in results


@pytest.mark.anyio
async def test_probe_exception_doesnt_crash():
    """A single probe failure must not crash the entire probe run."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("boom"))
    with patch("shutil.disk_usage", side_effect=OSError("no such dir")):
        settings = MagicMock(
            provider_ollama_url="http://bad:11434", provider_openai_key="",
            provider_anthropic_key="", model_routes=[], wal_path="/nonexistent",
            wal_max_size_gb=10.0, disk_min_free_percent=5.0,
            custom_startup_probes="", provider_health_check_on_startup=True)
        results = await run_startup_probes(client, settings)
    # Should still return results dict (probes failed gracefully)
    assert isinstance(results, dict)
