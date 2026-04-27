#!/usr/bin/env python3
"""Create Walacor schemas for the Gateway audit trail.

Usage:
  python scripts/setup_walacor_schemas.py

Reads from environment (or .env):
  WALACOR_SERVER    — e.g. https://sandbox.walacor.com/api
  WALACOR_USERNAME
  WALACOR_PASSWORD

Creates 3 schemas via POST /schemas (ETId=50 system envelope):
  ETId 9000031  gateway_executions   (34 fields — includes record_id chain)
  ETId 9000032  gateway_attempts     (10 fields)
  ETId 9000033  gateway_tool_events  (20 fields)

After running, update your .env:
  WALACOR_EXECUTIONS_ETID=9000031
  WALACOR_ATTEMPTS_ETID=9000032
  WALACOR_TOOL_EVENTS_ETID=9000033
"""

import os
import sys
import httpx

# ── Load .env if present ────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    for env_file in (".env", ".env.local", ".env.local-test"):
        if os.path.exists(env_file):
            load_dotenv(env_file, override=False)
except ImportError:
    pass

SERVER = os.environ.get("WALACOR_SERVER", "").rstrip("/")
USERNAME = os.environ.get("WALACOR_USERNAME", "")
PASSWORD = os.environ.get("WALACOR_PASSWORD", "")

if not all([SERVER, USERNAME, PASSWORD]):
    print("ERROR: Set WALACOR_SERVER, WALACOR_USERNAME, WALACOR_PASSWORD")
    sys.exit(1)

# ── New ETIds ───────────────────────────────────────────────────────────────
EXECUTIONS_ETID = 9000031
ATTEMPTS_ETID = 9000032
TOOL_EVENTS_ETID = 9000033
AGENT_RUN_MANIFESTS_ETID = 9000034   # Pillar 4 — signed agent-run manifests

# ── Schema definitions ──────────────────────────────────────────────────────
# POST /schemas uses: {ETId: 50, SV: 1, Schema: {ETId: <your_id>, Fields: [...]}}
# DataTypes: TEXT, INTEGER, BOOLEAN, DECIMAL, DATETIME

