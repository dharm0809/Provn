#!/usr/bin/env python3
"""CI guard: fail if dashboard source carries mockup placeholders or dead controls.

The lineage dashboard was ported from a static design mockup ("from design
zip, wired to real API"). The port was partial: mockup literals
(``alex.chen@acme.io``, invented model architectures) and dead controls
(``onClick={() => {}}``, interactive elements with no handler) survived
because they *look* finished in the rendered UI and pass human review.

This script makes that failure mode mechanical and blocking. It is a
by-construction guard: a partial port now fails CI instead of shipping a
plausible-looking placeholder to a production governance surface.

Run: python scripts/check_dashboard_placeholders.py
Exit 0 = clean, 1 = violations found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DASHBOARD_SRC = (
    Path(__file__).resolve().parent.parent
    / "src" / "gateway" / "lineage" / "dashboard" / "src"
)

# (compiled regex, human explanation). Keep patterns specific — false
# positives erode trust in the gate faster than anything.
PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[\w.\-]+@(?:acme\.io|example\.(?:com|org)|test\.com)\b"),
     "hardcoded mockup email — derive from API/auth context"),
    (re.compile(r"\b(?:lorem ipsum|foo@bar|john\.doe|jane\.doe)\b", re.I),
     "lorem/placeholder identity literal"),
    (re.compile(r"onClick=\{\s*\(\s*\)\s*=>\s*\{\s*\}\s*\}"),
     "no-op onClick={() => {}} — wire the handler or remove the control"),
    (re.compile(r"//\s*MOCK:"),
     "unresolved // MOCK: marker from a design-mockup port"),
]

# Files allowed to contain otherwise-flagged strings (e.g. this checker's
# own docs, or test fixtures that intentionally use example.com).
ALLOWLIST_SUFFIXES = (".test.jsx", ".test.js", ".spec.jsx", ".spec.js")


def main() -> int:
    if not DASHBOARD_SRC.is_dir():
        print(f"dashboard src not found: {DASHBOARD_SRC}", file=sys.stderr)
        return 1

    violations: list[str] = []
    for path in sorted(DASHBOARD_SRC.rglob("*.js*")):
        if path.name.endswith(ALLOWLIST_SUFFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat, why in PATTERNS:
                if pat.search(line):
                    rel = path.relative_to(DASHBOARD_SRC.parent.parent.parent.parent.parent)
                    violations.append(f"{rel}:{lineno}: {why}\n    {line.strip()[:120]}")

    if violations:
        print("Dashboard placeholder/dead-control check FAILED:\n", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            f"\n{len(violations)} violation(s). These are production-surface "
            "defects: a placeholder that looks real is worse than an obvious "
            "gap. Wire to real data or remove the control.",
            file=sys.stderr,
        )
        return 1

    print("Dashboard placeholder/dead-control check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
