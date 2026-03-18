#!/usr/bin/env bash
# Tier 1 local tests — unit + compliance
# Run this ON the EC2 from ~/Gateway
set -euo pipefail

echo "=== Tier 1: Unit Tests ==="
python3 -m pytest tests/unit/ -q --tb=short 2>&1 | tee /tmp/tier1_unit.txt
UNIT_RESULT=${PIPESTATUS[0]}

echo ""
echo "=== Tier 1: Compliance Tests ==="
python3 -m pytest tests/compliance/ -q --tb=short 2>&1 | tee /tmp/tier1_compliance.txt
COMPLIANCE_RESULT=${PIPESTATUS[0]}

python3 - <<'PYEOF'
import json, re, pathlib

def parse_pytest_summary(path):
    text = pathlib.Path(path).read_text()
    passed = int(m.group(1)) if (m := re.search(r'(\d+) passed', text)) else 0
    failed = int(m.group(1)) if (m := re.search(r'(\d+) failed', text)) else 0
    errors = int(m.group(1)) if (m := re.search(r'(\d+) error', text)) else 0
    return {"passed": passed, "failed": failed, "errors": errors}

result = {
    "tier": "1_local",
    "unit": parse_pytest_summary("/tmp/tier1_unit.txt"),
    "compliance": parse_pytest_summary("/tmp/tier1_compliance.txt"),
}
pathlib.Path("tests/artifacts").mkdir(exist_ok=True)
pathlib.Path("tests/artifacts/tier1_local.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
PYEOF

if [ $UNIT_RESULT -ne 0 ] || [ $COMPLIANCE_RESULT -ne 0 ]; then
    echo "GATE FAILED: Fix unit/compliance failures before proceeding to Tier 2"
    exit 1
fi
echo "GATE PASSED: All local tests pass"
