"""Regulatory framework compliance mappings.

Each mapping function takes gateway audit data and returns a structured
compliance assessment against a specific regulatory framework.
"""

from __future__ import annotations


def get_framework_mapping(framework: str, summary: dict, attestations: list, executions: list) -> dict:
    """Dispatch to the appropriate framework mapping function."""
    dispatch = {
        "eu_ai_act": map_eu_ai_act,
        "nist": map_nist_ai_rmf,
        "soc2": map_soc2,
        "iso42001": map_iso42001,
    }
    fn = dispatch.get(framework, map_eu_ai_act)
    return fn(summary, attestations, executions)


def _compliance_status(condition: bool, partial_condition: bool = False) -> str:
    if condition:
        return "compliant"
    if partial_condition:
        return "partial"
    return "non_compliant"


def map_eu_ai_act(summary: dict, attestations: list, executions: list) -> dict:
    """Map gateway data to EU AI Act Article 12 + Annex IV requirements."""
    has_records = summary.get("total_requests", 0) > 0
    chain_info = summary.get("chain_integrity", {})
    chain_valid = chain_info.get("all_valid", True) if chain_info else True

    article_12_reqs = [
        {
            "id": "12.1",
            "description": "Automatic recording of events relevant to identifying risk",
            "status": _compliance_status(has_records),
            "evidence_ref": "execution_records",
        },
        {
            "id": "12.2",
            "description": "Logging of period of use, reference database, input data",
            "status": _compliance_status(has_records),
            "evidence_ref": "execution_records",
        },
        {
            "id": "12.3",
            "description": "Record-keeping with tamper-evident integrity verification",
            "status": _compliance_status(chain_valid, partial_condition=has_records),
            "evidence_ref": "session_chain_verification",
        },
    ]

    article_14_reqs = [
        {
            "id": "14.1",
            "description": "Human oversight measures during AI system operation",
            "status": _compliance_status(summary.get("denied", 0) > 0 or len(attestations) > 0),
            "evidence_ref": "policy_enforcement",
        },
        {
            "id": "14.4",
            "description": "Ability to decide not to use or interrupt the system",
            "status": _compliance_status(True),
            "evidence_ref": "gateway_proxy_architecture",
        },
    ]

    article_12_status = (
        "compliant" if all(r["status"] == "compliant" for r in article_12_reqs)
        else "partial" if any(r["status"] == "compliant" for r in article_12_reqs)
        else "non_compliant"
    )

    return {
        "framework": "EU AI Act",
        "articles": {
            "article_12": {
                "title": "Record-Keeping",
                "status": article_12_status,
                "requirements": article_12_reqs,
            },
            "article_14": {
                "title": "Human Oversight",
                "status": "compliant" if all(r["status"] == "compliant" for r in article_14_reqs) else "partial",
                "requirements": article_14_reqs,
            },
        },
    }


def map_nist_ai_rmf(summary: dict, attestations: list, executions: list) -> dict:
    """Map gateway data to NIST AI Risk Management Framework functions."""
    has_records = summary.get("total_requests", 0) > 0

    return {
        "framework": "NIST AI RMF",
        "functions": {
            "govern": {
                "title": "Govern",
                "description": "Policies, processes, procedures for AI risk management",
                "status": _compliance_status(has_records),
                "evidence": [
                    {"control": "GV-1.1", "description": "AI governance policies established",
                     "status": _compliance_status(len(attestations) > 0),
                     "evidence_ref": "model_attestation_registry"},
                    {"control": "GV-1.3", "description": "Roles and responsibilities defined",
                     "status": _compliance_status(True),
                     "evidence_ref": "gateway_auth_config"},
                ],
            },
            "map": {
                "title": "Map",
                "description": "Context, categorization, and AI use case mapping",
                "status": _compliance_status(len(attestations) > 0),
                "evidence": [
                    {"control": "MP-2.3", "description": "AI model inventory maintained",
                     "status": _compliance_status(len(attestations) > 0),
                     "evidence_ref": "attestation_summary"},
                ],
            },
            "measure": {
                "title": "Measure",
                "description": "Quantitative and qualitative analysis of AI risks",
                "status": _compliance_status(has_records),
                "evidence": [
                    {"control": "MS-2.6", "description": "AI system performance monitored",
                     "status": _compliance_status(has_records),
                     "evidence_ref": "execution_metrics"},
                    {"control": "MS-2.7", "description": "Content safety analysis performed",
                     "status": _compliance_status(has_records),
                     "evidence_ref": "content_analysis_results"},
                ],
            },
            "manage": {
                "title": "Manage",
                "description": "Risk treatment, response, and recovery",
                "status": _compliance_status(summary.get("denied", 0) > 0 or has_records),
                "evidence": [
                    {"control": "MG-2.2", "description": "Risk treatment implemented (policy enforcement)",
                     "status": _compliance_status(summary.get("denied", 0) > 0 or has_records),
                     "evidence_ref": "policy_enforcement_stats"},
                    {"control": "MG-3.1", "description": "Incident response through audit trail",
                     "status": _compliance_status(has_records),
                     "evidence_ref": "execution_audit_trail"},
                ],
            },
        },
    }


