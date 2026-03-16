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
class SessionState:
    session_id: str
    last_sequence_number: int
    last_record_hash: str
    last_activity: datetime


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

    async def next_chain_values(self, session_id: str) -> tuple[int, str]:
        """
        Return (sequence_number, previous_record_hash) for the next record in this session.
        First record in a new session returns (0, GENESIS_HASH).
        """
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return 0, _GENESIS_HASH
            self._sessions.move_to_end(session_id)
            return state.last_sequence_number + 1, state.last_record_hash

    async def update(self, session_id: str, sequence_number: int, record_hash: str) -> None:
        """Record the chain state after a WAL write. Evicts stale sessions when over limit."""
        now = datetime.now(timezone.utc)
        async with self._lock:
            self._sessions[session_id] = SessionState(
                session_id=session_id,
                last_sequence_number=sequence_number,
                last_record_hash=record_hash,
                last_activity=now,
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
        # If still over limit, pop from front (oldest in LRU order) — O(1)
        while len(self._sessions) > self._max:
            self._sessions.popitem(last=False)

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

    def __init__(self, redis_client, ttl: int) -> None:
        self._r = redis_client
        self._ttl = ttl

    def _key(self, session_id: str) -> str:
        return f"gateway:session:{session_id}"

    async def next_chain_values(self, session_id: str) -> tuple[int, str]:
        """Read-only peek: returns (next_seq, prev_hash) without modifying Redis.

        Reads the last stored seq, returns last_seq + 1 (0 for a new session),
        matching in-memory SessionChainTracker.  No mutation happens here —
        update() atomically writes both seq and hash after a successful write.
        This eliminates orphan sequence-number gaps on write failure (Finding 3).

        Raises on Redis error — callers must catch and skip chain fields rather
        than forging (0, GENESIS_HASH) for an established session, which would
        silently corrupt the Merkle chain.
        """
        key = self._key(session_id)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hget(key, self._HASH_FIELD_SEQ)
            pipe.hget(key, self._HASH_FIELD_HASH)
            pipe.expire(key, self._ttl)
            raw_seq, raw_hash, _ = await pipe.execute()
        last_seq = int(raw_seq) if raw_seq else -1
        seq_num = last_seq + 1  # 0 for the first record (matches in-memory; Finding 1)
        prev_hash = (
            (raw_hash.decode() if isinstance(raw_hash, bytes) else raw_hash)
            if raw_hash else GENESIS_HASH
        )
        return seq_num, prev_hash

    async def update(self, session_id: str, seq_num: int, record_hash: str) -> None:
        """Atomically write seq and hash after a successful record write.

        Raises on Redis error — callers must wrap in try/except and log.
        Silently swallowing this error leaves Redis state permanently stale,
        diverging from the WAL/Walacor audit record.
        """
        key = self._key(session_id)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(key, self._HASH_FIELD_SEQ, seq_num)
            pipe.hset(key, self._HASH_FIELD_HASH, record_hash)
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
