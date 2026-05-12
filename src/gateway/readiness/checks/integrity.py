"""Integrity readiness checks: INT-01 through INT-07."""

from __future__ import annotations

import json
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from gateway.config import get_settings
from gateway.crypto.signing import signing_key_available, verify_canonical
from gateway.readiness.protocol import Category, CheckResult, Severity
from gateway.readiness.registry import register

if TYPE_CHECKING:
    from gateway.pipeline.context import PipelineContext


def _open_wal_ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


class _Int01SigningKeyLoaded:
    id = "INT-01"
    name = "Signing key loaded"
    category = Category.integrity
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        available = signing_key_available()
        elapsed = int((time.monotonic() - t0) * 1000)
        if available:
            return CheckResult(
                status="green",
                detail="Ed25519 signing key loaded",
                evidence={"key_loaded": True},
                elapsed_ms=elapsed,
            )
        return CheckResult(
            status="red",
            detail="Ed25519 signing key not loaded — records will be written unsigned",
            remediation="Set WALACOR_RECORD_SIGNING_KEY_PATH or ensure wal_path is writable for auto-provisioning",
            evidence={"key_loaded": False},
            elapsed_ms=elapsed,
        )


class _Int02SigningActive:
    id = "INT-02"
    name = "Signing active"
    category = Category.integrity
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if ctx.wal_writer is None:
            return CheckResult(status="amber", detail="WAL writer not available — cannot sample records", elapsed_ms=elapsed_ms())

        try:
            conn = _open_wal_ro(ctx.wal_writer._path)  # type: ignore[attr-defined]
            # Exclude internal execution records that legitimately skip the
            # signing path (startup self-test, intelligence verdict writes).
            rows = conn.execute(
                """
                SELECT record_json FROM wal_records
                 WHERE event_type='execution'
                   AND execution_id NOT LIKE 'self-test-%'
                   AND (request_type IS NULL OR request_type NOT IN ('system_task', 'intelligence_verdict'))
                 ORDER BY rowid DESC LIMIT 50
                """
            ).fetchall()
            conn.close()
        except Exception as exc:
            return CheckResult(status="amber", detail=f"Could not sample WAL records: {exc}", elapsed_ms=elapsed_ms())

        if not rows:
            return CheckResult(
                status="amber",
                detail="No execution records in WAL yet — cannot assess signing",
                evidence={"sampled": 0, "signed": 0},
                elapsed_ms=elapsed_ms(),
            )

        sampled = len(rows)
        signed = 0
        for (rec_json,) in rows:
            try:
                if json.loads(rec_json).get("record_signature"):
                    signed += 1
            except Exception:
                pass

        pct = signed / sampled
        status = "green" if pct >= 0.95 else ("amber" if pct >= 0.5 else "red")
        return CheckResult(
            status=status,
            detail=f"{signed}/{sampled} recent records signed ({pct:.0%})",
            remediation=None if status == "green" else "Ed25519 key loaded but records lack record_signature — check orchestrator._apply_session_chain",
            evidence={"sampled": sampled, "signed": signed, "window": "last 50 user-request execution records"},
            elapsed_ms=elapsed_ms(),
        )


