"""lifecycle event writer (Walacor + SQLite mirror).

Thin wrapper around the existing Walacor client that:

1. Submits a `LifecycleEvent` to the configured ETId
   (`walacor_lifecycle_events_etid`, default 9000024) with exponential
   backoff retry — three attempts at 1s / 2s / 4s. Network wobble or
   Walacor restart are the realistic failure modes and a couple of
   retries cover them without blocking the distillation cycle for long.
2. Mirrors every attempt (successful OR failed) into a SQLite table on
   `intelligence.db` so the dashboard can render lifecycle history
   without cross-network dependency on Walacor.

The mirror is the SOURCE OF TRUTH for local reads; a successful
Walacor write upgrades the row with `walacor_record_id` and
`write_status='written'`. A total failure leaves
`write_status='failed'` with `error_reason` populated — operators can
see the attempt without the Walacor side having to be queried.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.events import LifecycleEvent

logger = logging.getLogger(__name__)

# Backoff schedule: first retry after 1s, then 2s, then 4s. The
# distillation worker calls this off the hot path so the ~7s max
# latency for an unreachable Walacor is acceptable.
_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)


class LifecycleEventWriter:
    """Wraps Walacor writes with retry + local mirroring.

    `walacor_client` is any object exposing
    `async write_record(record: dict, *, etid: int | None = None)`.
    The existing `gateway.walacor.client.WalacorClient` matches that
    shape out of the box — the writer passes the event's ETId via the
    keyword argument when the client supports it, otherwise falls
    back to a positional-only signature.
    """

    def __init__(
        self,
        db: IntelligenceDB,
        walacor_client: Any,
        *,
        etid: int,
        max_attempts: int = 3,
        sleep: Any = asyncio.sleep,
    ) -> None:
        if walacor_client is None:
            raise ValueError("walacor_client is required")
        self._db = db
        self._client = walacor_client
        self._etid = int(etid)
        # `max_attempts` counts the initial call + retries. A value of
        # `1` disables retry. Clamp `max_attempts` against the backoff
        # schedule length so we never exceed what we can delay for.
        self._max_attempts = max(1, min(int(max_attempts), 1 + len(_BACKOFF_SECONDS)))
        # Injectable sleep for deterministic tests.
        self._sleep = sleep

    async def write_event(self, event: LifecycleEvent) -> int:
        """Write `event` to Walacor (with retry) and mirror to SQLite.

        Returns the mirror row id so callers can correlate subsequent
        reads. Never raises — a total write failure records a failed
        mirror row and returns its id.
        """
        record = event.to_record()
        last_error: Exception | None = None
        walacor_id: str | None = None
        attempts_used = 0

        for attempt_index in range(self._max_attempts):
            attempts_used = attempt_index + 1
            try:
                walacor_id = await self._submit(record)
                break
            except Exception as exc:
                last_error = exc
                # Last attempt — log at WARNING, let the mirror record the failure.
                if attempt_index == self._max_attempts - 1:
                    logger.warning(
                        "lifecycle write failed permanently (event=%s attempts=%d)",
                        event.event_type.value, attempts_used, exc_info=True,
                    )
                    break
                delay = _BACKOFF_SECONDS[attempt_index]
                logger.info(
                    "lifecycle write failed (attempt %d/%d, retry in %.1fs): %s",
                    attempts_used, self._max_attempts, delay, exc,
                )
                try:
                    await self._sleep(delay)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Sleep can't realistically fail but guard anyway.
                    pass

        status = "written" if walacor_id is not None else "failed"
        error_reason = None if status == "written" else str(last_error) if last_error else "unknown"

        return await asyncio.to_thread(
            self._write_mirror_row,
            event,
            record,
            walacor_id=walacor_id,
            status=status,
            error_reason=error_reason,
            attempts=attempts_used,
        )

    # ── internals ──────────────────────────────────────────────────────

    async def _submit(self, record: dict[str, Any]) -> str:
        """Submit `record` to Walacor and return the returned id.

        Supports both the keyword-etid and positional-only client
        shapes so this writer works against the real `WalacorClient`
        and any test double that implements just the positional form.
        """
        try:
            result = await self._client.write_record(record, etid=self._etid)
        except TypeError:
            # Fallback for clients whose `write_record` doesn't accept `etid`.
            result = await self._client.write_record(record)
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            # Walacor typically returns `{"id": "..."}` — tolerate any of
            # the common key names rather than coupling to one.
            for key in ("id", "record_id", "walacor_id"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    return value
        # Unrecognized shape — treat the write as "successful but not
        # correlatable". We still record the attempt in the mirror but
        # without a walacor_id so the dashboard can flag the anomaly.
        return ""

    def _write_mirror_row(
        self,
        event: LifecycleEvent,
        record: dict[str, Any],
        *,
        walacor_id: str | None,
        status: str,
        error_reason: str | None,
        attempts: int,
    ) -> int:
        conn = sqlite3.connect(self._db.path)
        try:
            cur = conn.execute(
                "INSERT INTO lifecycle_events_mirror "
                "(event_type, payload_json, timestamp, walacor_record_id, "
                "write_status, error_reason, attempts, written_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_type.value,
                    json.dumps(record, sort_keys=True),
                    event.timestamp,
                    walacor_id or None,
                    status,
                    error_reason,
                    attempts,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()
