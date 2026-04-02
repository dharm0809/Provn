"""OpenWebUI event receiver — writes all plugin events to a JSONL text file."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway.config import get_settings

logger = logging.getLogger(__name__)


def _get_log_path() -> Path:
    """Resolve the event log file path — always in the project directory."""
    return Path(__file__).resolve().parents[3] / "openwebui_events.log"


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
    return JSONResponse({"status": "ok", "log_file": str(log_path)})


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
