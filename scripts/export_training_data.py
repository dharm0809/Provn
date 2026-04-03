#!/usr/bin/env python3
"""Export execution records from Walacor API for unified model training.

Usage:
    python scripts/export_training_data.py [--output data/training_records.json]

Connects to Walacor sandbox, pulls all execution records (ETId 9000011),
tool events (9000013), and attempts (9000012) via getcomplex API.
Outputs a JSON file with raw records + analysis summary.
"""

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Walacor sandbox defaults
DEFAULT_SERVER = "https://sandbox.walacor.com/api"
DEFAULT_USER = "sonamsingh6658"
DEFAULT_PASS = "Walacor@123"
EXEC_ETID = 9000011
ATTEMPT_ETID = 9000012
TOOL_ETID = 9000013


async def authenticate(http: httpx.AsyncClient, server: str, user: str, pwd: str) -> str:
    """Authenticate with Walacor, return Bearer token."""
    resp = await http.post(
        f"{server}/auth/login",
        json={"userName": user, "password": pwd},
    )
    resp.raise_for_status()
    token = resp.json()["api_token"]
    log.info("Authenticated as %s", user)
    return token


async def fetch_all_records(
    http: httpx.AsyncClient, server: str, token: str, etid: int, label: str,
) -> list[dict]:
    """Fetch all records from a Walacor table via getcomplex pagination."""
    url = f"{server}/query/getcomplex"
    headers = {"Authorization": token, "Content-Type": "application/json", "ETId": str(etid)}
    all_records = []
    page_size = 200
    skip = 0

    while True:
        pipeline = [
            {"$sort": {"timestamp": -1}},
            {"$skip": skip},
            {"$limit": page_size},
        ]
        resp = await http.post(url, json=pipeline, headers=headers)
        if resp.status_code == 401:
            log.warning("Token expired during fetch, re-auth needed")
            break
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", []) if isinstance(body, dict) else body if isinstance(body, list) else []

        if not data:
            break

        all_records.extend(data)
        log.info("  %s: fetched %d records (total: %d)", label, len(data), len(all_records))
        skip += page_size

        if len(data) < page_size:
            break

    return all_records


def parse_metadata(record: dict) -> dict:
    """Parse metadata_json string back to dict if present."""
    mj = record.get("metadata_json")
    if mj and isinstance(mj, str):
        try:
            record["metadata"] = json.loads(mj)
        except (json.JSONDecodeError, ValueError):
            record["metadata"] = {}
    elif mj and isinstance(mj, dict):
        record["metadata"] = mj
    # Strip Walacor internal fields
    for k in ("_id", "ORGId", "UID", "IsDeleted", "SV", "LastModifiedBy", "CreatedBy"):
        record.pop(k, None)
    return record


