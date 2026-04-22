"""Snapshot test: response shape matches snapshot.schema.json."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).parent / "snapshot.schema.json"


def test_schema_snapshot_valid():
    """The snapshot schema itself is valid JSON."""
    schema = json.loads(SCHEMA_PATH.read_text())
    assert schema["type"] == "object"
    assert "checks" in schema["properties"]


def test_response_matches_schema():
    os.environ["WALACOR_GATEWAY_API_KEYS"] = "test-key-snap"
    os.environ["WALACOR_READINESS_ENABLED"] = "true"
    from gateway.config import get_settings
    get_settings.cache_clear()

    try:
        from starlette.testclient import TestClient
        from gateway.main import create_app
        client = TestClient(create_app(), raise_server_exceptions=False)
        resp = client.get("/v1/readiness", headers={"X-API-Key": "test-key-snap"})
        assert resp.status_code == 200
        body = resp.json()

        schema = json.loads(SCHEMA_PATH.read_text())
        _validate(body, schema)
    finally:
        get_settings.cache_clear()
        os.environ.pop("WALACOR_GATEWAY_API_KEYS", None)
        os.environ.pop("WALACOR_READINESS_ENABLED", None)


def _validate(obj, schema):
    """Minimal JSON schema validation (no external deps)."""
    required = schema.get("required", [])
    for key in required:
        assert key in obj, f"Missing required key: {key}"

    props = schema.get("properties", {})
    for key, subschema in props.items():
        if key not in obj:
            continue
        val = obj[key]
        typ = subschema.get("type")
        if typ == "string":
            assert isinstance(val, str), f"{key} should be string"
        elif typ == "number":
            assert isinstance(val, (int, float)), f"{key} should be number"
        elif typ == "integer":
            assert isinstance(val, int), f"{key} should be int"
        elif typ == "object":
            assert isinstance(val, dict), f"{key} should be dict"
            _validate(val, subschema)
        elif typ == "array":
            assert isinstance(val, list), f"{key} should be list"
            item_schema = subschema.get("items", {})
            for item in val:
                _validate(item, item_schema)
        enum = subschema.get("enum")
        if enum is not None:
            assert val in enum, f"{key}={val!r} not in {enum}"
