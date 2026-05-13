"""Regression test for fix A7: walacor_delivery tile field rename.

The connections tile used to surface ``pending_writes`` (a WAL-local
metric) next to ``success_rate_60s`` (a Walacor HTTP metric), which
operators misread as "Walacor is failing" when Walacor was fine and
the WAL backlog was caused by an unrelated condition.

Fix renames the field to ``wal_local_backlog`` and adds a separate
``walacor_delivery_lag_seconds`` derived from the WAL's oldest pending
record. This test pins both detail keys.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from gateway.connections.builder import build_walacor_delivery_tile


def _ctx_with(walacor: MagicMock, wal: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.walacor_client = walacor
    ctx.wal_writer = wal
    return ctx


def test_tile_exposes_renamed_fields():
    """A7: detail must expose wal_local_backlog + walacor_delivery_lag_seconds."""
    walacor = MagicMock()
    walacor.delivery_snapshot = MagicMock(return_value={
        "success_rate_60s": 1.0,
        "last_failure": None,
        "last_success_ts": "2026-05-12T00:00:00Z",
        "time_since_last_success_s": 1.0,
    })
    wal = MagicMock()
    wal.pending_count = MagicMock(return_value=3)
    wal.oldest_pending_seconds = MagicMock(return_value=42.5)

    tile = build_walacor_delivery_tile(_ctx_with(walacor, wal))
    detail = tile["detail"]
    assert "wal_local_backlog" in detail, (
        "renamed field must be present"
    )
    assert detail["wal_local_backlog"] == 3
    assert "walacor_delivery_lag_seconds" in detail
    assert detail["walacor_delivery_lag_seconds"] == 42.5
    # The legacy key must be gone so operator tooling that grep'd
    # for "pending_writes": next to "Walacor delivery" is forced to
    # update to the correct label.
    assert "pending_writes" not in detail


def test_tile_subline_uses_local_backlog_language():
    """A7: the subline calls out local backlog rather than Walacor pending writes."""
    walacor = MagicMock()
    walacor.delivery_snapshot = MagicMock(return_value={
        "success_rate_60s": 1.0,
        "last_failure": None,
        "last_success_ts": None,
        "time_since_last_success_s": None,
    })
    wal = MagicMock()
    wal.pending_count = MagicMock(return_value=12)
    wal.oldest_pending_seconds = MagicMock(return_value=None)

    tile = build_walacor_delivery_tile(_ctx_with(walacor, wal))
    assert "local backlog" in tile["subline"]
    assert "12" in tile["subline"]


def test_tile_handles_missing_wal():
    """A7: tile must degrade gracefully when wal_writer is absent."""
    walacor = MagicMock()
    walacor.delivery_snapshot = MagicMock(return_value={
        "success_rate_60s": 1.0,
        "last_failure": None,
        "last_success_ts": None,
        "time_since_last_success_s": None,
    })
    tile = build_walacor_delivery_tile(_ctx_with(walacor, wal=None))
    assert tile["detail"]["wal_local_backlog"] is None
    assert tile["detail"]["walacor_delivery_lag_seconds"] is None