class _Int03SignaturesVerify:
    id = "INT-03"
    name = "Signatures verify"
    category = Category.integrity
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if not signing_key_available():
            return CheckResult(status="amber", detail="No verify key loaded — cannot verify signatures", elapsed_ms=elapsed_ms())
        if ctx.wal_writer is None:
            return CheckResult(status="amber", detail="WAL writer not available", elapsed_ms=elapsed_ms())

        try:
            conn = _open_wal_ro(ctx.wal_writer._path)  # type: ignore[attr-defined]
            rows = conn.execute(
                "SELECT record_json FROM wal_records WHERE event_type='execution' "
                "ORDER BY rowid DESC LIMIT 100"
            ).fetchall()
            conn.close()
        except Exception as exc:
            return CheckResult(status="amber", detail=f"Could not sample records: {exc}", elapsed_ms=elapsed_ms())

        verified = attempted = 0
        first_failure = None
        for (rec_json,) in rows:
            if attempted >= 20:
                break
            try:
                rec = json.loads(rec_json)
            except Exception:
                continue
            sig = rec.get("record_signature")
            if not sig:
                continue
            attempted += 1
            ok = verify_canonical(
                record_id=rec.get("record_id"),
                previous_record_id=rec.get("previous_record_id"),
                sequence_number=int(rec.get("sequence_number") or 0),
                execution_id=str(rec.get("execution_id") or ""),
                timestamp=str(rec.get("timestamp") or ""),
                signature=sig,
            )
            if ok:
                verified += 1
            elif first_failure is None:
                first_failure = rec.get("execution_id")

        if attempted == 0:
            return CheckResult(
                status="amber",
                detail="No signed records found in last 100 — nothing to verify",
                evidence={"attempted": 0, "verified": 0},
                elapsed_ms=elapsed_ms(),
            )
        if verified == attempted:
            return CheckResult(
                status="green",
                detail=f"{verified}/{attempted} signatures verify",
                evidence={"attempted": attempted, "verified": verified},
                elapsed_ms=elapsed_ms(),
            )
        return CheckResult(
            status="red",
            detail=f"{verified}/{attempted} signatures verify — first failure execution_id={first_failure}",
            remediation="Key rotation without re-signing, or records tampered post-write",
            evidence={"attempted": attempted, "verified": verified, "first_failure": first_failure},
            elapsed_ms=elapsed_ms(),
        )


class _Int04WalacorAnchoringActive:
    id = "INT-04"
    name = "Walacor anchoring active"
    category = Category.integrity
    # Anchoring happens on the Walacor backend — the gateway cannot speed it
    # up or force it. A missing BlockId/TransId/DH is reportable (we still
    # want the dashboard to show it) but not a reason to remove the gateway
    # from the load balancer: signing + WAL + chain continuity still hold
    # locally. Downgrade to warn so the check surfaces without forcing the
    # rollup to "unready".
    severity = Severity.warn

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)
        settings = get_settings()

        if not settings.walacor_storage_enabled:
            return CheckResult(status="amber", detail="Walacor storage not configured", elapsed_ms=elapsed_ms())
        # Anchor fields (BlockId/TransId/DH) are populated by Walacor when
        # records are read back — they never live in the pre-submit local
        # WAL. Query Walacor directly for recent records and inspect the
        # envelope. If there's no client, we can't judge anchoring so this
        # check stays amber.
        client = getattr(ctx, "walacor_client", None)
        if client is None:
            return CheckResult(status="amber", detail="Walacor client not available", elapsed_ms=elapsed_ms())

        # Walacor stores the tamper-evidence anchor (DataHash) in a separate
        # /envelopes/hashes collection — NOT on the data record returned by
        # /query/getcomplex. Prior versions of this check projected
        # `BlockId/TransId/DH` off the data record and reported 0% anchored
        # forever because those columns are unconditionally null in the data
        # table. The right signal is `DH` from the hashes endpoint; the
        # blockchain identifiers (BlockId, TransId) populate later when OCM
        # batches a commit to the public chain — they're a secondary proof,
        # not the primary anchor.
        try:
            cutoff_ms = int(
                (datetime.now(timezone.utc) - timedelta(minutes=2)).timestamp() * 1000
            )
            hashes = await client.list_envelope_hashes(settings.walacor_executions_etid)
        except Exception as exc:
            return CheckResult(status="amber", detail=f"Walacor hashes query failed: {exc}", elapsed_ms=elapsed_ms())

        # Filter to records old enough that anchoring had a chance to run.
        # CreatedAt is millis-since-epoch from Walacor.
        eligible = [h for h in hashes if isinstance(h.get("CreatedAt"), (int, float)) and h["CreatedAt"] <= cutoff_ms]
        if not eligible:
            return CheckResult(
                status="amber",
                detail="No envelope-hash records older than 2m to verify yet",
                elapsed_ms=elapsed_ms(),
            )

        # Most-recent 20 by CreatedAt
        eligible.sort(key=lambda h: h.get("CreatedAt") or 0, reverse=True)
        sample = eligible[:20]
        sampled = len(sample)
        # DH is the primary anchor; ES values 30→40→80 trace the OCM pipeline,
        # 80 means "fully OCM-anchored, off-chain stored".
        anchored = sum(1 for h in sample if h.get("DH"))
        on_chain = sum(1 for h in sample if h.get("BlockId") and h.get("TransId"))
        pct = anchored / sampled
        status = "green" if pct >= 0.95 else ("amber" if pct >= 0.5 else "red")
        detail = f"{anchored}/{sampled} recent records anchored via DH ({pct:.0%})"
        if on_chain:
            detail += f"; {on_chain}/{sampled} also committed on-chain (BlockId/TransId)"
        return CheckResult(
            status=status,
            detail=detail,
            remediation=None if status == "green" else (
                "Walacor writes succeeded but DataHash not populated — verify OCM "
                "anchor worker health on the backend"
            ),
            evidence={"sampled": sampled, "anchored_dh": anchored, "on_chain": on_chain},
            elapsed_ms=elapsed_ms(),
        )


