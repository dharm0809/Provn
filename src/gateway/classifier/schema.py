"""Walacor schema validation — ensures every record has correct field types before write.

Validates execution records, tool events, and attempts against the known
Walacor schema. Fields that fail validation get logged and corrected.
Unknown fields get flagged for review. This is the last line of defense
before data hits Walacor.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Walacor Execution Record Schema (ETId 9000011) ───────────────────

EXECUTION_SCHEMA: dict[str, dict] = {
    # Required fields (must be present and non-null)
    "execution_id":         {"type": str,   "required": True},
    "tenant_id":            {"type": str,   "required": True},
    "gateway_id":           {"type": str,   "required": True},
    "timestamp":            {"type": str,   "required": True},
    "policy_version":       {"type": int,   "required": True},
    "policy_result":        {"type": str,   "required": True},
    # Important fields (should be present, default if missing)
    "model_attestation_id": {"type": str,   "required": False, "default": ""},
    "model_id":             {"type": str,   "required": False, "default": "unknown"},
    "provider":             {"type": str,   "required": False, "default": "unknown"},
    "user":                 {"type": str,   "required": False, "default": None},
    "session_id":           {"type": str,   "required": False, "default": None},
    "prompt_text":          {"type": str,   "required": False, "default": None},
    "response_content":     {"type": str,   "required": False, "default": None},
    "provider_request_id":  {"type": str,   "required": False, "default": None},
    "model_hash":           {"type": str,   "required": False, "default": None},
    "thinking_content":     {"type": str,   "required": False, "default": None},
    # Numeric fields (must be correct type)
    "prompt_tokens":        {"type": int,   "required": False, "default": 0},
    "completion_tokens":    {"type": int,   "required": False, "default": 0},
    "total_tokens":         {"type": int,   "required": False, "default": 0},
    "latency_ms":           {"type": float, "required": False, "default": None},
    # Boolean fields
    "cache_hit":            {"type": bool,  "required": False, "default": False},
    "cached_tokens":        {"type": int,   "required": False, "default": 0},
    "cache_creation_tokens":{"type": int,   "required": False, "default": 0},
    # Optional fields
    "retry_of":             {"type": str,   "required": False, "default": None},
    "variant_id":           {"type": str,   "required": False, "default": None},
    "metadata_json":        {"type": str,   "required": False, "default": None},
    "estimated_cost_usd":   {"type": float, "required": False, "default": None},
    # Chain fields
    "sequence_number":      {"type": int,   "required": False, "default": None},
    "record_hash":          {"type": str,   "required": False, "default": None},
    "previous_record_hash": {"type": str,   "required": False, "default": None},
}

# ── Walacor Tool Event Schema (ETId 9000013) ─────────────────────────

TOOL_EVENT_SCHEMA: dict[str, dict] = {
    "event_id":             {"type": str,   "required": True},
    "execution_id":         {"type": str,   "required": True},
    "tenant_id":            {"type": str,   "required": True},
    "gateway_id":           {"type": str,   "required": True},
    "timestamp":            {"type": str,   "required": True},
    "tool_name":            {"type": str,   "required": False, "default": "unknown"},
    "tool_type":            {"type": str,   "required": False, "default": "function"},
    "tool_source":          {"type": str,   "required": False, "default": "gateway"},
    "session_id":           {"type": str,   "required": False, "default": None},
    "input_data":           {"type": str,   "required": False, "default": None},
    "input_hash":           {"type": str,   "required": False, "default": None},
    "output_data":          {"type": str,   "required": False, "default": None},
    "output_hash":          {"type": str,   "required": False, "default": None},
    "sources":              {"type": str,   "required": False, "default": None},
    "duration_ms":          {"type": float, "required": False, "default": None},
    "iteration":            {"type": int,   "required": False, "default": None},
    "is_error":             {"type": bool,  "required": False, "default": False},
    "content_analysis":     {"type": str,   "required": False, "default": None},
}

# ── Walacor Attempt Schema (ETId 9000012) ─────────────────────────────

ATTEMPT_SCHEMA: dict[str, dict] = {
    "request_id":           {"type": str,   "required": True},
    "timestamp":            {"type": str,   "required": True},
    "tenant_id":            {"type": str,   "required": True},
    "disposition":          {"type": str,   "required": True},
    "status_code":          {"type": int,   "required": True},
    "path":                 {"type": str,   "required": False, "default": ""},
    "provider":             {"type": str,   "required": False, "default": None},
    "model_id":             {"type": str,   "required": False, "default": None},
    "execution_id":         {"type": str,   "required": False, "default": None},
    "user":                 {"type": str,   "required": False, "default": None},
}


def validate_record(record: dict[str, Any], schema: dict[str, dict], record_type: str = "execution") -> dict[str, Any]:
    """Validate and fix a record against the schema.

    - Missing required fields → logged as ERROR, default applied
    - Wrong type fields → coerced to correct type, logged as WARNING
    - Unknown fields → left as-is (Walacor client strips them)
    - Returns the cleaned record
    """
    issues: list[str] = []
    cleaned = dict(record)

    for field_name, field_spec in schema.items():
        value = cleaned.get(field_name)
        expected_type = field_spec["type"]
        required = field_spec["required"]
        default = field_spec.get("default")

        # Missing field
        if value is None and field_name not in cleaned:
            if required:
                issues.append(f"MISSING required field: {field_name}")
                cleaned[field_name] = default if default is not None else ""
            continue

        # Null value for required field
        if value is None and required:
            issues.append(f"NULL required field: {field_name}")
            cleaned[field_name] = default if default is not None else ""
            continue

        if value is None:
            continue

        # Type check and coercion
        if not isinstance(value, expected_type):
            try:
                if expected_type == int:
                    cleaned[field_name] = int(float(value)) if value else 0
                elif expected_type == float:
                    cleaned[field_name] = float(value) if value else 0.0
                elif expected_type == str:
                    cleaned[field_name] = str(value)
                elif expected_type == bool:
                    cleaned[field_name] = bool(value)
                issues.append(f"TYPE coerced: {field_name} {type(value).__name__} → {expected_type.__name__}")
            except (ValueError, TypeError):
                issues.append(f"TYPE FAILED: {field_name} {type(value).__name__} cannot coerce to {expected_type.__name__}")
                cleaned[field_name] = default

    if issues:
        eid = cleaned.get("execution_id") or cleaned.get("event_id") or cleaned.get("request_id") or "?"
        logger.warning(
            "Schema validation (%s %s): %d issue(s): %s",
            record_type, eid, len(issues), "; ".join(issues),
        )

    return cleaned


def validate_execution(record: dict[str, Any]) -> dict[str, Any]:
    """Validate an execution record before Walacor write."""
    return validate_record(record, EXECUTION_SCHEMA, "execution")


def validate_tool_event(record: dict[str, Any]) -> dict[str, Any]:
    """Validate a tool event record before Walacor write."""
    return validate_record(record, TOOL_EVENT_SCHEMA, "tool_event")


def validate_attempt(record: dict[str, Any]) -> dict[str, Any]:
    """Validate an attempt record before Walacor write."""
    return validate_record(record, ATTEMPT_SCHEMA, "attempt")
