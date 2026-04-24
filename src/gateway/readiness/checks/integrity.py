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
            rows = conn.execute(
                "SELECT record_json FROM wal_records WHERE event_type='execution' "
                "ORDER BY rowid DESC LIMIT 50"
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
            evidence={"sampled": sampled, "signed": signed, "window": "last 50 execution records"},
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
    severity = Severity.int

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

        try:
            # Only consider records old enough to have had a chance to be
            # anchored. Sandbox typically anchors within a minute; we allow
            # 2 minutes of headroom before counting an un-anchored record as
            # a failure.
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            pipeline = [
                {"$match": {"timestamp": {"$lte": cutoff}}},
                {"$sort": {"timestamp": -1}},
                {"$limit": 20},
                {"$project": {
                    "execution_id": 1,
                    "BlockId": 1, "TransId": 1, "DH": 1,
                }},
            ]
            raw = await client.query_complex(settings.walacor_executions_etid, pipeline)
        except Exception as exc:
            return CheckResult(status="amber", detail=f"Walacor query failed: {exc}", elapsed_ms=elapsed_ms())

        if not raw:
            return CheckResult(
                status="amber",
                detail="No execution records older than 2m to verify yet",
                elapsed_ms=elapsed_ms(),
            )

        sampled = len(raw)
        anchored = sum(
            1 for r in raw if r.get("BlockId") and r.get("TransId") and r.get("DH")
        )
        pct = anchored / sampled
        status = "green" if pct >= 0.95 else ("amber" if pct >= 0.5 else "red")
        return CheckResult(
            status=status,
            detail=f"{anchored}/{sampled} recent records anchored ({pct:.0%})",
            remediation=None if status == "green" else (
                "Walacor writes succeeded but the server has not produced BlockId/TransId/DH — "
                "verify sandbox/anchor worker health"
            ),
            evidence={"sampled": sampled, "anchored": anchored},
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

        if ctx.walacor_client is None or ctx.wal_writer is None:
            return CheckResult(status="amber", detail="Walacor client or WAL not available", elapsed_ms=elapsed_ms())

        try:
            conn = _open_wal_ro(ctx.wal_writer._path)  # type: ignore[attr-defined]
            rows = conn.execute(
                "SELECT record_json FROM wal_records WHERE event_type='execution' "
                "ORDER BY rowid DESC LIMIT 20"
            ).fetchall()
            conn.close()
        except Exception as exc:
            return CheckResult(status="amber", detail=f"Could not sample records: {exc}", elapsed_ms=elapsed_ms())

        anchored = []
        for (rec_json,) in rows:
            try:
                rec = json.loads(rec_json)
                if rec.get("walacor_block_id") and rec.get("walacor_trans_id") and rec.get("walacor_dh"):
                    anchored.append(rec)
            except Exception:
                pass
        if not anchored:
            return CheckResult(status="amber", detail="No anchored records available to probe", elapsed_ms=elapsed_ms())

        pick = random.choice(anchored)
        exec_id = pick.get("execution_id")
        try:
            result = await ctx.walacor_client.query_complex(
                settings.walacor_executions_etid,
                [{"$match": {"execution_id": exec_id}}, {"$limit": 1}],
            )
        except Exception as exc:
            return CheckResult(
                status="red",
                detail=f"Walacor round-trip failed: {exc}",
                remediation="Check Walacor server connectivity and auth",
                evidence={"execution_id": exec_id},
                elapsed_ms=elapsed_ms(),
            )

        if not result:
            return CheckResult(
                status="red",
                detail=f"Round-trip returned 0 records for execution_id={exec_id}",
                remediation="Record anchored locally but not found on Walacor — delivery failure",
                evidence={"execution_id": exec_id},
                elapsed_ms=elapsed_ms(),
            )

        remote = result[0]
        mismatch = []
        for field in ("walacor_block_id", "walacor_trans_id", "walacor_dh"):
            if remote.get(field) and pick.get(field) and remote.get(field) != pick.get(field):
                mismatch.append(field)
        if mismatch:
            return CheckResult(
                status="red",
                detail=f"Round-trip mismatch on {','.join(mismatch)} for execution_id={exec_id}",
                remediation="Local WAL anchor fields don't match Walacor envelope — data drift",
                evidence={"execution_id": exec_id, "mismatch": mismatch},
                elapsed_ms=elapsed_ms(),
            )
        return CheckResult(
            status="green",
            detail=f"Round-trip verified for execution_id={exec_id}",
            evidence={"execution_id": exec_id},
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


register(_Int01SigningKeyLoaded())
register(_Int02SigningActive())
register(_Int03SignaturesVerify())
register(_Int04WalacorAnchoringActive())
register(_Int05AnchorRoundTrip())
register(_Int06ChainContinuity())
register(_Int07AttemptCompleteness())
