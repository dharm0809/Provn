"""OpenWebUI event receiver — writes plugin events to JSONL and runs governance."""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.config import get_settings

logger = logging.getLogger(__name__)


def _get_log_path() -> Path:
    """Resolve the event log file path.

    Anchors to ``settings.wal_path`` so the log lives alongside the WAL
    volume (bounded, persistent, mounted in container deployments).  If
    ``wal_path`` is missing or unwritable we fall back to the system temp
    directory rather than the repo root — writing to the repo root in
    dev/prod creates an unbounded file that nothing rotates.
    """
    wal_path = ""
    try:
        wal_path = (get_settings().wal_path or "").strip()
    except Exception:
        wal_path = ""
    if wal_path:
        return Path(wal_path) / "openwebui_events.log"
    return Path(tempfile.gettempdir()) / "openwebui_events.log"


async def openwebui_events_receive(request: Request) -> JSONResponse:
    """POST /v1/openwebui/events

    Receives a JSON event payload from the OpenWebUI plugin and appends it
    as a single line to a JSONL text file.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # Server-side receipt timestamp
    body["received_at"] = datetime.now(timezone.utc).isoformat()

    log_path = _get_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(body, default=str) + "\n")
    except Exception as e:
        logger.error("Failed to write OpenWebUI event to %s: %s", log_path, e)
        return JSONResponse({"error": "Failed to write event"}, status_code=500)

    logger.info(
        "OpenWebUI event logged: type=%s chat_id=%s user=%s",
        body.get("event_type", "unknown"),
        body.get("chat_id", ""),
        body.get("user", {}).get("id", ""),
    )

    # Run governance pipeline (attestation, policy, session chain, WAL/Walacor write).
    # Runs in both full-governance and skip_governance modes — ctx.storage is
    # initialized in both paths (main.py).  When governance caches are absent
    # (skip_governance), attestation/policy steps are skipped but the execution
    # record is still written so plugin chats appear in Walacor and lineage.
    governance_result: dict = {}
    settings = get_settings()
    if settings.plugin_event_governance_enabled:
        try:
            from gateway.openwebui.governance import process_plugin_event

            governance_result = await process_plugin_event(body)
        except Exception:
            logger.warning("Plugin event governance failed", exc_info=True)

    return JSONResponse({
        "status": "ok",
        "log_file": str(log_path),
        "governance": governance_result,
    })


async def openwebui_events_list(request: Request) -> JSONResponse:
    """GET /v1/openwebui/events

    Returns logged events from the text file.  Supports query params:
      - limit  (int, default 100) — max events to return (most recent)
      - type   (str) — filter by event_type (inlet, outlet)
      - chat_id (str) — filter by chat_id
    """
    log_path = _get_log_path()

    if not log_path.exists():
        return JSONResponse({"events": [], "total": 0, "log_file": str(log_path)})

    limit = int(request.query_params.get("limit", "100"))
    event_type = request.query_params.get("type", "")
    chat_id = request.query_params.get("chat_id", "")

    events: list[dict] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and ev.get("event_type") != event_type:
                    continue
                if chat_id and ev.get("chat_id") != chat_id:
                    continue
                events.append(ev)
    except Exception as e:
        logger.error("Failed to read OpenWebUI events from %s: %s", log_path, e)
        return JSONResponse({"error": "Failed to read events"}, status_code=500)

    # Return the most recent events
    events = events[-limit:]

    return JSONResponse(
        {"events": events, "total": len(events), "log_file": str(log_path)}
    )