class _Int05AnchorRoundTrip:
    id = "INT-05"
    name = "Anchor round-trip"
    category = Category.integrity
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)
        settings = get_settings()

        if ctx.walacor_client is None:
            return CheckResult(status="amber", detail="Walacor client not available", elapsed_ms=elapsed_ms())

        # Round-trip strategy:
        #   1. Fetch /envelopes/hashes to find an anchored EId (DH non-null).
        #   2. Re-query that EId via /query/getcomplex to confirm the data
        #      record still exists at the same EId — i.e. the anchor proof
        #      and the data are in sync. (Mismatch ⇒ data deleted or moved
        #      under the anchor, which would invalidate the proof.)
        # We deliberately don't compare DH-against-recomputed-hash here — that
        # would require client-side encryption parity with OCM, which is out
        # of scope for a readiness check.
        try:
            hashes = await ctx.walacor_client.list_envelope_hashes(
                settings.walacor_executions_etid
            )
        except Exception as exc:
            return CheckResult(status="amber", detail=f"Hashes fetch failed: {exc}", elapsed_ms=elapsed_ms())

        # Match INT-04's eligibility window: a record's anchor proof and data
        # row are written by separate OCM stages, and the hashes endpoint can
        # be a few seconds ahead of the data table. Only probe records old
        # enough (2 minutes) that any consistency window has closed.
        cutoff_ms = int(
            (datetime.now(timezone.utc) - timedelta(minutes=2)).timestamp() * 1000
        )
        anchored = [
            h for h in hashes
            if h.get("DH") and h.get("EId")
            and isinstance(h.get("CreatedAt"), (int, float)) and h["CreatedAt"] <= cutoff_ms
        ]
        if not anchored:
            return CheckResult(
                status="amber",
                detail="No anchored records older than 2m available to probe",
                elapsed_ms=elapsed_ms(),
            )

        # Sample multiple candidates rather than flipping the whole check on
        # one random pick. Orphan hash entries can exist for legitimate reasons
        # — operator-issued probes that failed schema validation but still
        # generated a hash table row, or soft-deleted data records. We report
        # an aggregate hash↔data match rate and only red if a strong majority
        # are orphaned, which would indicate real corruption.
        sample_size = min(10, len(anchored))
        picks = random.sample(anchored, sample_size)
        # Single batched query: match all sampled EIds at once.
        eids = [p["EId"] for p in picks]
        try:
            result = await ctx.walacor_client.query_complex(
                settings.walacor_executions_etid,
                [{"$match": {"EId": {"$in": eids}}}, {"$project": {"EId": 1, "execution_id": 1}}],
            )
        except Exception as exc:
            return CheckResult(
                status="red",
                detail=f"Walacor data-record round-trip failed: {exc}",
                remediation="Hashes endpoint reachable but /query/getcomplex isn't — partial outage",
                evidence={"sample": sample_size},
                elapsed_ms=elapsed_ms(),
            )

        matched_eids = {r.get("EId") for r in result if r.get("EId")}
        matched = sum(1 for p in picks if p["EId"] in matched_eids)
        pct = matched / sample_size
        if pct >= 0.9:
            status = "green"
        elif pct >= 0.5:
            status = "amber"
        else:
            status = "red"
        detail = f"{matched}/{sample_size} sampled anchors have matching data records ({pct:.0%})"
        if matched < sample_size:
            detail += " — orphans likely from failed-validation probes"
        return CheckResult(
            status=status,
            detail=detail,
            remediation=None if status == "green" else (
                "Many DH anchors lack matching data records — check Walacor data-table integrity "
                "and review recent submits for schema validation failures"
            ),
            evidence={"sampled": sample_size, "matched": matched, "match_pct": round(pct, 2)},
            elapsed_ms=elapsed_ms(),
        )


