"""Tests for per-key policy assignments (B.5)."""

from __future__ import annotations

import hashlib
import os
import tempfile

import pytest


@pytest.fixture
def store():
    from gateway.control.store import ControlPlaneStore

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    s = ControlPlaneStore(db_path)
    yield s
    s.close()
    os.unlink(db_path)


def test_set_and_get_key_policies(store):
    key_hash = hashlib.sha256(b"test-key").hexdigest()
    store.set_key_policies(key_hash, ["policy-1", "policy-2"])
    result = store.get_key_policies(key_hash)
    assert set(result) == {"policy-1", "policy-2"}


def test_empty_key_policies(store):
    key_hash = hashlib.sha256(b"no-policies").hexdigest()
    result = store.get_key_policies(key_hash)
    assert result == []


def test_replace_key_policies(store):
    key_hash = hashlib.sha256(b"key").hexdigest()
    store.set_key_policies(key_hash, ["p1", "p2"])
    store.set_key_policies(key_hash, ["p3"])
    result = store.get_key_policies(key_hash)
    assert result == ["p3"]


def test_remove_key_policy(store):
    key_hash = hashlib.sha256(b"key").hexdigest()
    store.set_key_policies(key_hash, ["p1", "p2"])
    removed = store.remove_key_policy(key_hash, "p1")
    assert removed is True
    result = store.get_key_policies(key_hash)
    assert "p1" not in result
    assert "p2" in result


def test_remove_nonexistent_policy(store):
    key_hash = hashlib.sha256(b"key").hexdigest()
    removed = store.remove_key_policy(key_hash, "nonexistent")
    assert removed is False


def test_list_all_assignments(store):
    h1 = hashlib.sha256(b"key1").hexdigest()
    h2 = hashlib.sha256(b"key2").hexdigest()
    store.set_key_policies(h1, ["p1"])
    store.set_key_policies(h2, ["p2", "p3"])
    all_assignments = store.list_key_policy_assignments()
    assert len(all_assignments) == 3
    api_key_hashes = {a["api_key_hash"] for a in all_assignments}
    assert h1 in api_key_hashes
    assert h2 in api_key_hashes


def test_multiple_keys_isolation(store):
    h1 = hashlib.sha256(b"key1").hexdigest()
    h2 = hashlib.sha256(b"key2").hexdigest()
    store.set_key_policies(h1, ["exclusive-1"])
    store.set_key_policies(h2, ["exclusive-2"])
    assert store.get_key_policies(h1) == ["exclusive-1"]
    assert store.get_key_policies(h2) == ["exclusive-2"]


def test_assignment_dict_fields(store):
    """Each assignment record has api_key_hash, policy_id, created_at fields."""
    h = hashlib.sha256(b"akey").hexdigest()
    store.set_key_policies(h, ["pol-x"])
    assignments = store.list_key_policy_assignments()
    assert len(assignments) == 1
    rec = assignments[0]
    assert rec["api_key_hash"] == h
    assert rec["policy_id"] == "pol-x"
    assert "created_at" in rec


def test_set_empty_policy_list_clears_all(store):
    """set_key_policies with empty list should remove all assignments for the key."""
    h = hashlib.sha256(b"clear-key").hexdigest()
    store.set_key_policies(h, ["p1", "p2"])
    store.set_key_policies(h, [])
    assert store.get_key_policies(h) == []


def test_get_policy_by_id(store):
    """get_policy returns a single policy by ID with parsed rules."""
    now = "2024-01-01T00:00:00+00:00"
    store._ensure_conn().execute(
        """INSERT INTO policies
               (policy_id, policy_name, status, enforcement_level,
                rules_json, prompt_rules_json, rag_rules_json,
                tenant_id, description, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("test-pol-id", "My Policy", "active", "blocking", "[]", "[]", "[]",
         "tenant1", "", now, now),
    )
    store._ensure_conn().commit()
    pol = store.get_policy("test-pol-id")
    assert pol is not None
    assert pol["policy_id"] == "test-pol-id"
    assert pol["policy_name"] == "My Policy"
    assert pol["rules"] == []


def test_get_policy_missing_returns_none(store):
    assert store.get_policy("does-not-exist") is None


def test_get_policies_for_key_helper_no_store():
    """_get_policies_for_key returns None when control_store is None."""
    from gateway.pipeline.orchestrator import _get_policies_for_key

    class FakeCtx:
        control_store = None

    result = _get_policies_for_key("some-key", FakeCtx())
    assert result is None


def test_get_policies_for_key_helper_no_key():
    """_get_policies_for_key returns None when api_key is falsy."""
    from gateway.pipeline.orchestrator import _get_policies_for_key

    class FakeCtx:
        control_store = object()  # non-None

    assert _get_policies_for_key(None, FakeCtx()) is None
    assert _get_policies_for_key("", FakeCtx()) is None


def test_get_policies_for_key_helper_no_assignments(store):
    """_get_policies_for_key returns None when no assignments exist for the key."""
    from gateway.pipeline.orchestrator import _get_policies_for_key

    class FakeCtx:
        control_store = store

    result = _get_policies_for_key("unassigned-key", FakeCtx())
    assert result is None


def test_get_policies_for_key_helper_with_assignment(store):
    """_get_policies_for_key returns policy list when assignments and policies exist."""
    from gateway.pipeline.orchestrator import _get_policies_for_key

    # Create a policy in the store
    store.create_policy({
        "policy_id": "pol-abc",
        "policy_name": "Test Policy",
        "tenant_id": "t1",
        "rules": [],
        "prompt_rules": [],
        "rag_rules": [],
    })

    # Assign it to a key (using SHA-256 of "my-api-key")
    api_key = "my-api-key"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    store.set_key_policies(key_hash, ["pol-abc"])

    class FakeCtx:
        control_store = store

    result = _get_policies_for_key(api_key, FakeCtx())
    assert result is not None
    assert len(result) == 1
    assert result[0]["policy_id"] == "pol-abc"