def map_soc2(summary: dict, attestations: list, executions: list) -> dict:
    """Map gateway data to SOC 2 Type II Trust Services Criteria."""
    has_records = summary.get("total_requests", 0) > 0

    return {
        "framework": "SOC 2 Type II",
        "criteria": {
            "CC7.2": {
                "title": "System Operations — Monitoring",
                "description": "The entity monitors system components for anomalies",
                "status": _compliance_status(has_records),
                "evidence": [
                    {"description": "All AI inference requests logged with execution records",
                     "evidence_ref": "execution_records"},
                    {"description": "Content analysis (PII/toxicity) on every request",
                     "evidence_ref": "content_analysis"},
                ],
            },
            "CC7.3": {
                "title": "System Operations — Change Detection",
                "description": "The entity evaluates detected anomalies and security events",
                "status": _compliance_status(has_records),
                "evidence": [
                    {"description": "Session chain integrity verification via UUIDv7 ID-pointer chain + Ed25519 signatures",
                     "evidence_ref": "chain_verification"},
                    {"description": "Policy enforcement with allow/deny disposition tracking",
                     "evidence_ref": "policy_stats"},
                ],
            },
            "CC8.1": {
                "title": "Change Management",
                "description": "The entity authorizes, designs, and implements changes",
                "status": _compliance_status(len(attestations) > 0),
                "evidence": [
                    {"description": "Model attestation required before inference allowed",
                     "evidence_ref": "attestation_registry"},
                    {"description": "Policy-based access control for model usage",
                     "evidence_ref": "policy_enforcement"},
                ],
            },
        },
    }


def map_iso42001(summary: dict, attestations: list, executions: list) -> dict:
    """Map gateway data to ISO 42001 AI Management System clauses."""
    has_records = summary.get("total_requests", 0) > 0

    return {
        "framework": "ISO 42001",
        "clauses": {
            "6.1": {
                "title": "Actions to Address Risks and Opportunities",
                "status": _compliance_status(has_records),
                "evidence": [
                    {"description": "Pre-inference policy evaluation on every request",
                     "evidence_ref": "policy_enforcement"},
                    {"description": "Content safety analysis (PII, toxicity, Llama Guard)",
                     "evidence_ref": "content_analysis"},
                ],
            },
            "8.4": {
                "title": "AI System Operation",
                "status": _compliance_status(has_records),
                "evidence": [
                    {"description": "Complete audit trail of all AI inference operations",
                     "evidence_ref": "execution_records"},
                    {"description": "Token budget tracking and enforcement",
                     "evidence_ref": "budget_tracking"},
                ],
            },
            "9.1": {
                "title": "Monitoring, Measurement, Analysis and Evaluation",
                "status": _compliance_status(has_records),
                "evidence": [
                    {"description": "Prometheus metrics for real-time monitoring",
                     "evidence_ref": "prometheus_metrics"},
                    {"description": "Lineage dashboard for historical analysis",
                     "evidence_ref": "lineage_dashboard"},
                ],
            },
            "10.1": {
                "title": "Continual Improvement",
                "status": _compliance_status(len(attestations) > 0),
                "evidence": [
                    {"description": "Model capability registry with automatic discovery",
                     "evidence_ref": "model_capabilities"},
                    {"description": "Compliance export reports for periodic review",
                     "evidence_ref": "compliance_export_api"},
                ],
            },
        },
    }