class _Int06ChainContinuity:
    id = "INT-06"
    name = "Chain continuity"
    category = Category.integrity
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if ctx.wal_writer is None:
            return CheckResult(status="amber", detail="WAL writer not available", elapsed_ms=elapsed_ms())

        try:
            from gateway.lineage.reader import LineageReader
            reader = LineageReader(ctx.wal_writer._path)  # type: ignore[attr-defined]
            conn = _open_wal_ro(ctx.wal_writer._path)  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT session_id, COUNT(*) c FROM wal_records "
                "WHERE event_type='execution' AND session_id IS NOT NULL "
                "GROUP BY session_id HAVING c > 1 ORDER BY MAX(rowid) DESC LIMIT 1"
            ).fetchone()
            conn.close()
        except Exception as exc:
            return CheckResult(status="amber", detail=f"Could not query sessions: {exc}", elapsed_ms=elapsed_ms())

        if not row:
            return CheckResult(status="amber", detail="No multi-record session found to verify", elapsed_ms=elapsed_ms())

        session_id = row[0]
        try:
            result = reader.verify_chain(session_id)
        except Exception as exc:
            return CheckResult(status="amber", detail=f"verify_chain raised: {exc}", elapsed_ms=elapsed_ms())

        errors = result.get("errors", [])
        if not errors:
            return CheckResult(
                status="green",
                detail=f"Chain continuity OK for session {session_id} ({result.get('records_checked', 0)} records)",
                evidence={"session_id": session_id, "records_checked": result.get("records_checked")},
                elapsed_ms=elapsed_ms(),
            )
        return CheckResult(
            status="red",
            detail=f"Chain broken for session {session_id}: {errors[0]}",
            remediation="Investigate sequence gaps or id-pointer mismatches in orchestrator._apply_session_chain",
            evidence={"session_id": session_id, "errors": errors[:3]},
            elapsed_ms=elapsed_ms(),
        )


