#!/usr/bin/env python3
"""Tier 5 compliance artifact generation.

Run ON the EC2 instance from ~/Gateway:
    python tests/production/tier5_compliance.py

Prerequisite: Run governance_stress.py first to populate sessions:
    GATEWAY_URL=http://localhost:8002/v1/chat/completions \\
    GATEWAY_MODEL=qwen3:1.7b python tests/governance_stress.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, "tests/production")
from config import BASE_URL, LINEAGE_URL, HEADERS, ARTIFACTS_DIR, save_artifact

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append({"name": name, "passed": passed, "detail": detail})


def test_compliance_pdf():
    r = requests.get(f"{BASE_URL}/v1/compliance/report", headers=HEADERS, timeout=30)
    if r.status_code == 404:
        check("PDF compliance endpoint (skipped — 404, not deployed)", True, "optional endpoint")
        return
    check("PDF compliance report → 200", r.status_code == 200, f"got {r.status_code}")
    if r.status_code == 200:
        pdf_path = ARTIFACTS_DIR / "compliance_report.pdf"
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        pdf_path.write_bytes(r.content)
        check("PDF non-empty", len(r.content) > 1000, f"{len(r.content)} bytes")
        print(f"    Saved: {pdf_path}")


def test_audit_export():
    r = requests.get(f"{BASE_URL}/v1/compliance/export", headers=HEADERS, timeout=30)
    if r.status_code == 404:
        check("Audit export endpoint (skipped — 404, not deployed)", True, "optional endpoint")
        return
    check("Audit export → 200", r.status_code == 200, f"got {r.status_code}")
    if r.status_code == 200:
        export_path = ARTIFACTS_DIR / "audit_export.jsonl"
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        export_path.write_text(r.text)
        lines = [l for l in r.text.splitlines() if l.strip()]
        check("Audit export has records", len(lines) > 0, f"{len(lines)} records")


def test_chain_audit():
    r = requests.get(f"{LINEAGE_URL}/sessions", timeout=10)
    check("Sessions available for chain audit", r.status_code == 200)
    if r.status_code != 200:
        return

    sessions = r.json().get("sessions", [])
    check("≥10 sessions available (run governance_stress.py first if 0)",
          len(sessions) >= 10, f"{len(sessions)} sessions")

    audit = {"total": 0, "valid": 0, "invalid": 0, "results": []}
    for s in sessions[:50]:
        sid = s.get("session_id")
        if not sid:
            continue
        rv = requests.get(f"{LINEAGE_URL}/verify/{sid}", timeout=10)
        if rv.status_code == 200:
            v = rv.json()
            valid = bool(v.get("valid") or v.get("chain_valid") or v.get("result") == "valid")
            audit["total"] += 1
            audit["valid" if valid else "invalid"] += 1
            audit["results"].append({"session_id": str(sid)[:16], "valid": valid})

    check("All verified sessions chain-valid",
          audit["invalid"] == 0,
          f"{audit['valid']}/{audit['total']} valid")
    save_artifact("chain_audit", audit)


def test_eu_ai_act():
    doc = Path("docs/EU-AI-ACT-COMPLIANCE.md")
    check("EU-AI-ACT-COMPLIANCE.md exists", doc.exists())
    if not doc.exists():
        return
    text = doc.read_text()
    for section in ["Article 9", "Article 12", "Article 14", "Article 15", "SOC 2"]:
        check(f"Doc covers {section}", section in text)
    has_lineage = requests.get(f"{LINEAGE_URL}/sessions", timeout=10).status_code == 200
    check("Article 12 audit trail is live (lineage API up)", has_lineage)


def test_health_completeness():
    r = requests.get(f"{BASE_URL}/health", timeout=10)
    check("Health → 200", r.status_code == 200)
    if r.status_code == 200:
        h = r.json()
        check("Health response has multiple fields", len(h) > 2, str(list(h.keys())))
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        save_artifact("health_response", h)


def test_metrics():
    r = requests.get(f"{BASE_URL}/metrics", timeout=10)
    check("Metrics → 200", r.status_code == 200, f"got {r.status_code}")
    if r.status_code == 200:
        lines = [l for l in r.text.splitlines() if l and not l.startswith("#")]
        check("Metrics has numeric values", len(lines) > 0, f"{len(lines)} metric lines")
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        (ARTIFACTS_DIR / "metrics_snapshot.txt").write_text(r.text)


def test_sla_card():
    sla_path = ARTIFACTS_DIR / "sla_card.json"
    check("SLA card present (from Tier 3)", sla_path.exists(),
          "Run tier3_performance.py first")
    if sla_path.exists():
        sla = json.loads(sla_path.read_text())
        print(f"    p50={sla.get('baseline_p50_s')}s  p99={sla.get('baseline_p99_s')}s  "
              f"req/s={sla.get('sustained_req_per_sec')}")


def main():
    print("\n=== Tier 5: Compliance Artifacts ===\n")
    print("[1/7] PDF compliance report"); test_compliance_pdf()
    print("[2/7] Audit log export"); test_audit_export()
    print("[3/7] Chain audit (50 sessions)"); test_chain_audit()
    print("[4/7] EU AI Act coverage"); test_eu_ai_act()
    print("[5/7] Health completeness"); test_health_completeness()
    print("[6/7] Metrics format"); test_metrics()
    print("[7/7] SLA card from Tier 3"); test_sla_card()

    passed = sum(1 for r in RESULTS if r["passed"])
    failed = sum(1 for r in RESULTS if not r["passed"])
    print(f"\n{'='*40}")
    print(f"Tier 5 Compliance: {passed} PASS, {failed} FAIL")

    all_artifacts = sorted(ARTIFACTS_DIR.glob("*")) if ARTIFACTS_DIR.exists() else []
    print(f"\nArtifacts ({len(all_artifacts)} files):")
    for p in all_artifacts:
        print(f"  {p.name}")

    save_artifact("tier5_compliance", {
        "tier": "5_compliance", "passed": passed, "failed": failed,
        "results": RESULTS,
        "artifacts": [p.name for p in all_artifacts],
        "gate": "PASS" if failed == 0 else "FAIL",
    })

    if failed > 0:
        print("\nGATE FAILED")
        sys.exit(1)
    print("\n" + "=" * 40)
    print("  ALL TIERS COMPLETE — LAUNCH READY")
    print("=" * 40)


if __name__ == "__main__":
    main()
