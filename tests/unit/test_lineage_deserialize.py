"""Tests for the Walacor lineage record deserializer (C9 + C10).

Two behaviour changes:

* **C9** — `CreatedAt`, `UpdatedAt`, `EId` are stripped from the top-level
  record returned to the dashboard. These are Walacor envelope/bookkeeping
  fields that no JSX reads; before the fix they cluttered every record body.
  `EId` is still re-attached as `_walacor_eid` by the caller (it's needed
  for the envelope-drawer round-trip), so the strip only affects the
  default operator view.

* **C10** — `metadata._internal` is PRESERVED (not promoted to top level,
  not stripped). Internal classifier flags like `_intent` and
  `schema_mapper_*` live under that nested key on records the write side
  produces. Operators with debug=true can still see classifier reasoning;
  the simple session view ignores it.
"""

from __future__ import annotations

import json

from gateway.lineage.walacor_reader import _deserialize_record


def test_deserialize_strips_walacor_internal_envelope_fields():
    """C9: CreatedAt, UpdatedAt, EId removed alongside the existing Walacor
    internals (_id, ORGId, UID, IsDeleted, SV, LastModifiedBy)."""
    raw = {
        "execution_id": "ex-1",
        "model_id": "llama3.1:8b",
        # Walacor bookkeeping that used to leak into the dashboard view:
        "CreatedAt": "2026-05-12T10:00:00",
        "UpdatedAt": "2026-05-12T10:01:00",
        "EId": 12345,
        "_id": "obj-id",
        "ORGId": "org-1",
        "UID": "uid-1",
        "IsDeleted": False,
        "SV": 1,
        "LastModifiedBy": "system",
    }
    out = _deserialize_record(dict(raw))
    # Stripped (C9 + existing strip list):
    for k in ("CreatedAt", "UpdatedAt", "EId", "_id", "ORGId", "UID",
              "IsDeleted", "SV", "LastModifiedBy"):
        assert k not in out, f"{k} should be stripped"
    # User-facing fields preserved:
    assert out["execution_id"] == "ex-1"
    assert out["model_id"] == "llama3.1:8b"


def test_deserialize_preserves_metadata_internal_namespace():
    """C10: `metadata._internal` is NOT promoted (stays nested) and NOT
    stripped — operators auditing classifier reasoning can drill in. The
    simple session view ignores it because nothing in JSX reads
    `metadata._internal`."""
    raw = {
        "execution_id": "ex-2",
        "metadata_json": json.dumps({
            "request_type": "user_message",
            "_internal": {
                "_intent": "research",
                "_intent_confidence": 0.91,
                "schema_mapper_confidence": 0.98,
                "schema_mapper_mapped": 7,
            },
            "walacor_audit": {"user_question": "what is X?"},
        }),
    }
    out = _deserialize_record(dict(raw))
    meta = out.get("metadata") or {}
    # _internal must survive the deserializer untouched (still nested).
    assert "_internal" in meta
    assert meta["_internal"]["_intent"] == "research"
    assert meta["_internal"]["schema_mapper_confidence"] == 0.98
    # And the user-facing audit data is also preserved.
    assert meta["walacor_audit"]["user_question"] == "what is X?"
    # Importantly _internal must NOT be promoted to top level — keeps the
    # default operator view uncluttered.
    assert "_internal" not in out


def test_deserialize_strips_legacy_walacor_fields_alongside_c9_additions():
    """Sanity: the pre-C9 strip list (_id, ORGId, etc.) still works exactly
    as before — C9 only adds three fields to the list, doesn't remove any."""
    raw = {
        "execution_id": "ex-3",
        "_id": "should-go",
        "ORGId": "should-go",
        "UID": "should-go",
        "IsDeleted": False,
        "SV": 7,
        "LastModifiedBy": "should-go",
    }
    out = _deserialize_record(dict(raw))
    assert "execution_id" in out
    for legacy in ("_id", "ORGId", "UID", "IsDeleted", "SV", "LastModifiedBy"):
        assert legacy not in out


def test_deserialize_metadata_json_string_parsed():
    """Existing behaviour: metadata_json (JSON string) → metadata (dict).
    Confirms C9/C10 changes don't break the standard deserialization."""
    raw = {
        "execution_id": "ex-4",
        "metadata_json": json.dumps({"event_source": "openwebui_plugin"}),
    }
    out = _deserialize_record(dict(raw))
    assert "metadata_json" not in out
    assert out["metadata"]["event_source"] == "openwebui_plugin"


def test_deserialize_preserves_file_metadata_promotion():
    """Existing behaviour: file_metadata is promoted from metadata to top
    level for the dashboard. Confirms the C10 _internal-preservation didn't
    accidentally break the file_metadata promotion."""
    raw = {
        "execution_id": "ex-5",
        "metadata_json": json.dumps({
            "file_metadata": [{"filename": "x.txt", "size_bytes": 42}],
            "request_type": "user_message",
            "_internal": {"_intent": "qa"},
        }),
    }
    out = _deserialize_record(dict(raw))
    # file_metadata promoted to top level.
    assert out.get("file_metadata") == [{"filename": "x.txt", "size_bytes": 42}]
    # _internal still preserved under metadata, not promoted.
    assert out["metadata"]["_internal"]["_intent"] == "qa"


def test_deserialize_handles_missing_metadata_json():
    """When metadata_json is absent the deserializer must not crash. The
    pre-fix code worked this way; C9 changes don't regress it."""
    raw = {"execution_id": "ex-6", "CreatedAt": "2026-01-01", "EId": 99}
    out = _deserialize_record(dict(raw))
    assert out["execution_id"] == "ex-6"
    # C9 strips happen even when metadata is absent.
    assert "CreatedAt" not in out
    assert "EId" not in out
