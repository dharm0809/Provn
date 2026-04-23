import pytest

from gateway.walacor.client import WalacorClient


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _make_client() -> WalacorClient:
    # Constructor signature in the real code is (server, username, password, ...).
    # We never call start(), so no HTTP connection is made.
    return WalacorClient(server="http://localhost:9999", username="x", password="y")


@pytest.mark.anyio
async def test_delivery_snapshot_empty_when_no_activity(anyio_backend):
    client = _make_client()
    snap = client.delivery_snapshot()
    assert snap == {
        "success_rate_60s": 1.0,
        "pending_writes": 0,
        "last_failure": None,
        "last_success_ts": None,
        "time_since_last_success_s": None,
    }


@pytest.mark.anyio
async def test_delivery_snapshot_records_outcomes(anyio_backend):
    client = _make_client()
    client._record_delivery("submit_execution", ok=True, detail=None)
    client._record_delivery("submit_execution", ok=False, detail="HTTP 502")
    client._record_delivery("submit_execution", ok=True, detail=None)
    snap = client.delivery_snapshot()
    assert snap["success_rate_60s"] == pytest.approx(2 / 3, rel=1e-3)
    assert snap["last_failure"]["detail"] == "HTTP 502"
    assert snap["last_success_ts"] is not None
