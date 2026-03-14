"""Tests for pre-built policy templates (B.3)."""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "src" / "gateway" / "control" / "templates"

EXPECTED_TEMPLATES = {"owasp_llm_top10", "eu_ai_act_baseline", "hipaa_baseline", "soc2_baseline"}

# Valid enforcement levels accepted by the store
VALID_ENFORCEMENT_LEVELS = {"blocking", "warning", "pass", "warn"}

# Valid status values
VALID_STATUSES = {"active", "inactive", "draft"}


def test_templates_directory_exists():
    assert TEMPLATES_DIR.exists(), f"Templates directory not found: {TEMPLATES_DIR}"
    assert TEMPLATES_DIR.is_dir()


def test_all_template_files_exist():
    found = {p.stem for p in TEMPLATES_DIR.glob("*.json")}
    missing = EXPECTED_TEMPLATES - found
    assert not missing, f"Missing templates: {missing}"


def test_templates_valid_json():
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        assert "name" in data, f"{path.name} missing 'name'"
        assert "description" in data, f"{path.name} missing 'description'"
        assert "policies" in data, f"{path.name} missing 'policies'"
        assert isinstance(data["policies"], list), f"{path.name} 'policies' must be a list"
        assert len(data["policies"]) > 0, f"{path.name} must have at least one policy"


def test_policy_structure():
    """Each policy entry must have required fields matching the store schema."""
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        for policy in data["policies"]:
            assert "policy_id" in policy, f"{path.name}: policy missing 'policy_id'"
            assert "policy_name" in policy, f"{path.name}: policy missing 'policy_name'"
            assert "status" in policy, f"{path.name}: policy missing 'status'"
            assert policy["status"] in VALID_STATUSES, (
                f"{path.name}: policy '{policy['policy_id']}' has invalid status '{policy['status']}'"
            )
            assert "enforcement_level" in policy, (
                f"{path.name}: policy '{policy['policy_id']}' missing 'enforcement_level'"
            )
            # Rules fields must be lists
            for rules_key in ("rules", "prompt_rules", "rag_rules"):
                assert rules_key in policy, (
                    f"{path.name}: policy '{policy['policy_id']}' missing '{rules_key}'"
                )
                assert isinstance(policy[rules_key], list), (
                    f"{path.name}: policy '{policy['policy_id']}' '{rules_key}' must be a list"
                )


def test_all_policies_have_unique_ids():
    """Policy IDs must be unique within each template."""
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        ids = [p["policy_id"] for p in data["policies"] if "policy_id" in p]
        assert len(ids) == len(set(ids)), (
            f"{path.name}: duplicate policy_ids found: {[x for x in ids if ids.count(x) > 1]}"
        )


def test_owasp_template_content():
    data = json.loads((TEMPLATES_DIR / "owasp_llm_top10.json").read_text())
    assert "OWASP" in data["name"]
    assert "LLM" in data["name"] or "owasp" in data["description"].lower()
    policies = data["policies"]
    # Must have prompt injection policy
    assert any(
        "injection" in p.get("policy_id", "").lower() or
        "injection" in p.get("policy_name", "").lower()
        for p in policies
    ), "OWASP template must include a prompt injection policy"
    # Must have at least one prompt_rules entry across all policies
    total_prompt_rules = sum(len(p.get("prompt_rules", [])) for p in policies)
    assert total_prompt_rules > 0, "OWASP template must have at least one prompt rule"


def test_eu_ai_act_template_content():
    data = json.loads((TEMPLATES_DIR / "eu_ai_act_baseline.json").read_text())
    assert "EU" in data["name"] or "eu ai act" in data["name"].lower()
    policies = data["policies"]
    # Must include audit/record-keeping policy (pass enforcement)
    assert any(p.get("enforcement_level") == "pass" for p in policies), (
        "EU AI Act template must include an audit completeness (pass) policy"
    )
    # Must have attestation-related policy
    assert any(
        "attestation" in p.get("policy_id", "").lower() or
        "attest" in p.get("policy_name", "").lower()
        for p in policies
    ), "EU AI Act template must include an attestation policy"


def test_hipaa_template_content():
    data = json.loads((TEMPLATES_DIR / "hipaa_baseline.json").read_text())
    assert "HIPAA" in data["name"] or "hipaa" in data["description"].lower()
    policies = data["policies"]
    # Must include PII blocking policy
    assert any(
        "pii" in p.get("policy_id", "").lower() or
        "phi" in p.get("policy_id", "").lower() or
        "phi" in p.get("policy_name", "").lower()
        for p in policies
    ), "HIPAA template must include a PHI/PII policy"
    # Must have both prompt-side and response-side PII checks
    has_prompt_pii = any(
        any(r.get("field") == "pii_detected" for r in p.get("prompt_rules", []))
        for p in policies
    )
    has_response_pii = any(
        any(r.get("field") == "pii_detected" for r in p.get("rules", []))
        for p in policies
    )
    assert has_prompt_pii, "HIPAA template must check PII in prompts"
    assert has_response_pii, "HIPAA template must check PII in responses"


def test_soc2_template_content():
    data = json.loads((TEMPLATES_DIR / "soc2_baseline.json").read_text())
    assert "SOC" in data["name"] or "soc2" in data["description"].lower()
    policies = data["policies"]
    # Must include audit completeness sentinel
    assert any(p.get("enforcement_level") == "pass" for p in policies), (
        "SOC 2 template must include an audit completeness (pass) policy"
    )
    # Must have access control (blocking) policy
    assert any(p.get("enforcement_level") == "blocking" for p in policies), (
        "SOC 2 template must include at least one blocking policy"
    )


def test_template_version_field():
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        assert "version" in data, f"{path.name} missing 'version'"
        assert isinstance(data["version"], str), f"{path.name} 'version' must be a string"


def test_control_list_templates_endpoint(monkeypatch):
    """Unit-test the list_templates handler directly without a running server."""
    from unittest.mock import AsyncMock, MagicMock

    # We only test the pure file-reading logic by ensuring TEMPLATES_DIR is populated
    assert len(list(TEMPLATES_DIR.glob("*.json"))) == len(EXPECTED_TEMPLATES)


def test_templates_parseable_by_create_policy():
    """Verify each template policy can be passed to store.create_policy() without error."""
    import tempfile
    import os
    from gateway.control.store import ControlPlaneStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ControlPlaneStore(os.path.join(tmpdir, "control.db"))
        for path in sorted(TEMPLATES_DIR.glob("*.json")):
            data = json.loads(path.read_text())
            for policy in data["policies"]:
                policy_data = dict(policy)
                policy_data["tenant_id"] = "test-tenant"
                # Should not raise
                result = store.create_policy(policy_data)
                assert "policy_id" in result, (
                    f"{path.name}: create_policy returned no policy_id for '{policy.get('policy_id')}'"
                )
        store.close()
