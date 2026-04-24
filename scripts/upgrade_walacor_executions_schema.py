#!/usr/bin/env python3
"""Upgrade the gateway_executions schema on Walacor to include the UUIDv7
ID-pointer chain fields (record_id, previous_record_id).

Strategy:
  1. Fetch the current schema for ETId=9000021 (gateway_executions).
  2. Build a new schema definition at SV=current+1 with the extra fields.
  3. POST /schemas with the bumped SV.

If Walacor refuses in-place schema evolution, the script exits non-zero
with the server response so the operator can pivot to a new ETId.

Usage:  python scripts/upgrade_walacor_executions_schema.py
"""
from __future__ import annotations

import os
import sys

import httpx

try:
    from dotenv import load_dotenv
    for env_file in (".env", ".env.local", ".env.local-test"):
        if os.path.exists(env_file):
            load_dotenv(env_file, override=False)
except ImportError:
    pass

# Reuse the canonical field list from the setup script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup_walacor_schemas import (  # noqa: E402
    EXECUTIONS_ETID,
    EXECUTIONS_FIELDS,
)

SERVER = os.environ.get("WALACOR_SERVER", "").rstrip("/")
USERNAME = os.environ.get("WALACOR_USERNAME", "")
PASSWORD = os.environ.get("WALACOR_PASSWORD", "")

if not all([SERVER, USERNAME, PASSWORD]):
    print("ERROR: Set WALACOR_SERVER, WALACOR_USERNAME, WALACOR_PASSWORD")
    sys.exit(1)


def authenticate(client: httpx.Client) -> str:
    r = client.post(f"{SERVER}/auth/login", json={"userName": USERNAME, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["api_token"]


def current_schema_sv(client: httpx.Client, token: str, etid: int) -> int | None:
    """Best-effort probe of the current schema version. Returns None if unknown."""
    headers = {"Authorization": token, "Content-Type": "application/json", "ETId": "50"}
    r = client.post(
        f"{SERVER}/query/getcomplex",
        json=[{"$match": {"Schema.ETId": etid}}, {"$sort": {"SV": -1}}, {"$limit": 1}],
        headers=headers,
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  schema probe HTTP {r.status_code}: {r.text[:200]}")
        return None
    body = r.json()
    rows = body if isinstance(body, list) else body.get("data") or body.get("rows") or []
    if not rows:
        return None
    row = rows[0]
    sv = row.get("SV") or row.get("sv") or (row.get("Schema", {}) or {}).get("SV")
    return int(sv) if sv is not None else None


def upgrade(client: httpx.Client, token: str, etid: int, fields: list, new_sv: int) -> bool:
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "ETId": "50",
        "SV": str(new_sv),
    }
    payload = {
        "ETId": 50,
        "SV": new_sv,
        "Schema": {
            "ETId": etid,
            "TableName": "gateway_executions",
            "Family": "gateway",
            "DoSummary": True,
            "Fields": fields,
            "Indexes": [],
        },
    }
    r = client.post(f"{SERVER}/schemas", json=payload, headers=headers, timeout=30)
    print(f"  POST /schemas SV={new_sv} HTTP {r.status_code}")
    print(f"  body: {r.text[:500]}")
    if r.status_code not in (200, 201):
        return False
    try:
        body = r.json()
        if isinstance(body, dict) and body.get("success") is False:
            return False
    except ValueError:
        pass
    return True


def main() -> None:
    with httpx.Client(timeout=30.0) as client:
        print(f"Server: {SERVER}")
        token = authenticate(client)

        print(f"\nProbing current SV for ETId={EXECUTIONS_ETID} …")
        sv = current_schema_sv(client, token, EXECUTIONS_ETID)
        if sv is None:
            print("  Could not determine current SV — defaulting to 1 → trying SV=2.")
            sv = 1
        else:
            print(f"  Current SV={sv}")

        new_sv = sv + 1
        print(f"\nAttempting upgrade to SV={new_sv} with {len(EXECUTIONS_FIELDS)} fields …")
        ok = upgrade(client, token, EXECUTIONS_ETID, EXECUTIONS_FIELDS, new_sv)

        if ok:
            print("\nSchema upgrade accepted.")
            return
        print(
            "\nSchema upgrade REJECTED. Walacor may not allow in-place evolution "
            "on this ETId. Options:\n"
            "  (a) Delete ETId 9000021 and re-create via setup_walacor_schemas.py\n"
            "  (b) Allocate a new ETId for gateway_executions and point the gateway at it\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
