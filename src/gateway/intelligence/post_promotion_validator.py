"""Post-promotion validator — auto-rollback when a candidate regresses.

A model can pass shadow validation, promote, and immediately regress on
production traffic — shadow validation samples may not match the
production distribution, or the promotion can interact badly with a
data shift that happened concurrently.

This validator runs every `interval_s`, looks at each model promoted
within `window_h`, and compares:

    current  = accuracy_in_window(start=promoted_at, end=now)
    previous = accuracy_in_window(start=promoted_at - window, end=promoted_at)

If `previous - current >= threshold` and the comparison clears the
sample / coverage floors, the validator calls `registry.rollback`,
emits a `model_rolled_back` lifecycle event, and starts a per-model
cooldown so a flapping candidate doesn't thrash on every tick.

This is the third layer of defence (shadow gate first, drift monitor
second). All three observe the same `divergence_signal` ground truth
populated by the harvesters.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from gateway.intelligence.db import IntelligenceDB
from gateway.intelligence.events import build_rollback_event
from gateway.intelligence.registry import ALLOWED_MODEL_NAMES, ModelRegistry

logger = logging.getLogger(__name__)


@dataclass
class _PromotionRow:
    model: str
    version: str
    promoted_at: datetime
    # Version of the promotion that came BEFORE this one — used as the
    # "previous accuracy" reference. None when no prior promotion is
    # recorded for the model (first-ever promotion).
    previous_version: str | None = None
    previous_promoted_at: datetime | None = None


class PostPromotionValidator:
    """Auto-rollback when a freshly-promoted model regresses on live traffic."""

    def __init__(
        self,
        db: IntelligenceDB,
        registry: ModelRegistry,
        *,
        threshold: float = 0.05,
        window_h: int = 24,
        interval_s: int = 600,
        min_samples: int = 200,
        min_coverage: float = 0.30,
        cooldown_h: int = 12,
        settle_minutes: int = 15,
        lifecycle_writer: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._threshold = float(threshold)
        self._window = timedelta(hours=int(window_h))
        self._interval = max(1, int(interval_s))
        self._min_samples = max(1, int(min_samples))
        self._min_coverage = float(min_coverage)
        self._cooldown = timedelta(hours=int(cooldown_h))
        self._settle = timedelta(minutes=int(settle_minutes))
        self._writer = lifecycle_writer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_check_at: datetime | None = None
        self._last_rollback_at: dict[str, datetime] = {}

    @property
    def last_check_at(self) -> datetime | None:
        return self._last_check_at

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="post-promotion-validator")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.check_once()
            except Exception:
                logger.exception("post-promotion validator cycle failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

    # ── core check ─────────────────────────────────────────────────────

    async def check_once(self) -> list[dict[str, Any]]:
        """Run one validation cycle. Returns a list of {model, action, reason} entries."""
        now = self._clock()
        self._last_check_at = now
        promotions = await asyncio.to_thread(self._recent_promotions, now)
        results: list[dict[str, Any]] = []
        for promo in promotions:
            outcome = await self._evaluate(promo, now)
            if outcome is not None:
                results.append(outcome)
        return results

    async def _evaluate(self, promo: _PromotionRow, now: datetime) -> dict[str, Any] | None:
        # Don't judge a candidate that just promoted — give it time to
        # accumulate signals.
        if now - promo.promoted_at < self._settle:
            return None
        # Per-model cooldown — avoid thrashing on a flapping model.
        last = self._last_rollback_at.get(promo.model)
        if last is not None and (now - last) < self._cooldown:
            return None
        if promo.model not in ALLOWED_MODEL_NAMES:
            return None

        # Per-version filter — see commit 3 of the version-column
        # migration. "current" must look at THIS version's verdicts
        # only, and "previous" must look at the PRIOR promotion's
        # version's verdicts only. Without the filter, a flapping
        # promotion (v2 → v1 → v3 in a short window) would mix
        # versions in both windows and produce noisy comparisons.
        current = await asyncio.to_thread(
            self._db.accuracy_in_window,
            promo.model,
            version=promo.version,
            start=promo.promoted_at,
            end=now,
        )
        # Use the previous version's promoted_at as the start of its
        # window when we know it; otherwise fall back to a duration
        # window backwards from the current promotion.
        prev_window_start = (
            promo.previous_promoted_at
            if promo.previous_promoted_at is not None
            else promo.promoted_at - self._window
        )
        previous = await asyncio.to_thread(
            self._db.accuracy_in_window,
            promo.model,
            version=promo.previous_version,
            start=prev_window_start,
            end=promo.promoted_at,
        )
        if (
            current.sample_count < self._min_samples
            or previous.sample_count < self._min_samples
            or current.coverage < self._min_coverage
            or previous.coverage < self._min_coverage
        ):
            return None
        delta = previous.accuracy - current.accuracy
        if delta < self._threshold:
            return None

        # Rollback — pick the most recent archive file for this model.
        archive_target = self._latest_archive(promo.model)
        if archive_target is None:
            logger.warning(
                "post-promotion: %s regressed (delta=%.3f) but no archive available — cannot rollback",
                promo.model, delta,
            )
            return {
                "model": promo.model,
                "action": "rollback_skipped",
                "reason": "no_archive",
                "delta": delta,
            }
        try:
            await self._registry.rollback(promo.model, archive_target)
        except Exception as exc:
            logger.exception(
                "post-promotion: rollback of %s to %s failed: %s",
                promo.model, archive_target, exc,
            )
            return {
                "model": promo.model,
                "action": "rollback_failed",
                "reason": str(exc),
                "delta": delta,
            }

        self._last_rollback_at[promo.model] = now
        reason_text = (
            f"regression {promo.version} delta={delta:.3f} "
            f"(prev={previous.accuracy:.3f} curr={current.accuracy:.3f})"
        )
        logger.warning(
            "post-promotion auto-rollback: model=%s from=%s to_archive=%s %s",
            promo.model, promo.version, archive_target, reason_text,
        )
        await self._emit_rollback_event(
            model=promo.model,
            from_version=promo.version,
            to_archive=archive_target,
            reason=reason_text,
            delta=delta,
            sample_count=current.sample_count,
        )
        try:
            from gateway.metrics.prometheus import model_rollback_total
            model_rollback_total.labels(model=promo.model, reason="regression").inc()
        except Exception:
            pass
        return {
            "model": promo.model,
            "action": "rolled_back",
            "from_version": promo.version,
            "to_archive": archive_target,
            "delta": delta,
            "reason": reason_text,
        }

    # ── helpers ────────────────────────────────────────────────────────

    def _recent_promotions(self, now: datetime) -> list[_PromotionRow]:
        """One row per model — the most recent promotion within `window_h`,
        annotated with the prior promotion's version + timestamp.

        Walks `lifecycle_events_mirror` newest-first. The first
        MODEL_PROMOTED event per model is the "current" promotion; the
        second is "previous_version" (used as the per-version
        accuracy baseline in `_evaluate`). MODEL_ROLLED_BACK events
        are ignored — they don't change `version=` on the new
        verdicts (the verdicts that follow a rollback carry the
        restored version, which must have been MODEL_PROMOTED at some
        earlier point).
        """
        import json
        import sqlite3 as sql

        cutoff = now - self._window
        seen: dict[str, _PromotionRow] = {}
        with sql.connect(self._db.path) as conn:
            rows = conn.execute(
                "SELECT payload_json, timestamp FROM lifecycle_events_mirror "
                "WHERE event_type = 'model_promoted' "
                "ORDER BY written_at DESC"
            ).fetchall()
        # Pre-parse so we can do a single pass that captures both
        # current + previous in order.
        by_model: dict[str, list[tuple[str, datetime]]] = {}
        for payload_json, ts in rows:
            try:
                payload = json.loads(payload_json)
            except (ValueError, TypeError):
                continue
            model = payload.get("model_name")
            version = payload.get("candidate_version")
            if not (isinstance(model, str) and isinstance(version, str)):
                continue
            try:
                promoted_at = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if promoted_at.tzinfo is None:
                promoted_at = promoted_at.replace(tzinfo=timezone.utc)
            by_model.setdefault(model, []).append((version, promoted_at))
        for model, ordered in by_model.items():
            current_version, current_at = ordered[0]
            if current_at < cutoff:
                continue
            prev_version: str | None = None
            prev_at: datetime | None = None
            if len(ordered) > 1:
                prev_version, prev_at = ordered[1]
            seen[model] = _PromotionRow(
                model=model,
                version=current_version,
                promoted_at=current_at,
                previous_version=prev_version,
                previous_promoted_at=prev_at,
            )
        return list(seen.values())

    def _latest_archive(self, model: str) -> str | None:
        archive_dir = self._registry.base / "archive"
        if not archive_dir.is_dir():
            return None
        matches = sorted(
            p for p in archive_dir.iterdir()
            if p.is_file() and p.suffix == ".onnx" and p.name.startswith(f"{model}-")
        )
        return matches[-1].name if matches else None

    async def _emit_rollback_event(
        self, *, model: str, from_version: str, to_archive: str,
        reason: str, delta: float, sample_count: int,
    ) -> None:
        """Persist the rollback event.

        Audit-trail invariant: the local SQLite mirror is written
        UNCONDITIONALLY. The optional `lifecycle_writer` only handles
        the remote (Walacor) leg — its absence must never cause the
        event to be lost from the local audit log.
        """
        event = build_rollback_event(
            model_name=model,
            from_version=from_version,
            to_archive=to_archive,
            reason=reason,
            delta=delta,
            sample_count=sample_count,
        )
        if self._writer is None:
            # Local-only path — writer would have done both legs.
            try:
                await asyncio.to_thread(
                    self._db.write_lifecycle_event, event, status="local_only",
                )
            except Exception:
                logger.warning("rollback local mirror write failed", exc_info=True)
            return
        # Writer present — it owns both the remote write AND the local
        # mirror row (LifecycleEventWriter._write_mirror_row covers
        # both success and failure paths). Don't double-write here.
        try:
            if hasattr(self._writer, "write_event"):
                await self._writer.write_event(event)
            elif hasattr(self._writer, "write_lifecycle_event"):
                await self._writer.write_lifecycle_event(event)
            else:
                await self._writer.write_record(event.to_record())
        except Exception:
            logger.warning("rollback lifecycle write failed", exc_info=True)
            # Writer failed in an unexpected way (LifecycleEventWriter
            # doesn't raise on remote failure, but other writer shapes
            # might) — fall through to a local-mirror write so the
            # audit row isn't lost.
            try:
                await asyncio.to_thread(
                    self._db.write_lifecycle_event, event,
                    status="failed", error_reason="writer raised",
                )
            except Exception:
                logger.warning("rollback local mirror fallback failed", exc_info=True)