EXECUTIONS_FIELDS = [
    # Core identity
    {"FieldName": "execution_id",            "DataType": "TEXT", "MaxLength": 255, "Required": True},
    {"FieldName": "model_attestation_id",    "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "model_id",                "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "provider",                "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "tenant_id",               "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "gateway_id",              "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "timestamp",               "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "user",                    "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "session_id",              "DataType": "TEXT", "MaxLength": 255, "Required": False},
    # Policy
    {"FieldName": "policy_version",          "DataType": "INTEGER", "Required": False},
    {"FieldName": "policy_result",           "DataType": "TEXT", "MaxLength": 255, "Required": False},
    # Content
    {"FieldName": "prompt_text",             "DataType": "TEXT", "MaxLength": 65535, "Required": False},
    {"FieldName": "response_content",        "DataType": "TEXT", "MaxLength": 65535, "Required": False},
    {"FieldName": "thinking_content",        "DataType": "TEXT", "MaxLength": 65535, "Required": False},
    {"FieldName": "metadata_json",           "DataType": "TEXT", "MaxLength": 65535, "Required": False},
    # Provider details
    {"FieldName": "provider_request_id",     "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "model_hash",              "DataType": "TEXT", "MaxLength": 255, "Required": False},
    # Token usage
    {"FieldName": "prompt_tokens",           "DataType": "INTEGER", "Required": False},
    {"FieldName": "completion_tokens",       "DataType": "INTEGER", "Required": False},
    {"FieldName": "total_tokens",            "DataType": "INTEGER", "Required": False},
    {"FieldName": "cached_tokens",           "DataType": "INTEGER", "Required": False},
    {"FieldName": "cache_creation_tokens",   "DataType": "INTEGER", "Required": False},
    {"FieldName": "cache_hit",               "DataType": "BOOLEAN", "Required": False},
    {"FieldName": "latency_ms",              "DataType": "DECIMAL", "Required": False},
    # Session chain (UUIDv7 ID-pointer chain + legacy Merkle fields during transition)
    {"FieldName": "sequence_number",         "DataType": "INTEGER", "Required": False},
    {"FieldName": "record_id",               "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "previous_record_id",      "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "record_hash",             "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "previous_record_hash",    "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "record_signature",        "DataType": "TEXT", "MaxLength": 512, "Required": False},
    # Tool awareness
    {"FieldName": "tool_strategy",           "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "tool_count",              "DataType": "INTEGER", "Required": False},
    # Routing
    {"FieldName": "variant_id",              "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "retry_of",               "DataType": "TEXT", "MaxLength": 255, "Required": False},
]

ATTEMPTS_FIELDS = [
    {"FieldName": "request_id",     "DataType": "TEXT", "MaxLength": 255, "Required": True},
    {"FieldName": "timestamp",      "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "tenant_id",      "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "path",           "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "disposition",    "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "status_code",    "DataType": "INTEGER", "Required": False},
    {"FieldName": "provider",       "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "model_id",       "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "execution_id",   "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "user",           "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "reason",         "DataType": "TEXT", "MaxLength": 1024, "Required": False},
]

TOOL_EVENTS_FIELDS = [
    # Identity
    {"FieldName": "event_id",           "DataType": "TEXT", "MaxLength": 255, "Required": True},
    {"FieldName": "execution_id",       "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "session_id",         "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "tenant_id",          "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "gateway_id",         "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "prompt_id",          "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "timestamp",          "DataType": "TEXT", "MaxLength": 255, "Required": False},
    # Tool details
    {"FieldName": "tool_name",          "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "tool_type",          "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "tool_source",        "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "mcp_server_name",    "DataType": "TEXT", "MaxLength": 255, "Required": False},
    # Input/output
    {"FieldName": "input_data",         "DataType": "TEXT", "MaxLength": 65535, "Required": False},
    {"FieldName": "input_hash",         "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "output_data",        "DataType": "TEXT", "MaxLength": 65535, "Required": False},
    {"FieldName": "output_hash",        "DataType": "TEXT", "MaxLength": 255, "Required": False},
    {"FieldName": "sources",            "DataType": "TEXT", "MaxLength": 65535, "Required": False},
    # Execution metadata
    {"FieldName": "duration_ms",        "DataType": "DECIMAL", "Required": False},
    {"FieldName": "iteration",          "DataType": "INTEGER", "Required": False},
    {"FieldName": "is_error",           "DataType": "BOOLEAN", "Required": False},
    {"FieldName": "content_analysis",   "DataType": "TEXT", "MaxLength": 65535, "Required": False},
]

AGENT_RUN_MANIFEST_FIELDS = [
    # Identity
    {"FieldName": "run_id",                   "DataType": "TEXT", "MaxLength": 255,   "Required": True},
    {"FieldName": "tenant_id",                "DataType": "TEXT", "MaxLength": 255,   "Required": False},
    {"FieldName": "trace_id",                 "DataType": "TEXT", "MaxLength": 255,   "Required": False},
    # Caller — keep the JSON blob; queries can JSON-extract
    {"FieldName": "caller_identity",          "DataType": "TEXT", "MaxLength": 4096,  "Required": False},
    # Framework guess (rule-based v1; ONNX in v2)
    {"FieldName": "framework_guess",          "DataType": "TEXT", "MaxLength": 1024,  "Required": False},
    # Lifecycle
    {"FieldName": "start_ts",                 "DataType": "TEXT", "MaxLength": 64,    "Required": False},
    {"FieldName": "end_ts",                   "DataType": "TEXT", "MaxLength": 64,    "Required": False},
    {"FieldName": "end_reason",               "DataType": "TEXT", "MaxLength": 64,    "Required": False},
    # Aggregated counts (also derivable from the lists, but cheap to filter on)
    {"FieldName": "llm_call_count",           "DataType": "INTEGER",                  "Required": False},
    {"FieldName": "tool_event_count",         "DataType": "INTEGER",                  "Required": False},
    # The actual references — JSON arrays so you can keep schema flat. Larger
    # bound because a long ReAct loop can carry dozens of tool events.
    {"FieldName": "llm_calls",                "DataType": "TEXT", "MaxLength": 65535, "Required": False},
    {"FieldName": "reconstructed_tool_events","DataType": "TEXT", "MaxLength": 65535, "Required": False},
    # Tamper-evident integrity
    {"FieldName": "message_chain_hash",       "DataType": "TEXT", "MaxLength": 128,   "Required": False},
    {"FieldName": "signature",                "DataType": "TEXT", "MaxLength": 512,   "Required": False},
    {"FieldName": "signed_at",                "DataType": "TEXT", "MaxLength": 64,    "Required": False},
    {"FieldName": "signing_key_id",           "DataType": "TEXT", "MaxLength": 255,   "Required": False},
]

SCHEMAS = [
    {"etid": EXECUTIONS_ETID,            "table": "gateway_executions",        "fields": EXECUTIONS_FIELDS},
    {"etid": ATTEMPTS_ETID,              "table": "gateway_attempts",          "fields": ATTEMPTS_FIELDS},
    {"etid": TOOL_EVENTS_ETID,           "table": "gateway_tool_events",       "fields": TOOL_EVENTS_FIELDS},
    {"etid": AGENT_RUN_MANIFESTS_ETID,   "table": "gateway_agent_run_manifests", "fields": AGENT_RUN_MANIFEST_FIELDS},
]


# ── API ─────────────────────────────────────────────────────────────────────

def authenticate(client: httpx.Client) -> str:
    resp = client.post(f"{SERVER}/auth/login", json={"userName": USERNAME, "password": PASSWORD})
    resp.raise_for_status()
    return resp.json()["api_token"]


def create_schema(client: httpx.Client, token: str, etid: int, table_name: str, fields: list) -> bool:
    """Create a schema via POST /schemas with ETId=50 system envelope."""
    print(f"\n{'='*60}")
    print(f"Creating: {table_name} (ETId={etid}, {len(fields)} fields)")

    # Walacor schema creation format:
    # Outer ETId=50 (system schema envelope), inner Schema.ETId = your ID
    payload = {
        "ETId": 50,
        "SV": 1,
        "Schema": {
            "ETId": etid,
            "TableName": table_name,
            "Family": "gateway",
            "DoSummary": True,
            "Fields": fields,
            "Indexes": [],
        },
    }

    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "ETId": "50",
        "SV": "1",
    }

    resp = client.post(f"{SERVER}/schemas", json=payload, headers=headers, timeout=30)

    if resp.status_code == 200:
        body = resp.json()
        if isinstance(body, dict) and body.get("success") is False:
            error = body.get("errors", body.get("error", ""))
            error_str = str(error).lower()
            if "already" in error_str or "exist" in error_str or "duplicate" in error_str:
                print(f"  => Already exists (OK)")
                return True
            print(f"  => REJECTED: {error}")
            return False
        print(f"  => Created successfully")
        return True
    elif resp.status_code == 400:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        errors = body.get("errors", [])
        error_str = str(errors).lower()
        if "already" in error_str or "exist" in error_str or "duplicate" in error_str:
            print(f"  => Already exists (OK)")
            return True
        print(f"  => HTTP 400: {errors}")
        return False
    else:
        print(f"  => HTTP {resp.status_code}: {resp.text[:300]}")
        return False


def verify_schema(client: httpx.Client, token: str, etid: int, table_name: str) -> bool:
    headers = {"Authorization": token, "Content-Type": "application/json", "ETId": str(etid)}
    resp = client.post(
        f"{SERVER}/query/getcomplex",
        json=[{"$match": {}}, {"$limit": 1}],
        headers=headers, timeout=10,
    )
    if resp.status_code == 200:
        print(f"  Verified: ETId={etid} ({table_name}) is queryable")
        return True
    else:
        print(f"  WARNING: ETId={etid} query returned {resp.status_code}")
        return False


def main():
    print(f"Walacor Schema Setup")
    print(f"Server: {SERVER}")
    print(f"User:   {USERNAME}")

    client = httpx.Client(timeout=30.0)

    print("\nAuthenticating...")
    try:
        token = authenticate(client)
        print(f"  => OK")
    except Exception as e:
        print(f"  => FAILED: {e}")
        sys.exit(1)

    results = []
    for s in SCHEMAS:
        ok = create_schema(client, token, s["etid"], s["table"], s["fields"])
        results.append((s["table"], s["etid"], ok))

    print(f"\n{'='*60}")
    print("Verifying...")
    for table, etid, _ in results:
        verify_schema(client, token, etid, table)

    print(f"\n{'='*60}")
    all_ok = all(ok for _, _, ok in results)
    for table, etid, ok in results:
        print(f"  {table:30s} ETId={etid}  [{'OK' if ok else 'FAILED'}]")

    if all_ok:
        print(f"\nUpdate your .env:")
        print(f"  WALACOR_EXECUTIONS_ETID={EXECUTIONS_ETID}")
        print(f"  WALACOR_ATTEMPTS_ETID={ATTEMPTS_ETID}")
        print(f"  WALACOR_TOOL_EVENTS_ETID={TOOL_EVENTS_ETID}")
    else:
        print(f"\nSome schemas failed — check errors above.")
        sys.exit(1)

    client.close()


if __name__ == "__main__":
    main()
