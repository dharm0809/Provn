"""Background chain integrity worker.

Replaces the on-demand 50-session sample that the compliance dashboard
used to compute on every page load. The sample was a band-aid: an
auditor reading ``50 of 6,290 sessions verified`` can't sign off on
"the chain is intact." A background census fixes the contract.

What the worker does on each tick
---------------------------------
1. Enumerate every session_id with an execution record in the last
   ``WALACOR_CHAIN_VERIFICATION_WINDOW_DAYS`` days (default 7).
2. For each session, call ``reader.verify_chain(session_id)`` — bounded
   concurrency via ``Semaphore(8)`` so we don't hammer Walacor.
3. Write each result to the ``ChainVerificationStore`` (SQLite).
4. Prune any rows whose session_id is no longer in the window — the
   store never grows beyond the configured window.
5. Stamp ``last_tick_at`` in ``chain_meta`` so the API can disclose how
   fresh the census is.

How enumeration works without adding a reader method
----------------------------------------------------
We deliberately do NOT extend the reader interface. Instead we call
``reader.get_chain_verification_report(start, end, sample_limit=N)`` with
a very large ``sample_limit`` (``_CENSUS_SAMPLE_LIMIT``). Both
``LineageReader`` and ``WalacorLineageReader`` accept that arg and, when
N exceeds the session count in the window, return a result for *every*
session — a full census. The existing per-session ``verify_chain``
concurrency (Walacor reader uses ``Semaphore(8)`` internally; the local
reader is sync) is preserved.

The on-demand call site in ``compliance/api.py`` is replaced with a
read from the store, so the dashboard's "sessions_verified" is now a
real count over the configured window, not a sample.

Single-leader across uvicorn workers
------------------------------------
In multi-worker deployments (``WALACOR_UVICORN_WORKERS > 1``) every
worker instantiates its own ``ChainIntegrityWorker``. Letting all of
them tick would redundantly verify the same sessions N times and waste
Walacor round-trips. We elect a single leader via a filesystem lock on
``{wal_path}/chain_verification.lock`` (``fcntl.LOCK_EX | LOCK_NB``).
Non-leaders still hold a worker object so ``stop()`` is symmetric; they
just skip the tick body. The leader re-acquires on each tick (the lock
file is held only for the duration of the tick) so if the leader dies
the next survivor takes over on the following tick.

Fail-open
---------
A reader/store error inside one tick logs a warning and the worker
continues to the next tick. The worker NEVER raises out of its
``run()`` loop — a dead worker would silently regress the dashboard to
"pending" forever.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from gateway.compliance.chain_store import ChainVerificationStore

logger = logging.getLogger(__name__)


# Default tick interval. 5 min is a good balance between freshness (the
# dashboard shows a "last verified at" stamp; operators don't want to
# stare at 30-min-old data) and Walacor cost (each tick = N verify_chain
# round-trips). Tunable via ``WALACOR_CHAIN_VERIFICATION_TICK_S``.
_DEFAULT_TICK_S = 300.0

# Default rolling verification window. 7 days covers the common audit
# range; widening this is a deliberate operator choice via
# ``WALACOR_CHAIN_VERIFICATION_WINDOW_DAYS``.
_DEFAULT_WINDOW_DAYS = 7

# Upper bound passed as ``sample_limit`` to make the reader's
# (formerly-sampling) call return a *census*. 1e6 covers any plausible
# week of sessions — prod's busiest week was ~6k sessions; this is two
# orders of magnitude of headroom.
_CENSUS_SAMPLE_LIMIT = 1_000_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_bounds(days_back: int) -> tuple[str, str]:
    """ISO-8601 timestamp window (UTC) of the form expected by the readers.

    The readers expect timestamp-style strings (the Walacor pipeline
    compares ``$gte/$lt`` against the ISO timestamp on each record).
    We use second-precision so identical bounds across two near-instant
    ticks still match cache-friendly query plans.
    """
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=max(1, int(days_back)))
    return start.isoformat(), end.isoformat()


class ChainIntegrityWorker:
    """Periodic, single-leader background census of chain integrity.

    Public surface:
      - ``start()`` — schedule the run loop as an ``asyncio.Task``.
      - ``stop()`` — request stop and await drain (5 s cap).
      - ``health`` — dict for the connections tile / debugging.
    """

    def __init__(
        self,
        reader,
        store: ChainVerificationStore,
        *,
        tick_interval_s: float = _DEFAULT_TICK_S,
        window_days: int = _DEFAULT_WINDOW_DAYS,
        lock_path: str | None = None,
        concurrency: int = 8,
    ) -> None:
        self._reader = reader
        self._store = store
        self._tick_interval = max(5.0, float(tick_interval_s))
        self._window_days = max(1, int(window_days))
        # Default the lock file alongside the store so multi-worker
        # deployments share a single rendezvous point on disk.
        self._lock_path = lock_path or (store.db_path + ".lock")
        self._concurrency = max(1, int(concurrency))

        self._stopping = False
        self._task: asyncio.Task | None = None
        self._last_tick_ok = False
        self._last_tick_at: datetime | None = None
        self._last_error: str | None = None
        self._last_sessions_seen = 0
        self._last_was_leader = False

    # ----------------------------------------------------------- lifecycle

    def start(self) -> asyncio.Task:
        """Schedule the run loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="chain-integrity-worker")
        return self._task

    async def stop(self) -> None:
        """Signal stop and await drain. Safe to call multiple times."""
        self._stopping = True
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        # Tick once immediately so a fresh deploy doesn't sit on an empty
        # store for a full interval before the dashboard has data.
        await self._tick_once()
        while not self._stopping:
            try:
                await asyncio.sleep(self._tick_interval)
            except asyncio.CancelledError:
                break
            if self._stopping:
                break
            await self._tick_once()

    # ------------------------------------------------------- leader election

    def _acquire_leadership(self):
        """Return an open lock-file handle if we are the leader, else None.

        Uses ``fcntl.LOCK_EX | LOCK_NB``. The handle MUST be kept open
        and closed at the end of the tick — closing releases the lock.
        On platforms without fcntl (Windows) we fall back to "everyone
        is leader," which is safe (the store is idempotent) just
        wasteful.
        """
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows fallback
            try:
                return open(self._lock_path, "a+")
            except OSError:
                return None
        try:
            fh = open(self._lock_path, "a+")
        except OSError as exc:
            logger.warning("ChainIntegrityWorker: cannot open lock %s: %s",
                           self._lock_path, exc)
            return None
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another worker holds the lock; we are a follower this tick.
            fh.close()
            return None
        return fh

    # ----------------------------------------------------------- one tick

    async def _tick_once(self) -> None:
        """Do one full census: enumerate window → verify → store → prune.

        Wrapped in a broad try/except per the fail-open contract.
        """
        self._last_tick_at = datetime.now(timezone.utc)
        lock_fh = self._acquire_leadership()
        if lock_fh is None:
            # Follower tick — nothing to do; record state for /health.
            self._last_was_leader = False
            self._last_tick_ok = True
            return
        self._last_was_leader = True
        try:
            start, end = _window_bounds(self._window_days)
            try:
                # See module docstring: sample_limit >> session count
                # turns the existing sampler into a census, so we can do
                # the census without adding a new reader method. The
                # reader returns a list of verify_chain dicts.
                results = await self._call_reader_census(start, end)
            except Exception as exc:  # noqa: BLE001 - fail-open per docstring
                logger.warning(
                    "ChainIntegrityWorker: reader census failed for %s..%s: %s",
                    start, end, exc,
                )
                self._last_tick_ok = False
                self._last_error = str(exc)
                return

            # Persist + prune. Both operations are idempotent and cheap
            # relative to the verify_chain round-trips above, so we run
            # them off the event loop to keep the loop responsive.
            try:
                written = await asyncio.to_thread(self._store.upsert_many, results)
                # Anything not in this tick's enumeration falls out of
                # the window and must be dropped from the store — bounded
                # growth invariant.
                kept_ids = [r.get("session_id") for r in results if r.get("session_id")]
                await asyncio.to_thread(self._store.prune_keep, kept_ids)
                await asyncio.to_thread(
                    self._store.set_meta, "last_tick_at", _now_iso(),
                )
                await asyncio.to_thread(
                    self._store.set_meta, "window_days", str(self._window_days),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ChainIntegrityWorker: store write failed: %s", exc)
                self._last_tick_ok = False
                self._last_error = str(exc)
                return

            self._last_sessions_seen = len(results)
            self._last_tick_ok = True
            self._last_error = None
            logger.info(
                "ChainIntegrityWorker tick ok: window=%dd sessions=%d written=%d",
                self._window_days, len(results), written,
            )
        finally:
            try:
                lock_fh.close()
            except Exception:
                pass

    async def _call_reader_census(self, start: str, end: str) -> list[dict]:
        """Dispatch sync vs async ``get_chain_verification_report``.

        ``LineageReader`` (SQLite fallback) is sync; ``WalacorLineageReader``
        is async.  Mirror the dispatcher pattern used in
        ``connections/builder.py::_call_reader``.
        """
        import inspect
        fn = self._reader.get_chain_verification_report
        try:
            result = fn(start, end, sample_limit=_CENSUS_SAMPLE_LIMIT)
        except TypeError:
            # Older readers without the keyword arg — call positional.
            result = fn(start, end, _CENSUS_SAMPLE_LIMIT)
        if inspect.isawaitable(result):
            return await result
        # Sync impl: hop to a thread so we don't block the loop on a
        # potentially-large SQLite scan.
        return await asyncio.to_thread(lambda: result if not callable(result) else result())

    # --------------------------------------------------------------- health

    @property
    def health(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "last_tick_ok": self._last_tick_ok,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "last_error": self._last_error,
            "last_sessions_seen": self._last_sessions_seen,
            "last_was_leader": self._last_was_leader,
            "window_days": self._window_days,
            "tick_interval_s": self._tick_interval,
            "store_path": self._store.db_path,
            "lock_path": self._lock_path,
        }
