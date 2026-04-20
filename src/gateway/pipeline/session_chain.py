"""Phase 13: Merkle chain for session conversation integrity (G5)."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from gateway.core import compute_sha3_512_string

logger = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 128
# Exported alias used by Redis tracker
GENESIS_HASH = _GENESIS_HASH


@dataclass
class ChainValues:
    """Values returned by next_chain_values for the next record in a session."""
    sequence_number: int
    previous_record_hash: str       # legacy SHA3 chain — kept during transition
    previous_record_id: str | None  # new ID-pointer chain


@dataclass
class SessionState:
    session_id: str
    last_sequence_number: int
    last_record_hash: str
    last_activity: datetime
    last_record_id: str | None = None


class SessionChainTracker:
    """
    Thread-safe in-memory Merkle chain tracker.
    Maintains (sequence_number, previous_record_hash) for each active session.
    Sessions are evicted after ttl_seconds of inactivity or when over max_sessions.
    """

    def __init__(self, max_sessions: int = 10_000, ttl_seconds: int = 3600) -> None:
        self._max = max_sessions
        self._ttl = ttl_seconds
        self._sessions: OrderedDict[str, SessionState] = OrderedDict()
        self._lock = asyncio.Lock()
        # Per-session locks ensure that a full (next_chain_values →
        # compute hash → write → update) transaction runs atomically
        # per session. Without this, two concurrent requests for the
        # same session can both read the same `last_record_hash` (since
        # the first one hasn't called `update()` yet), producing two
        # records whose `previous_record_hash` points at the same prior
        # hash — breaking Merkle-chain linkage.
        #
        # The in-process lock is ONLY correct when the gateway runs on
        # a single worker. Multi-replica deployments must configure
        # sticky-session affinity at the LB OR switch to the Redis
        # tracker, which scopes the lock across replicas.
        self._session_locks: dict[str, asyncio.Lock] = {}

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session lock for serializing chain writes.

        Callers (the orchestrator) hold this lock across
        `next_chain_values` + record write + `update` so the critical
        section is atomic per session. Creation is idempotent —
        `dict.setdefault` on a bare dict is safe under single-loop
        asyncio because the check-and-insert contains no `await`.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def next_chain_values(self, session_id: str) -> ChainValues:
        """
        Atomically reserve and return ChainValues for the next record in this session.
        First record in a new session returns sequence_number=0, previous_record_id=None.

        The sequence number is incremented immediately under the lock so that
        concurrent requests to the same session get distinct values. The hash
        and record_id are updated later via update(). A sequence gap (from a
        failed request) is acceptable; duplicate sequence numbers corrupt the chain.
        """
        now = datetime.now(timezone.utc)
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                # Reserve seq 0 — create placeholder state
                self._sessions[session_id] = SessionState(
                    session_id=session_id,
                    last_sequence_number=0,
                    last_record_hash=_GENESIS_HASH,
                    last_activity=now,
                    last_record_id=None,
                )
                self._sessions.move_to_end(session_id)
                return ChainValues(
                    sequence_number=0,
                    previous_record_hash=_GENESIS_HASH,
                    previous_record_id=None,
                )
            next_seq = state.last_sequence_number + 1
            prev_hash = state.last_record_hash
            prev_record_id = state.last_record_id
            # Reserve this sequence number immediately
            state.last_sequence_number = next_seq
            state.last_activity = now
            self._sessions.move_to_end(session_id)
            return ChainValues(
                sequence_number=next_seq,
                previous_record_hash=prev_hash,
                previous_record_id=prev_record_id,
            )

    async def update(
        self,
        session_id: str,
        sequence_number: int,
        record_hash: str = "",
        record_id: str | None = None,
    ) -> None:
        """Record the chain hash/id after a WAL write. Evicts stale sessions when over limit.

        Uses max(current, incoming) for sequence_number so that a slow request's
        update() cannot regress the counter that a faster concurrent request
        already advanced via next_chain_values().
        """
        now = datetime.now(timezone.utc)
        async with self._lock:
            existing = self._sessions.get(session_id)
            # Never regress the sequence number — a concurrent next_chain_values()
            # may have already advanced it past what this update() carries.
            effective_seq = max(sequence_number, existing.last_sequence_number) if existing else sequence_number
            self._sessions[session_id] = SessionState(
                session_id=session_id,
                last_sequence_number=effective_seq,
                last_record_hash=record_hash or (existing.last_record_hash if existing else _GENESIS_HASH),
                last_activity=now,
                last_record_id=record_id if record_id is not None else (existing.last_record_id if existing else None),
            )
            self._sessions.move_to_end(session_id)
            if len(self._sessions) > self._max:
                self._evict_locked(now)

    def _evict_locked(self, now: datetime) -> None:
        """Remove sessions inactive beyond TTL. Then evict oldest if still over limit."""
        cutoff = now.timestamp() - self._ttl
        to_delete = [
            sid for sid, s in self._sessions.items()
            if s.last_activity.timestamp() < cutoff
        ]
        for sid in to_delete:
            del self._sessions[sid]
            self._session_locks.pop(sid, None)
        # If still over limit, pop from front (oldest in LRU order) — O(1)
        while len(self._sessions) > self._max:
            sid, _ = self._sessions.popitem(last=False)
            self._session_locks.pop(sid, None)

    def warm(self, sessions: list[tuple]) -> None:
        """Bulk-load session chain state on startup (e.g. from WAL).

        *sessions* is a list of (session_id, last_sequence_number, last_record_hash_or_id).
        Each tuple may be 3 or 4 elements: (sid, seq, record_hash, record_id=None).
        Called synchronously during startup before any requests, so no lock needed.
        """
        now = datetime.now(timezone.utc)
        for entry in sessions:
            sid, seq = entry[0], entry[1]
            hash_val = entry[2] if len(entry) > 2 else _GENESIS_HASH
            rec_id = entry[3] if len(entry) > 3 else None
            self._sessions[sid] = SessionState(
                session_id=sid,
                last_sequence_number=seq,
                last_record_hash=hash_val,
                last_activity=now,
                last_record_id=rec_id,
            )
        if sessions:
            logger.info("Session chain warmed with %d sessions from WAL", len(sessions))

    def active_session_count(self) -> int:
        return len(self._sessions)


class RedisSessionChainTracker:
    """Redis-backed session chain tracker for multi-replica deployments.

    Keys: gateway:session:{session_id}  (HASH with fields: seq, hash)
    TTL:  self._ttl seconds (refreshed on each access)

    Note: AI chat sessions are inherently sequential (client waits for response
    before sending next message), so the window between next_chain_values and
    update is theoretical, not practical. Configure sticky-session affinity
    (cookie or header-based) at the load balancer per session_id to eliminate
    the window entirely at zero per-request cost.
    """

    _HASH_FIELD_SEQ = "seq"
    _HASH_FIELD_HASH = "hash"
    _HASH_FIELD_RECORD_ID = "record_id"

    def __init__(self, redis_client, ttl: int) -> None:
        self._r = redis_client
        self._ttl = ttl
        # Per-session asyncio.Lock that only serializes within the
        # current worker. Cross-replica serialization still requires
        # sticky-session affinity at the LB — documented below.
        self._session_locks: dict[str, asyncio.Lock] = {}

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """Return a per-session lock serializing chain writes within
        this worker. Multi-replica deployments MUST pair this with
        sticky-session affinity at the LB — otherwise a concurrent
        request for the same session hitting a different replica will
        still read stale `prev_hash` from Redis while this replica is
        mid-transaction.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _key(self, session_id: str) -> str:
        return f"gateway:session:{session_id}"

    async def next_chain_values(self, session_id: str) -> ChainValues:
        """Atomically reserve next sequence number and return ChainValues.

        Uses HINCRBY to atomically increment the sequence counter so concurrent
        requests to the same session get distinct values. The hash is read in
        the same pipeline. A sequence gap from a failed request is acceptable;
        duplicate sequence numbers corrupt the Merkle chain.

        Raises on Redis error — callers must catch and skip chain fields rather
        than forging (0, GENESIS_HASH) for an established session, which would
        silently corrupt the Merkle chain.
        """
        key = self._key(session_id)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, self._HASH_FIELD_SEQ, 1)
            pipe.hget(key, self._HASH_FIELD_HASH)
            pipe.hget(key, self._HASH_FIELD_RECORD_ID)
            pipe.expire(key, self._ttl)
            new_seq_raw, raw_hash, raw_record_id, _ = await pipe.execute()
        # HINCRBY returns the value AFTER increment. First call: 0→1, so seq_num = 1-1 = 0
        seq_num = int(new_seq_raw) - 1
        prev_hash = (
            (raw_hash.decode() if isinstance(raw_hash, bytes) else raw_hash)
            if raw_hash else GENESIS_HASH
        )
        prev_record_id = (
            (raw_record_id.decode() if isinstance(raw_record_id, bytes) else raw_record_id)
            if raw_record_id else None
        )
        return ChainValues(
            sequence_number=seq_num,
            previous_record_hash=prev_hash,
            previous_record_id=prev_record_id,
        )

    async def update(
        self,
        session_id: str,
        seq_num: int,
        record_hash: str = "",
        record_id: str | None = None,
    ) -> None:
        """Atomically write seq, hash, and record_id after a successful record write.

        Raises on Redis error — callers must wrap in try/except and log.
        Silently swallowing this error leaves Redis state permanently stale,
        diverging from the WAL/Walacor audit record.
        """
        key = self._key(session_id)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(key, self._HASH_FIELD_SEQ, seq_num)
            pipe.hset(key, self._HASH_FIELD_HASH, record_hash)
            if record_id is not None:
                pipe.hset(key, self._HASH_FIELD_RECORD_ID, record_id)
            pipe.expire(key, self._ttl)
            await pipe.execute()

    def active_session_count(self) -> int:
        return -1  # Redis doesn't cheaply count by prefix; return sentinel


def make_session_chain_tracker(redis_client, settings):
    """Return Redis-backed tracker if redis_client is provided, else in-memory."""
    if redis_client is not None:
        return RedisSessionChainTracker(redis_client, ttl=settings.session_chain_ttl)
    return SessionChainTracker(
        max_sessions=settings.session_chain_max_sessions,
        ttl_seconds=settings.session_chain_ttl,
    )


def compute_record_hash(
    execution_id: str,
    policy_version: int,
    policy_result: str,
    previous_record_hash: str,
    sequence_number: int,
    timestamp: str,
) -> str:
    """
    Compute SHA3-512 hash over canonical record fields for chain integrity.
    Prompt/response are not hashed here — Walcor backend hashes them.
    """
    canonical = "|".join([
        execution_id,
        str(policy_version),
        policy_result,
        previous_record_hash,
        str(sequence_number),
        timestamp,
    ])
    return compute_sha3_512_string(canonical)
