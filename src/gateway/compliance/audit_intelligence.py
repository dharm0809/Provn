"""Compliance Audit Intelligence — gaps analysis, readiness scoring, evidence linking.

Upgrades the compliance page from a data dump to an actual intelligence tool.
Instead of "you have 336 records → compliant", it tells you:
  - What's strong, what's weak, what's missing
  - A 0-100 audit readiness score with dimension breakdown
  - Specific evidence records linked to each requirement
  - Actionable recommendations for each gap
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Capability Dimensions ────────────────────────────────────────────────────
# Each dimension tests a specific gateway capability. Weight = importance.

def assess_audit_readiness(
    summary: dict,
    attestations: list,
    executions: list,
    chain_report: list,
    health: dict | None = None,
) -> dict:
    """Compute audit readiness score with dimension breakdown and gap analysis.

    Returns:
        {
            "score": 0-100,
            "grade": "A" | "B" | "C" | "D" | "F",
            "dimensions": [...],
            "gaps": [...],
            "strengths": [...],
            "recommendations": [...],
        }
    """
    dimensions = []
    gaps = []
    strengths = []
    recommendations = []

    total_requests = summary.get("total_requests", 0)
    allowed = summary.get("allowed", 0)
    denied = summary.get("denied", 0)
    models = summary.get("models_used", [])
    content_analysis = summary.get("content_analysis_coverage", 0)

    # Extract health data
    h = health or {}
    analyzers_count = h.get("content_analyzers", 0)
    chain_active = bool(h.get("session_chain", {}).get("active_sessions") is not None)
    wal_ok = h.get("wal", {}).get("disk_usage_bytes") is not None
    enforcement = h.get("enforcement_mode", "unknown")

    # ── 1. Record Completeness (weight: 20) ──────────────────────────
    d1_score = 0
    d1_evidence = []
    if total_requests > 0:
        d1_score += 40  # Have records
        d1_evidence.append(f"{total_requests} total requests recorded")
    if total_requests >= 100:
        d1_score += 30  # Sufficient volume for statistical analysis
        d1_evidence.append("Sufficient volume for statistical analysis")
    if total_requests >= 1000:
        d1_score += 30  # Production-grade volume
    elif total_requests >= 100:
        d1_score += 15
    d1_score = min(d1_score, 100)
    dimensions.append({
        "name": "Record Completeness",
        "score": d1_score, "weight": 20,
        "description": "Complete audit trail of all AI inference operations",
        "evidence": d1_evidence,
    })
    if total_requests == 0:
        gaps.append({"dimension": "Record Completeness", "severity": "critical",
                      "issue": "No execution records found in the audit period",
                      "fix": "Send requests through the gateway to generate audit records"})

    # ── 2. Chain Integrity (weight: 20) ──────────────────────────────
    d2_score = 0
    d2_evidence = []
    sessions_verified = len(chain_report)
    valid_sessions = sum(1 for r in chain_report if r.get("valid", False))
    invalid_sessions = sessions_verified - valid_sessions

    if sessions_verified > 0:
        d2_score += 30
        d2_evidence.append(f"{sessions_verified} sessions verified")
        integrity_pct = (valid_sessions / sessions_verified * 100) if sessions_verified else 0
        d2_score += int(integrity_pct * 0.7)  # Up to 70 more points
        d2_evidence.append(f"{valid_sessions}/{sessions_verified} chains valid ({integrity_pct:.0f}%)")
        if integrity_pct == 100:
            strengths.append("All session chains verified — tamper-evident integrity confirmed")
        elif invalid_sessions > 0:
            gaps.append({"dimension": "Chain Integrity", "severity": "warning",
                          "issue": f"{invalid_sessions} session(s) with broken chains",
                          "fix": "Chain breaks usually occur after gateway restarts. Ensure stable deployments."})
    else:
        gaps.append({"dimension": "Chain Integrity", "severity": "critical",
                      "issue": "No session chains available for verification",
                      "fix": "Enable session_chain_enabled=true and ensure records are being written"})
    d2_score = min(d2_score, 100)
    dimensions.append({
        "name": "Chain Integrity", "score": d2_score, "weight": 20,
        "description": "Tamper-evident ID-pointer chain verification across all sessions",
        "evidence": d2_evidence,
    })

    # ── 3. Content Safety (weight: 15) ───────────────────────────────
    # The compliance-relevant signal is "did an analyzer actually run on
    # traffic?", NOT "how many analyzers were configured at boot". The
    # summary exposes analyzer coverage measured over the same window as
    # the score; we combine it with the configured-count floor so a tenant
    # with zero traffic still sees a meaningful signal.
    total_exec = summary.get("total_executions", 0) or summary.get("total_requests", 0)
    coverage_pct = float(summary.get("content_analysis_coverage_pct", 0.0))
    covered = summary.get("content_analysis_covered", 0)

    d3_score = 0
    d3_evidence = []
    if total_exec > 0:
        # 70 points from actual runtime coverage, 30 from configured breadth.
        d3_score = int(coverage_pct * 0.7)
        config_bonus = min(30, analyzers_count * 8)
        d3_score += config_bonus
        d3_score = min(d3_score, 100)
        d3_evidence.append(
            f"{covered}/{total_exec} executions carry analyzer output ({coverage_pct:.0f}% coverage)"
        )
        d3_evidence.append(f"{analyzers_count} analyzer(s) configured")
        if coverage_pct < 50:
            gaps.append({"dimension": "Content Safety", "severity": "warning",
                          "issue": f"Only {coverage_pct:.0f}% of executions have analyzer output",
                          "fix": "Verify analyzers are not timing out or failing open on request traffic"})
        elif coverage_pct >= 95 and analyzers_count >= 4:
            strengths.append("Full analyzer coverage on production traffic (≥95%, multi-layer)")
    else:
        # Nothing to measure — fall back to configured-breadth only, but
        # don't mistake absence of traffic for good safety posture.
        d3_score = min(60, analyzers_count * 15)
        if analyzers_count > 0:
            d3_evidence.append(
                f"{analyzers_count} analyzer(s) configured; no traffic in window to verify coverage"
            )
        else:
            gaps.append({"dimension": "Content Safety", "severity": "warning",
                          "issue": "No content analyzers configured and no traffic to verify",
                          "fix": "Enable pii_detection_enabled and toxicity_detection_enabled"})

    dimensions.append({
        "name": "Content Safety", "score": d3_score, "weight": 15,
        "description": "PII / toxicity / safety analyzer coverage on window traffic",
        "evidence": d3_evidence,
    })

    # ── 4. Model Governance (weight: 15) ─────────────────────────────
    d4_score = 0
    d4_evidence = []

    if len(attestations) > 0:
        d4_score += 50
        d4_evidence.append(f"{len(attestations)} model attestation(s) registered")
        if all(a.get("status") == "active" for a in attestations):
            d4_score += 30
            d4_evidence.append("All attestations active")
        d4_score += min(20, len(models) * 5)  # Bonus for multi-model tracking
        strengths.append("Model attestation registry active — all models are tracked and approved")
    else:
        d4_score = 20  # Auto-attestation gives some governance
        d4_evidence.append("Auto-attestation mode (models self-attested on first use)")
        gaps.append({"dimension": "Model Governance", "severity": "info",
                      "issue": "Using auto-attestation — no explicit model approval process",
                      "fix": "Enable control_plane_enabled and register models via the Control API"})

    if denied > 0:
        d4_score = min(d4_score + 10, 100)
        d4_evidence.append(f"{denied} requests denied by policy — enforcement working")

    d4_score = min(d4_score, 100)
    dimensions.append({
        "name": "Model Governance", "score": d4_score, "weight": 15,
        "description": "Model attestation, policy enforcement, access control",
        "evidence": d4_evidence,
    })

    # ── 5. User Identity (weight: 10) ────────────────────────────────
    d5_score = 0
    d5_evidence = []

    users_with_identity = 0
    anonymous_users = 0
    for ex in executions[:100]:  # Sample first 100
        user = ex.get("user", "")
        if user and "anonymous" not in user.lower():
            users_with_identity += 1
        else:
            anonymous_users += 1

    total_sampled = users_with_identity + anonymous_users
    if total_sampled > 0:
        identity_pct = users_with_identity / total_sampled * 100
        d5_score = int(identity_pct)
        d5_evidence.append(f"{identity_pct:.0f}% of requests have user identity ({users_with_identity}/{total_sampled} sampled)")
        if identity_pct < 50:
            gaps.append({"dimension": "User Identity", "severity": "warning",
                          "issue": f"{anonymous_users} requests from anonymous users",
                          "fix": "Enable ENABLE_FORWARD_USER_INFO_HEADERS=true on OpenWebUI, or configure JWT auth"})
        elif identity_pct >= 90:
            strengths.append("Strong user identity coverage — most requests are attributed to named users")

    dimensions.append({
        "name": "User Identity", "score": d5_score, "weight": 10,
        "description": "User attribution for every AI interaction (identity, roles, team)",
        "evidence": d5_evidence,
    })

    # ── 6. Data Persistence (weight: 10) ─────────────────────────────
    d6_score = 0
    d6_evidence = []

    if wal_ok:
        d6_score += 50
        d6_evidence.append("Local WAL (Write-Ahead Log) operational")
    walacor_ok = h.get("storage", {}).get("backend") == "walacor"
    if walacor_ok:
        d6_score += 50
        d6_evidence.append("Walacor blockchain backend connected — immutable storage")
        strengths.append("Dual-write to WAL + Walacor blockchain — data survives any single failure")
    else:
        gaps.append({"dimension": "Data Persistence", "severity": "info",
                      "issue": "Walacor backend not connected — using local WAL only",
                      "fix": "Configure WALACOR_SERVER for blockchain-backed immutable storage"})

    d6_score = min(d6_score, 100)
    dimensions.append({
        "name": "Data Persistence", "score": d6_score, "weight": 10,
        "description": "Dual-write to local WAL and Walacor blockchain for tamper-proof storage",
        "evidence": d6_evidence,
    })

    # ── 7. Enforcement Mode (weight: 10) ─────────────────────────────
    d7_score = 0
    d7_evidence = []

    if enforcement == "enforced":
        d7_score = 100
        d7_evidence.append("Full governance enforcement active")
        strengths.append("Gateway running in enforced mode — all policies are actively applied")
    elif enforcement == "permissive":
        d7_score = 50
        d7_evidence.append("Permissive mode — policies logged but not enforced")
        gaps.append({"dimension": "Enforcement", "severity": "warning",
                      "issue": "Running in permissive mode — violations are logged but not blocked",
                      "fix": "Set enforcement_mode=enforced for production compliance"})
    else:
        d7_evidence.append(f"Enforcement mode: {enforcement}")

    dimensions.append({
        "name": "Enforcement Mode", "score": d7_score, "weight": 10,
        "description": "Policy enforcement mode — enforced blocks violations, permissive only logs",
        "evidence": d7_evidence,
    })

    # ── Compute Overall Score ────────────────────────────────────────
    total_weight = sum(d["weight"] for d in dimensions)
    weighted_score = sum(d["score"] * d["weight"] for d in dimensions) / total_weight if total_weight else 0
    score = round(weighted_score)

    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    # ── Build Recommendations ────────────────────────────────────────
    for gap in sorted(gaps, key=lambda g: {"critical": 0, "warning": 1, "info": 2}.get(g["severity"], 3)):
        recommendations.append({
            "priority": gap["severity"],
            "dimension": gap["dimension"],
            "action": gap["fix"],
        })

    return {
        "score": score,
        "grade": grade,
        "assessed_at": datetime.now(timezone.utc).isoformat(),
        "dimensions": dimensions,
        "gaps": gaps,
        "strengths": strengths,
        "recommendations": recommendations,
    }
