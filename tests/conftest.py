import os
import sys
from pathlib import Path

# ── Isolate tests from local `.env.gateway` credentials ──────────────────
#
# pydantic-settings auto-loads `.env.gateway` from the repo root. Three
# Walacor-backend fields (`walacor_server`, `walacor_username`,
# `walacor_password`) use `validation_alias=AliasChoices("WALACOR_SERVER",
# …)` which BYPASSES the `WALACOR_` env_prefix. So a local dev with a
# populated `.env.gateway` would silently have the test suite connect
# to a real Walacor sandbox, write test records to it, and read other
# developers' executions back — cross-test pollution.
#
# Strategy: force the credential env vars to empty strings. Pydantic-
# settings gives shell env vars higher precedence than `.env` files,
# so an explicit empty string overrides whatever `.env.gateway` sets.
# Unsetting via `pop()` would fall back to the dotenv file — NOT what
# we want. Tests that legitimately exercise a real Walacor backend
# must override these back in their own fixture.
for _cred in ("WALACOR_SERVER", "WALACOR_USERNAME", "WALACOR_PASSWORD"):
    os.environ[_cred] = ""

# ── WeasyPrint shared-lib path on macOS ──────────────────────────────────
#
# WeasyPrint needs Homebrew shared libs (pango, cairo, gobject) on macOS.
# `DYLD_FALLBACK_LIBRARY_PATH` is the documented API, but cffi's dlopen
# path through ctypes.util.find_library only reliably picks up
# `DYLD_LIBRARY_PATH` under SIP. Set both so imports succeed regardless
# of which dyld search rule fires first.
if sys.platform == "darwin":
    _brew_lib = Path("/opt/homebrew/lib")
    if _brew_lib.exists():
        for _var in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
            existing = os.environ.get(_var, "")
            parts = [p for p in existing.split(":") if p]
            if str(_brew_lib) not in parts:
                parts.append(str(_brew_lib))
                os.environ[_var] = ":".join(parts)
