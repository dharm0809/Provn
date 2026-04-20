"""Unit tests for the ControlPlaneStore (SQLite CRUD for attestations, policies, budgets)."""

from __future__ import annotations

import os
import tempfile

import pytest

from gateway.control.store import ControlPlaneStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store():
    """Create a temp ControlPlaneStore, yield it, then cleanup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "control.db")
        s = ControlPlaneStore(db_path)
        yield s
        s.close()


# ---------------------------------------------------------------------------
# Schema / lifecycle
# ---------------------------------------------------------------------------

def test_schema_created_on_first_access(store: ControlPlaneStore):
    """Tables are lazily created on first operation."""
    rows = store.list_attestations()
    assert rows == []
    rows = store.list_policies()
    assert rows == []
    rows = store.list_budgets()
    assert rows == []


def test_close_and_reopen():
    """Store survives close + reopen (data persists)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "control.db")
        s = ControlPlaneStore(db_path)
        s.upsert_attestation({"model_id": "m1", "provider": "ollama", "tenant_id": "t1"})
        s.close()

        s2 = ControlPlaneStore(db_path)
        rows = s2.list_attestations()
        s2.close()
        assert len(rows) == 1
        assert rows[0]["model_id"] == "m1"


# ---------------------------------------------------------------------------
# Attestation CRUD
# ---------------------------------------------------------------------------

def test_upsert_attestation_creates_new(store: ControlPlaneStore):
    result = store.upsert_attestation({
        "model_id": "qwen3:4b",
        "provider": "ollama",
        "tenant_id": "t1",
        "status": "active",
        "notes": "test model",
    })
    assert "attestation_id" in result
    assert result["model_id"] == "qwen3:4b"
    assert result["status"] == "active"
    assert result["verification_level"] == "admin_attested"


def test_upsert_attestation_updates_on_conflict(store: ControlPlaneStore):
    """Upserting the same tenant/provider/model updates status and notes."""
    r1 = store.upsert_attestation({
        "model_id": "m1", "provider": "ollama", "tenant_id": "t1",
        "status": "active", "notes": "v1",
    })
    r2 = store.upsert_attestation({
        "model_id": "m1", "provider": "ollama", "tenant_id": "t1",
        "status": "revoked", "notes": "v2",
    })
    # Same attestation_id preserved on conflict
    assert r2["attestation_id"] == r1["attestation_id"]
    assert r2["status"] == "revoked"
    assert r2["notes"] == "v2"
    # Only one row
    assert len(store.list_attestations()) == 1


def test_list_attestations_filters_by_tenant(store: ControlPlaneStore):
    store.upsert_attestation({"model_id": "m1", "provider": "ollama", "tenant_id": "t1"})
    store.upsert_attestation({"model_id": "m2", "provider": "ollama", "tenant_id": "t2"})

    all_rows = store.list_attestations()
    assert len(all_rows) == 2

    t1_rows = store.list_attestations("t1")
    assert len(t1_rows) == 1
    assert t1_rows[0]["model_id"] == "m1"


def test_delete_attestation(store: ControlPlaneStore):
    r = store.upsert_attestation({"model_id": "m1", "provider": "ollama", "tenant_id": "t1"})
    assert store.delete_attestation(r["attestation_id"]) is True
    assert len(store.list_attestations()) == 0


def test_delete_attestation_nonexistent(store: ControlPlaneStore):
    assert store.delete_attestation("no-such-id") is False


# ---------------------------------------------------------------------------
# Policy CRUD
# ---------------------------------------------------------------------------

def test_create_policy(store: ControlPlaneStore):
    result = store.create_policy({
        "policy_name": "safety-v1",
        "enforcement_level": "blocking",
        "tenant_id": "t1",
        "description": "blocks unsafe content",
        "rules": [{"field": "toxicity", "operator": "gt", "value": "0.8"}],
    })
    assert "policy_id" in result

    rows = store.list_policies()
    assert len(rows) == 1
    assert rows[0]["policy_name"] == "safety-v1"
    assert rows[0]["rules"] == [{"field": "toxicity", "operator": "gt", "value": "0.8"}]
    assert rows[0]["prompt_rules"] == []
    assert rows[0]["rag_rules"] == []