def analyze_records(records: list[dict]) -> dict:
    """Analyze record quality and produce a summary."""
    summary = {
        "total_records": len(records),
        "providers": Counter(),
        "models": Counter(),
        "issues": defaultdict(list),
        "field_coverage": defaultdict(int),
        "intent_distribution": Counter(),
        "prompt_quality": {"has_user_question": 0, "concat_all": 0, "empty": 0},
        "usage_quality": {"has_tokens": 0, "missing_tokens": 0, "zero_tokens": 0},
    }

    for r in records:
        provider = r.get("provider", "unknown")
        model = r.get("model_id", "unknown")
        summary["providers"][provider] += 1
        summary["models"][model] += 1

        # Check field coverage
        for field in ("execution_id", "model_id", "provider", "prompt_text",
                       "response_content", "thinking_content", "session_id",
                       "timestamp", "policy_result", "latency_ms"):
            if r.get(field) not in (None, "", 0):
                summary["field_coverage"][field] += 1

        meta = r.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, ValueError):
                meta = {}

        # Intent distribution
        intent = meta.get("_intent", "none")
        summary["intent_distribution"][intent] += 1

        # Prompt quality: does prompt_text look like full conversation vs single question?
        pt = r.get("prompt_text", "") or ""
        audit = meta.get("walacor_audit", {})
        uq = audit.get("user_question", "")

        if not pt:
            summary["prompt_quality"]["empty"] += 1
        elif uq and uq != pt and len(pt) > len(uq) * 2:
            summary["prompt_quality"]["concat_all"] += 1
        else:
            summary["prompt_quality"]["has_user_question"] += 1

        # Usage quality
        usage = meta.get("token_usage", {})
        if not usage:
            summary["usage_quality"]["missing_tokens"] += 1
        elif usage.get("total_tokens", 0) == 0 and usage.get("prompt_tokens", 0) == 0:
            summary["usage_quality"]["zero_tokens"] += 1
        else:
            summary["usage_quality"]["has_tokens"] += 1

        # Specific issues per record
        eid = r.get("execution_id", "?")[:12]
        if not r.get("model_id"):
            summary["issues"]["missing_model_id"].append(eid)
        if not r.get("provider"):
            summary["issues"]["missing_provider"].append(eid)
        if not r.get("response_content") and not r.get("thinking_content"):
            summary["issues"]["empty_response"].append(eid)
        if pt and "\n" in pt and len(pt) > 500:
            summary["issues"]["prompt_is_full_conversation"].append(eid)

    # Convert Counters to dicts for JSON serialization
    summary["providers"] = dict(summary["providers"])
    summary["models"] = dict(summary["models"])
    summary["intent_distribution"] = dict(summary["intent_distribution"])
    summary["issues"] = {k: v[:10] for k, v in summary["issues"].items()}  # cap examples
    summary["field_coverage"] = dict(summary["field_coverage"])
    return summary


async def main():
    parser = argparse.ArgumentParser(description="Export Walacor records for training")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--output", default="data/training_records.json")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        limits=httpx.Limits(max_connections=10),
    ) as http:
        token = await authenticate(http, args.server, args.user, args.password)

        # Fetch all record types
        log.info("Fetching execution records (ETId %d)...", EXEC_ETID)
        executions = await fetch_all_records(http, args.server, token, EXEC_ETID, "executions")

        log.info("Fetching attempt records (ETId %d)...", ATTEMPT_ETID)
        attempts = await fetch_all_records(http, args.server, token, ATTEMPT_ETID, "attempts")

        log.info("Fetching tool event records (ETId %d)...", TOOL_ETID)
        tool_events = await fetch_all_records(http, args.server, token, TOOL_ETID, "tool_events")

    # Parse metadata
    executions = [parse_metadata(r) for r in executions]
    attempts = [parse_metadata(r) for r in attempts]
    tool_events = [parse_metadata(r) for r in tool_events]

    # Analyze
    log.info("\n=== Execution Records Analysis ===")
    exec_summary = analyze_records(executions)
    log.info("Total: %d records", exec_summary["total_records"])
    log.info("Providers: %s", exec_summary["providers"])
    log.info("Models: %s", exec_summary["models"])
    log.info("Prompt quality: %s", exec_summary["prompt_quality"])
    log.info("Usage quality: %s", exec_summary["usage_quality"])
    log.info("Intent distribution: %s", exec_summary["intent_distribution"])
    log.info("Field coverage: %s", exec_summary["field_coverage"])
    if exec_summary["issues"]:
        log.info("Issues found:")
        for issue, examples in exec_summary["issues"].items():
            log.info("  %s: %d occurrences", issue, len(examples))

    # Save everything
    output = {
        "export_info": {
            "server": args.server,
            "timestamp": str(__import__("datetime").datetime.now(__import__("datetime").timezone.utc)),
        },
        "executions": executions,
        "attempts": attempts,
        "tool_events": tool_events,
        "analysis": exec_summary,
    }

    output_path.write_text(json.dumps(output, indent=2, default=str))
    log.info("\nExported %d executions, %d attempts, %d tool events → %s",
             len(executions), len(attempts), len(tool_events), output_path)
    log.info("Analysis summary saved alongside records.")


if __name__ == "__main__":
    asyncio.run(main())
