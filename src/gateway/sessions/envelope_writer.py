"""SessionEnvelopeWriter: accumulates per-turn state and dual-writes Walacor + WAL.

Phase 24 / Phase A scope:
 - Augments — does not replace — the existing per-execution dual-write
   (ETId 9000011) and tool-event dual-write (ETId 9000013).
 - Per-turn flush only. Debounce / idle sweeper is deferred to Phase C.
 - Rollover cap detection only. Full rollover-to-child envelope is Phase C;
   here we simply mark the envelope as ``rolled_over``, flush once more, and
   stop accumulating further turns for that session id (with a warning log).
 - Redaction method is scaffolded but its HTTP API is Phase D.

Failures on either side of the dual-write are logged but NEVER raised — a
broken session envelope must not break the request pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.sessions.state import SessionEnvelopeState

logger = logging.getLogger(__name__)


class SessionEnvelopeWriter:
    """In-memory accumulator + dual-write orchestrator for session envelopes.

    The writer is a process-local cache keyed by ``session_id``. It holds one
    :class:`SessionEnvelopeState` per active session, guards each with its own
    :class:`asyncio.Lock` (so concurrent turns on the same session serialise
    while turns across different sessions remain parallel), and on each
    :meth:`on_turn_complete` writes the updated state to both WAL (local mirror)
    and Walacor (append-only rollup envelope).
    """

    def __init__(self, wal_writer: Any, walacor_client: Any, settings: Any) -> None:
        self._wal_writer = wal_writer
        self._walacor_client = walacor_client
        self._settings = settings
        self._states: dict[str, SessionEnvelopeState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Phase A: lock reaping is Phase C. Lock dict growth is bounded in
        # practice by session_chain_ttl eviction upstream.

    # ── Lock helpers ────────────────────────────────────────────────────

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    # ── Public entry point ──────────────────────────────────────────────

    async def on_turn_complete(
        self,
        session_id: str,
        execution_record: dict[str, Any],
        tool_events_count: int,
        identity: Any | None,
    ) -> None:
        """Record one completed turn, then dual-write the updated envelope.

        Safe to invoke from the orchestrator without try/except — this method
        logs and swallows every failure internally. Returns quickly even on
        partial I/O failure so the response path is never blocked.
        """
        if not session_id:
            return
        if not self._settings.session_envelope_enabled:
            return

        lock = self._lock_for(session_id)
        async with lock:
            state = self._states.get(session_id)
            if state is None:
                state = SessionEnvelopeState(
                    session_id=session_id,
                    tenant_id=self._settings.gateway_tenant_id,
                    gateway_id=self._settings.gateway_id,
                )
                self._states[session_id] = state

            # Already-capped session: stop accumulating. Phase C will mint a
            # child envelope and re-home new turns under a fresh session id.
            if state.status == "rolled_over":
                logger.debug(
                    "Session %s envelope already rolled_over; dropping turn",
                    session_id,
                )
                return

            max_turns = self._settings.session_envelope_max_turns
            max_tokens = self._settings.session_envelope_max_tokens
            if state.is_at_cap(max_turns, max_tokens):
                logger.warning(
                    "Session envelope at cap (turns=%d, total_tokens=%d); "
                    "marking rolled_over and not adding turn for session=%s. "
                    "Phase A does not mint child envelopes — Phase C will.",
                    state.turn_count, state.total_tokens, session_id,
                )
                state.status = "rolled_over"
                await self._write(state)
                return

            try:
                state.add_turn(execution_record, tool_events_count, identity)
            except Exception:
                logger.error(
                    "Session envelope add_turn failed session=%s (non-fatal)",
                    session_id, exc_info=True,
                )
                return

            await self._write(state)

    # ── Redaction scaffold (Phase D completes the HTTP API) ─────────────

    async def redact_turn(
        self,
        session_id: str,
        execution_id: str,
        reason: str,
    ) -> bool:
        """Tombstone a turn's content in an active session envelope.

        Returns True if a matching turn was found and scrubbed. The scrubbed
        envelope is immediately dual-written so the redaction propagates.
        Note: once the in-memory state has been evicted (Phase C idle sweeper
        or rollover), this method returns False; Phase D will add a
        WAL-replay + re-submit path.
        """
        if not session_id or not self._settings.session_envelope_enabled:
            return False
        lock = self._lock_for(session_id)
        async with lock:
            state = self._states.get(session_id)
            if state is None:
                return False
            scrubbed = state.redact_turn(execution_id, reason)
            if scrubbed:
                await self._write(state)
            return scrubbed

    # ── Shutdown hook ───────────────────────────────────────────────────

    async def shutdown_flush(self) -> None:
        """Final best-effort flush of every in-memory envelope.

        Called from the app lifespan shutdown hook. Phase A does not persist
        open/closed lifecycle metadata beyond this — Phase C will add an
        explicit ``idle_closed`` transition and drain.
        """
        for session_id, state in list(self._states.items()):
            try:
                await self._write(state)
            except Exception:
                logger.warning(
                    "Session envelope shutdown flush failed session=%s",
                    session_id, exc_info=True,
                )

    # ── Internal: dual-write ────────────────────────────────────────────

    async def _write(self, state: SessionEnvelopeState) -> None:
        """WAL first, then Walacor. Both sides fail-soft; never raise."""
        # --- WAL mirror (local SQLite, fire-and-forget via writer thread) ---
        if self._wal_writer is not None:
            try:
                self._wal_writer.write_session_envelope(state.to_wal_row())
            except Exception:
                logger.error(
                    "Session envelope WAL write failed session=%s (non-fatal)",
                    state.session_id, exc_info=True,
                )

        # --- Walacor append-only submit --------------------------------------
        if self._walacor_client is not None:
            try:
                await self._walacor_client.write_session_envelope(
                    state.to_walacor_record()
                )
            except Exception:
                logger.warning(
                    "Session envelope Walacor submit failed session=%s (non-fatal)",
                    state.session_id, exc_info=True,
                )
