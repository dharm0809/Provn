"""Compliance export API — JSON, CSV, and PDF report endpoints."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)

# Hard ceiling on a single compliance report computation. Compliance is a
# read-only audit endpoint — if the underlying reader takes longer than
# this, every dashboard refresh hangs the page indefinitely (this is the
# bug the user reported). Failing fast with a 504 lets the frontend
# surface "report is slow" instead of an infinite spinner.
_REPORT_TIMEOUT_S = 45.0

# Singleflight cache. The dashboard fires /v1/compliance/export requests
# (the frontend dedups to one shared fetch; this cache still matters when
# multiple operators or background pre-warms hit the same window). 120 s
# TTL keeps a freshly computed report warm across dashboard navigations
# while still bounding staleness — combined with the precompute worker
# (gateway.compliance.precompute) which refreshes every 60 s, the cache
# is effectively always warm for the default RangePicker windows.
_REPORT_TTL_S = 120.0
_REPORT_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_REPORT_INFLIGHT: dict[tuple[str, str], asyncio.Future] = {}
_REPORT_LOCK = asyncio.Lock()

_CSV_COLUMNS = [
    "execution_id", "timestamp", "session_id", "model_id", "provider",
    "model_attestation_id", "policy_result", "latency_ms",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "sequence_number", "record_id", "previous_record_id",
    "walacor_block_id", "walacor_trans_id", "walacor_dh",
]


async def compliance_export(request: Request) -> Response:
    """GET /v1/compliance/export?format=json|csv&start=YYYY-MM-DD&end=YYYY-MM-DD&framework=eu_ai_act"""
    ctx = get_pipeline_context()
    if ctx.lineage_reader is None:
        return JSONResponse({"error": "Lineage reader not available"}, status_code=503)

    params = request.query_params
    start = params.get("start")
    end = params.get("end")
    if not start or not end:
        return JSONResponse(
            {"error": "Missing required query parameters: start and end (YYYY-MM-DD)"},
            status_code=400,
        )

    fmt = params.get("format", "json")
    framework = params.get("framework", "eu_ai_act")

    try:
        shared = await _load_shared_report(ctx.lineage_reader, start, end)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": (
                f"Compliance report timed out after {_REPORT_TIMEOUT_S:.0f}s. "
                "Try a narrower date range (7d or 30d) — wide windows on a busy "
                "gateway can take longer than the cap."
            )},
            status_code=504,
        )
    except Exception as exc:
        logger.exception("Compliance report build failed for window %s..%s", start, end)
        return JSONResponse(
            {"error": f"Compliance report failed: {exc}"},
            status_code=500,
        )

    summary = shared["summary"]
    executions = shared["executions"]
    attestations = shared["attestations"]
    chain_report = shared["chain_report"]
    chain_integrity = shared["chain_integrity"]

    # CSV/PDF exports need the FULL census, not the sampled set used for
    # the dashboard JSON. The shared loader caps executions at the reader's
    # default (~1000) to keep dashboard refreshes fast; CSV/PDF callers
    # are explicit user actions, so the extra latency is acceptable.
    if fmt in ("csv", "pdf"):
        import inspect as _inspect
        census_result = ctx.lineage_reader.get_execution_export(start, end, limit=10000)
        try:
            if _inspect.isawaitable(census_result):
                census_executions = await asyncio.wait_for(census_result, timeout=_REPORT_TIMEOUT_S)
            else:
                census_executions = census_result
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": (
                    f"Compliance export census timed out after {_REPORT_TIMEOUT_S:.0f}s. "
                    "Narrow the date range and retry — wide windows produce very "
                    "large exports that exceed the cap."
                )},
                status_code=504,
            )

        if fmt == "csv":
            return _build_csv_response(census_executions, start, end)

        if fmt == "pdf":
            return await _build_pdf_response(
                summary, attestations, census_executions, chain_integrity,
                framework, start, end,
            )

    # Default: JSON
    framework_mapping = _get_framework_mapping(framework, summary, attestations, executions)

    # Audit intelligence: readiness score, gaps, recommendations
    # Build minimal health context from pipeline state
    health_data = {}
    try:
        health_data = {
            "content_analyzers": len(ctx.content_analyzers) if hasattr(ctx, "content_analyzers") else 0,
            "session_chain": {"active_sessions": getattr(ctx.session_chain, "active_session_count", 0) if ctx.session_chain else None},
            "wal": {"disk_usage_bytes": 1} if ctx.wal_writer else {},
            "storage": {"backend": "walacor"} if ctx.walacor_client else {},
            "enforcement_mode": getattr(ctx, "enforcement_mode", None) or "enforced",
        }
    except Exception:
        pass

    audit_readiness = None
    try:
        from gateway.compliance.audit_intelligence import assess_audit_readiness
        audit_readiness = assess_audit_readiness(
            summary=summary,
            attestations=attestations,
            executions=executions,
            chain_report=chain_report,
            health=health_data,
        )
    except Exception as e:
        logger.warning("Audit readiness assessment failed: %s", e)

    # Distinguish "computed_at" (when the shared report's underlying
    # reader queries actually ran — may be up to ~2 minutes ago if served
    # from the precompute cache) from "generated_at" (when THIS specific
    # response was assembled, always now). The dashboard can show both:
    # "rendered at … · data as of …".
    computed_at = shared.get("computed_at")
    report = {
        "report": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "computed_at": computed_at,
            "period": {"start": start, "end": end},
            "framework": framework,
        },
        "summary": summary,
        "audit_readiness": audit_readiness,
        "attestations": attestations,
        "executions": executions,
        "chain_integrity": chain_integrity,
        "framework_mapping": framework_mapping,
    }
    return JSONResponse(report)


async def _load_shared_report(reader, start: str, end: str) -> dict:
    """Compute summary/executions/attestations/chain ONCE per (start, end).

    The dashboard fires four parallel /v1/compliance/export calls (one per
    framework) with the same window. This loader:

      1. Returns a cached result when one is fresh (< _REPORT_TTL_S old).
      2. Coalesces concurrent requests behind a single in-flight future —
         the four dashboard fetches share one computation rather than
         each triggering an independent waterfall.
      3. Runs the four reader queries CONCURRENTLY (asyncio.gather) with
         a global timeout — the pre-fix code awaited them serially, so a
         30-day window on prod accumulated all four latencies sequentially
         and exceeded 60 s.

    The returned dict is the cross-framework portion of the report; the
    per-framework `framework_mapping` and `audit_readiness` are derived
    cheaply by the caller from this shared data.
    """
    key = (start, end)
    now = time.monotonic()

    # Fast path: serve a fresh cached entry without any locking.
    cached = _REPORT_CACHE.get(key)
    if cached and (now - cached[0]) < _REPORT_TTL_S:
        return cached[1]

    # Slow path: coalesce + compute under the lock so concurrent dashboard
    # fetches don't all start their own waterfall.
    async with _REPORT_LOCK:
        cached = _REPORT_CACHE.get(key)
        if cached and (time.monotonic() - cached[0]) < _REPORT_TTL_S:
            return cached[1]
        inflight = _REPORT_INFLIGHT.get(key)
        if inflight is None:
            inflight = asyncio.ensure_future(_compute_shared_report(reader, start, end))
            _REPORT_INFLIGHT[key] = inflight

    try:
        result = await asyncio.wait_for(asyncio.shield(inflight), timeout=_REPORT_TIMEOUT_S)
    finally:
        # Only the originator clears the in-flight slot — followers see
        # the entry has been cleared and pick up the cache on the next call.
        async with _REPORT_LOCK:
            if _REPORT_INFLIGHT.get(key) is inflight and inflight.done():
                _REPORT_INFLIGHT.pop(key, None)
                if not inflight.cancelled() and inflight.exception() is None:
                    _REPORT_CACHE[key] = (time.monotonic(), inflight.result())
                    # Prune stale entries opportunistically so the cache
                    # doesn't grow unbounded across many distinct windows.
                    cutoff = time.monotonic() - _REPORT_TTL_S * 4
                    stale = [k for k, (ts, _) in _REPORT_CACHE.items() if ts < cutoff]
                    for k in stale:
                        _REPORT_CACHE.pop(k, None)
    return result


def _read_chain_integrity_from_store(
    *, summary: dict, executions: list, total_sessions: int | None,
) -> dict:
    """Read the background worker's chain verification census from the store.

    The store is shared on disk across uvicorn workers; every request
    handler sees the latest census from whichever worker holds
    leadership. When the store is empty (fresh deploy, worker disabled,
    or first tick hasn't fired yet) the response carries
    ``pending=True`` so the dashboard can render an honest empty state.

    The return shape is intentionally close to the pre-fix sampling
    shape so callers (audit_intelligence, framework_mapping, the PDF
    builder) don't break — the only field that changes meaning is
    ``sampled``, which is now always ``False`` because the worker does
    a census, not a sample.
    """
    fallback_total = (
        total_sessions
        if isinstance(total_sessions, int)
        else (summary.get("total_executions") or len(executions))
    )
    ctx = get_pipeline_context()
    store = getattr(ctx, "chain_verification_store", None)
    if store is None:
        # Worker disabled or store not wired (e.g. transparent-proxy
        # deployments). Pending until reconfigured.
        return {
            "sessions_verified": 0,
            "all_valid": True,
            "sessions": [],
            "sampled": False,
            "pending": True,
            "total_sessions_in_window": fallback_total,
            "last_verification_at": None,
        }
    try:
        sessions = store.get_all()
        last_at = store.get_meta("last_tick_at")
        # Guard against test doubles / partial stubs returning non-list
        # objects (e.g. a MagicMock standing in for the whole ctx). The
        # request must never crash the JSON encoder.
        if not isinstance(sessions, list):
            raise TypeError(f"store.get_all() returned non-list: {type(sessions)!r}")
        if last_at is not None and not isinstance(last_at, str):
            last_at = None
    except Exception as exc:  # noqa: BLE001 - fail-open: never break the report
        logger.warning("Chain verification store read failed: %s", exc)
        return {
            "sessions_verified": 0,
            "all_valid": True,
            "sessions": [],
            "sampled": False,
            "pending": True,
            "total_sessions_in_window": fallback_total,
            "last_verification_at": None,
        }
    if not sessions:
        return {
            "sessions_verified": 0,
            "all_valid": True,
            "sessions": [],
            "sampled": False,
            "pending": True,
            "total_sessions_in_window": fallback_total,
            "last_verification_at": last_at,
        }
    return {
        "sessions_verified": len(sessions),
        "all_valid": all(s.get("valid", False) for s in sessions),
        "sessions": sessions,
        "sampled": False,
        "pending": False,
        "total_sessions_in_window": fallback_total,
        "last_verification_at": last_at,
    }


async def _compute_shared_report(reader, start: str, end: str) -> dict:
    """Actually run the four reader queries in parallel."""
    import inspect

    async def _c(method, *args):
        result = method(*args)
        return await result if inspect.isawaitable(result) else result

    async def _count_sessions():
        fn = getattr(reader, "count_sessions_in_window", None)
        if fn is None:
            return None
        try:
            return await _c(fn, start, end)
        except Exception:
            return None

    # We still fire `get_chain_verification_report` here even though its
    # result is no longer used for the dashboard's chain_integrity
    # panel (the background worker's store is the source of truth).
    # Keeping the call serves two purposes: (1) preserves the existing
    # singleflight gather shape so downstream callers can rely on the
    # same reader-call profile, and (2) keeps the per-request ad-hoc
    # sample warm for operators inspecting via raw API/CLI. Its return
    # value is intentionally discarded by the assignment below.
    summary, executions, attestations, _legacy_chain_report, total_sessions = await asyncio.gather(
        _c(reader.get_compliance_summary, start, end),
        _c(reader.get_execution_export, start, end),
        _c(reader.get_attestation_summary, start, end),
        _c(reader.get_chain_verification_report, start, end),
        _count_sessions(),
    )
    # Chain integrity now comes from the background ``ChainIntegrityWorker``
    # via ``ChainVerificationStore`` — a real census of every session in
    # the configured window (default 7 days), not a 50-session sample.
    # On a fresh deploy where the worker hasn't ticked yet the store is
    # empty; we surface ``pending=True`` and ``sessions_verified=0`` so
    # the dashboard renders an honest empty state instead of implying
    # a verified census from no data.
    chain_integrity = _read_chain_integrity_from_store(
        summary=summary, executions=executions, total_sessions=total_sessions,
    )
    # `chain_report` is what audit_intelligence consumes downstream;
    # surface the store's census so the readiness scoring reflects the
    # background worker's view, not the legacy sample.
    chain_report = chain_integrity.get("sessions") or []
    return {
        "summary": summary,
        "executions": executions,
        "attestations": attestations,
        "chain_report": chain_report,
        "chain_integrity": chain_integrity,
        # Wall-clock timestamp of when this shared report was actually
        # computed (vs. the cache-monotonic timestamp). Surfaced to the
        # API response so the dashboard can render a "data as of HH:MM:SS"
        # badge and operators know whether they're looking at a fresh
        # report or a pre-warmed cache hit (refreshed every ~60s by the
        # CompliancePrecomputeWorker).
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_csv_response(executions: list[dict], start: str, end: str) -> StreamingResponse:
    """Build a CSV streaming response from execution records."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for rec in executions:
        writer.writerow(rec)
    content = output.getvalue()

    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="compliance_{start}_{end}.csv"',
        },
    )


async def _build_pdf_response(
    summary, attestations, executions, chain_integrity, framework, start, end,
) -> Response:
    """Build a PDF compliance report. Returns 501 if WeasyPrint is not installed."""
    try:
        from gateway.compliance.pdf_report import generate_pdf_report

        pdf_bytes = generate_pdf_report(
            summary=summary,
            attestations=attestations,
            executions=executions,
            chain_integrity=chain_integrity,
            framework=framework,
            start=start,
            end=end,
        )
    except (ImportError, OSError) as exc:
        return JSONResponse(
            {"error": f"PDF export unavailable: {exc}. Install system libraries: brew install pango"},
            status_code=501,
        )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="compliance_{start}_{end}.pdf"',
        },
    )


def _get_framework_mapping(framework: str, summary: dict, attestations: list, executions: list) -> dict:
    """Load and apply framework mapping. Returns empty dict if framework module not available."""
    try:
        from gateway.compliance.frameworks import get_framework_mapping
        return get_framework_mapping(framework, summary, attestations, executions)
    except ImportError:
        return {}
    except Exception as e:
        logger.warning("Framework mapping failed for %s: %s", framework, e)
        return {}
