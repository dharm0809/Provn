"""Compliance export API — JSON, CSV, and PDF report endpoints."""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from gateway.pipeline.context import get_pipeline_context

logger = logging.getLogger(__name__)

_CSV_COLUMNS = [
    "execution_id", "timestamp", "session_id", "model_id", "provider",
    "model_attestation_id", "policy_result", "latency_ms",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "sequence_number", "record_hash",
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

    import inspect
    reader = ctx.lineage_reader

    async def _c(method, *args):
        result = method(*args)
        return await result if inspect.isawaitable(result) else result

    summary = await _c(reader.get_compliance_summary, start, end)
    executions = await _c(reader.get_execution_export, start, end)
    attestations = await _c(reader.get_attestation_summary, start, end)
    chain_report = await _c(reader.get_chain_verification_report, start, end)

    chain_integrity = {
        "sessions_verified": len(chain_report),
        "all_valid": all(r.get("valid", False) for r in chain_report),
        "sessions": chain_report,
    }

    if fmt == "csv":
        return _build_csv_response(executions, start, end)

    if fmt == "pdf":
        return await _build_pdf_response(
            summary, attestations, executions, chain_integrity, framework, start, end,
        )

    # Default: JSON
    framework_mapping = _get_framework_mapping(framework, summary, attestations, executions)

    report = {
        "report": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {"start": start, "end": end},
            "framework": framework,
        },
        "summary": summary,
        "attestations": attestations,
        "executions": executions,
        "chain_integrity": chain_integrity,
        "framework_mapping": framework_mapping,
    }
    return JSONResponse(report)


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