class _Int07AttemptCompleteness:
    id = "INT-07"
    name = "Attempt completeness"
    category = Category.integrity
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - t0) * 1000)

        if ctx.wal_writer is None:
            return CheckResult(status="amber", detail="WAL writer not available", elapsed_ms=elapsed_ms())

        try:
            conn = _open_wal_ro(ctx.wal_writer._path)  # type: ignore[attr-defined]
            # Join executions (older than 30s to avoid race with the async
            # attempt write in completeness_middleware's finally block) ⨝ attempts.
            # Restrict to last 100 executions for bounded cost.
            #
            # created_at is stored as ISO 8601 ('2025-…T01:30:45.123+00:00'),
            # but datetime('now') returns SQLite format ('2025-… 01:30:45').
            # Direct string comparison gets the 'T' vs space ordering wrong — wrap
            # both sides in datetime() so SQLite parses them to a common form.
            # Exclude internal execution records that never flow through the
            # HTTP request pipeline (startup self-test, intelligence worker
            # verdicts, etc.) — completeness_middleware only writes attempt
            # rows for real inbound requests, so these would be counted as
            # spurious orphans.
            rows = conn.execute(
                """
                SELECT r.execution_id,
                       (SELECT COUNT(*) FROM gateway_attempts a WHERE a.execution_id = r.execution_id) AS matched
                  FROM wal_records r
                 WHERE r.event_type='execution'
                   AND r.execution_id NOT LIKE 'self-test-%'
                   AND (r.request_type IS NULL OR r.request_type NOT IN ('system_task', 'intelligence_verdict'))
                   AND datetime(r.created_at) < datetime('now', '-30 seconds')
                 ORDER BY r.rowid DESC
                 LIMIT 100
                """
            ).fetchall()
            conn.close()
        except Exception as exc:
            return CheckResult(status="amber", detail=f"Query failed: {exc}", elapsed_ms=elapsed_ms())

        if not rows:
            return CheckResult(status="amber", detail="No executions older than 30s to check", elapsed_ms=elapsed_ms())

        total = len(rows)
        missing = [r[0] for r in rows if r[1] == 0]
        if not missing:
            return CheckResult(
                status="green",
                detail=f"{total}/{total} executions have matching attempt row",
                evidence={"sampled": total, "missing": 0},
                elapsed_ms=elapsed_ms(),
            )
        return CheckResult(
            status="red",
            detail=f"{len(missing)}/{total} executions missing attempt row",
            remediation="completeness_middleware finally block failing for some requests — investigate write path",
            evidence={"sampled": total, "missing": len(missing), "first_missing": missing[0]},
            elapsed_ms=elapsed_ms(),
        )


class _Int08ProductionModelsPresent:
    """All registered production models have a readable .onnx file on disk.

    A registered name with a missing or unreadable file means the next
    request silently falls back to deterministic / heuristic classification.
    Severity = `int` (local invariant we can assert).
    """

    id = "INT-08"
    name = "Production models present"
    category = Category.integrity
    severity = Severity.int

    async def run(self, ctx: "PipelineContext") -> CheckResult:
        t0 = time.monotonic()
        registry = getattr(ctx, "model_registry", None)
        if registry is None:
            return CheckResult(
                status="amber",
                detail="Intelligence layer disabled — no model registry",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        try:
            names = registry.list_production_models()
        except Exception as exc:
            return CheckResult(
                status="amber",
                detail=f"registry.list_production_models failed: {exc}",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        if not names:
            return CheckResult(
                status="green",
                detail="No production models registered",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
        unhealthy: list[dict] = []
        for name in names:
            path = registry.production_path(name)
            try:
                stat = path.stat()
                if stat.st_size == 0:
                    unhealthy.append({"model": name, "status": "empty", "path": str(path)})
            except FileNotFoundError:
                unhealthy.append({"model": name, "status": "missing", "path": str(path)})
            except OSError as exc:
                unhealthy.append({"model": name, "status": "unreadable", "path": str(path), "error": str(exc)})
        elapsed = int((time.monotonic() - t0) * 1000)
        if not unhealthy:
            return CheckResult(
                status="green",
                detail=f"{len(names)}/{len(names)} production models readable",
                evidence={"sampled": len(names)},
                elapsed_ms=elapsed,
            )
        return CheckResult(
            status="red",
            detail=f"{len(unhealthy)}/{len(names)} production model files unhealthy",
            remediation="Restore the missing .onnx file or roll back via /v1/control/intelligence/rollback",
            evidence={"sampled": len(names), "unhealthy": unhealthy},
            elapsed_ms=elapsed,
        )


register(_Int01SigningKeyLoaded())
register(_Int02SigningActive())
register(_Int03SignaturesVerify())
register(_Int04WalacorAnchoringActive())
register(_Int05AnchorRoundTrip())
register(_Int06ChainContinuity())
register(_Int07AttemptCompleteness())
register(_Int08ProductionModelsPresent())