def test_update_policy(store: ControlPlaneStore):
    r = store.create_policy({"policy_name": "p1", "tenant_id": "t1"})
    updated = store.update_policy(r["policy_id"], {
        "policy_name": "p1-updated",
        "enforcement_level": "audit_only",
        "rules": [{"field": "pii", "operator": "eq", "value": "true"}],
    })
    assert updated is True

    rows = store.list_policies()
    assert rows[0]["policy_name"] == "p1-updated"
    assert rows[0]["enforcement_level"] == "audit_only"
    assert rows[0]["rules"] == [{"field": "pii", "operator": "eq", "value": "true"}]


def test_update_policy_nonexistent(store: ControlPlaneStore):
    assert store.update_policy("no-such-id", {"policy_name": "x"}) is False


def test_update_policy_no_fields(store: ControlPlaneStore):
    r = store.create_policy({"policy_name": "p1", "tenant_id": "t1"})
    assert store.update_policy(r["policy_id"], {}) is False


def test_delete_policy(store: ControlPlaneStore):
    r = store.create_policy({"policy_name": "p1", "tenant_id": "t1"})
    assert store.delete_policy(r["policy_id"]) is True
    assert len(store.list_policies()) == 0


def test_list_policies_filters_by_tenant(store: ControlPlaneStore):
    store.create_policy({"policy_name": "p1", "tenant_id": "t1"})
    store.create_policy({"policy_name": "p2", "tenant_id": "t2"})

    assert len(store.list_policies("t1")) == 1
    assert len(store.list_policies("t2")) == 1
    assert len(store.list_policies()) == 2


# ---------------------------------------------------------------------------
# Budget CRUD
# ---------------------------------------------------------------------------

def test_upsert_budget_creates_new(store: ControlPlaneStore):
    result = store.upsert_budget({
        "tenant_id": "t1",
        "user": "alice",
        "period": "monthly",
        "max_tokens": 100000,
    })
    assert "budget_id" in result
    assert result["max_tokens"] == 100000


def test_upsert_budget_updates_on_conflict(store: ControlPlaneStore):
    r1 = store.upsert_budget({
        "tenant_id": "t1", "user": "alice", "period": "monthly", "max_tokens": 100000,
    })
    r2 = store.upsert_budget({
        "tenant_id": "t1", "user": "alice", "period": "monthly", "max_tokens": 200000,
    })
    assert r2["budget_id"] == r1["budget_id"]
    assert r2["max_tokens"] == 200000
    assert len(store.list_budgets()) == 1


def test_delete_budget(store: ControlPlaneStore):
    r = store.upsert_budget({"tenant_id": "t1", "user": "", "period": "daily", "max_tokens": 5000})
    assert store.delete_budget(r["budget_id"]) is True
    assert len(store.list_budgets()) == 0


def test_delete_budget_nonexistent(store: ControlPlaneStore):
    assert store.delete_budget("no-such-id") is False


def test_list_budgets_filters_by_tenant(store: ControlPlaneStore):
    store.upsert_budget({"tenant_id": "t1", "user": "", "period": "monthly", "max_tokens": 1000})
    store.upsert_budget({"tenant_id": "t2", "user": "", "period": "monthly", "max_tokens": 2000})

    assert len(store.list_budgets("t1")) == 1
    assert len(store.list_budgets()) == 2


# ---------------------------------------------------------------------------
# Sync-contract formatters
# ---------------------------------------------------------------------------

def test_get_attestation_proofs_format(store: ControlPlaneStore):
    store.upsert_attestation({
        "model_id": "m1", "provider": "ollama", "tenant_id": "t1",
        "status": "active", "verification_level": "admin_attested",
    })
    proofs = store.get_attestation_proofs("t1")
    assert len(proofs) == 1
    p = proofs[0]
    assert set(p.keys()) == {"attestation_id", "model_id", "provider", "status", "verification_level", "tenant_id", "model_hash"}
    assert p["model_id"] == "m1"
    assert p["status"] == "active"


def test_get_active_policies_excludes_disabled(store: ControlPlaneStore):
    store.create_policy({"policy_name": "active-p", "tenant_id": "t1", "status": "active"})
    store.create_policy({"policy_name": "disabled-p", "tenant_id": "t1", "status": "disabled"})

    active = store.get_active_policies("t1")
    assert len(active) == 1
    assert active[0]["policy_name"] == "active-p"
