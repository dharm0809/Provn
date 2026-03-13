"""Tests for content policy CRUD in control plane store."""
import pytest
import tempfile
import os
from gateway.control.store import ControlPlaneStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as td:
        s = ControlPlaneStore(os.path.join(td, "test.db"))
        yield s


def test_upsert_content_policy(store):
    p = store.upsert_content_policy(
        tenant_id="t1", analyzer_id="walacor.pii.v1",
        category="credit_card", action="block")
    assert p["analyzer_id"] == "walacor.pii.v1"
    assert p["action"] == "block"
    assert "id" in p


def test_upsert_content_policy_idempotent(store):
    p1 = store.upsert_content_policy(
        tenant_id="t1", analyzer_id="walacor.pii.v1",
        category="credit_card", action="block")
    p2 = store.upsert_content_policy(
        tenant_id="t1", analyzer_id="walacor.pii.v1",
        category="credit_card", action="warn")
    assert p1["id"] == p2["id"]
    assert p2["action"] == "warn"


def test_list_content_policies(store):
    store.upsert_content_policy("t1", "walacor.pii.v1", "credit_card", "block")
    store.upsert_content_policy("t1", "walacor.pii.v1", "ssn", "block")
    store.upsert_content_policy("t1", "walacor.toxicity.v1", "self_harm", "warn")
    policies = store.list_content_policies()
    assert len(policies) == 3


def test_list_content_policies_by_analyzer(store):
    store.upsert_content_policy("t1", "walacor.pii.v1", "credit_card", "block")
    store.upsert_content_policy("t1", "walacor.toxicity.v1", "self_harm", "warn")
    policies = store.list_content_policies(analyzer_id="walacor.pii.v1")
    assert len(policies) == 1
    assert policies[0]["category"] == "credit_card"


def test_delete_content_policy(store):
    p = store.upsert_content_policy("t1", "walacor.pii.v1", "credit_card", "block")
    assert store.delete_content_policy(p["id"]) is True
    assert len(store.list_content_policies()) == 0


def test_delete_nonexistent_policy(store):
    assert store.delete_content_policy("nonexistent") is False


def test_seed_defaults(store):
    store.seed_default_content_policies()
    policies = store.list_content_policies()
    # Should have defaults for PII (7 categories) + Llama Guard (14) + Toxicity (3)
    assert len(policies) > 10
    # Check S4 defaults to block
    s4 = [p for p in policies if p["category"] == "S4"]
    assert len(s4) == 1
    assert s4[0]["action"] == "block"


def test_seed_defaults_idempotent(store):
    store.seed_default_content_policies()
    count1 = len(store.list_content_policies())
    store.seed_default_content_policies()
    count2 = len(store.list_content_policies())
    assert count1 == count2
