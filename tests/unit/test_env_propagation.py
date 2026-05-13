"""Catch the 'forgot to document a new env var' regression class.

The gateway loads env vars via two surfaces:

  1. Docker: `env_file: .env.gateway` in `docker-compose.yml` propagates every
     entry in the host's `.env.gateway` into the container.
  2. Native: `Settings(env_file=(".env", ".env.gateway"))` in `config.py`
     reads the same file directly.

If a developer adds a new `walacor_xxx` field to `Settings` but forgets to
document it in `.env.gateway.example`, two things go wrong:

  - Operators copying `.env.gateway.example` → `.env.gateway` won't see the
    new knob and can't tune it.
  - The compose env_file forwards exactly what's *in* `.env.gateway`, so a
    field that exists in code but isn't in the example file gets silently
    ignored at deploy time.

This test parses `Settings` and asserts every public WALACOR_* env var is
mentioned in `.env.gateway.example` (either with the canonical WALACOR_*
name or, where present, the `validation_alias` alternates like
`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`).

If you're adding a new Settings field: just add the matching line to
`.env.gateway.example`. The test runs in seconds.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices

from gateway.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_EXAMPLE = REPO_ROOT / ".env.gateway.example"

# Fields that are intentionally not env-driven, or that come from the
# docker-compose `environment:` block of deployment-shape constants
# (`WALACOR_PROVIDER_OLLAMA_URL` and `WALACOR_WAL_PATH`) rather than the
# user-managed env file. Skip them so the test doesn't false-alarm.
_NOT_IN_ENV_FILE: frozenset[str] = frozenset({
    "WALACOR_PROVIDER_OLLAMA_URL",  # set by compose to http://ollama:11434
    "WALACOR_WAL_PATH",             # set by compose to /var/walacor/wal volume mount
})


def _all_env_names_for_field(name: str, field) -> set[str]:
    """Return every env-var name a Settings field accepts.

    Pydantic's resolution order for a field's env name:
      - `validation_alias=AliasChoices(...)` if present → those exact names
      - otherwise `<env_prefix><FIELD_NAME>` uppercased
    """
    alias = field.validation_alias
    if isinstance(alias, AliasChoices):
        return {str(c).upper() for c in alias.choices if isinstance(c, str)}
    if isinstance(alias, str):
        return {alias.upper()}
    return {f"WALACOR_{name.upper()}"}


def _example_env_keys(path: Path) -> set[str]:
    """Parse a dotenv-style file and return every defined key (commented-out
    `# KEY=` placeholders count — they document the var).
    """
    keys: set[str] = set()
    text = path.read_text()
    # Either `KEY=...` or `# KEY=...` on its own line.
    for m in re.finditer(r"(?m)^\s*#?\s*([A-Z_][A-Z0-9_]*)=", text):
        keys.add(m.group(1))
    return keys


def test_every_settings_field_is_documented_in_env_gateway_example():
    assert GATEWAY_EXAMPLE.exists(), f"missing {GATEWAY_EXAMPLE}"
    example_keys = _example_env_keys(GATEWAY_EXAMPLE)

    missing: list[str] = []
    for field_name, field in Settings.model_fields.items():
        candidates = _all_env_names_for_field(field_name, field)
        # A field is "documented" if ANY of its accepted env names appears in
        # the example file. This lets validation_alias entries like
        # OPENAI_API_KEY satisfy the check without forcing
        # WALACOR_PROVIDER_OPENAI_KEY into the file too.
        if not candidates & example_keys:
            # Only flag fields that get their value from env at all — skip the
            # two deployment-shape constants set by compose.
            if any(c in _NOT_IN_ENV_FILE for c in candidates):
                continue
            missing.append(
                f"  - {field_name} (looking for any of: "
                f"{sorted(candidates)})"
            )

    assert not missing, (
        "Settings fields not documented in .env.gateway.example. Add a line "
        "for each (commented or with a default value) so operators discover "
        "the knob and so the docker-compose env_file forwards it:\n"
        + "\n".join(missing)
    )


def test_validation_aliases_for_industry_standard_keys():
    """Document the two intentional aliases as a regression pin: if anyone
    refactors `provider_openai_key` / `provider_anthropic_key` and drops the
    `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` aliases, users who set those
    standard env vars would silently get an empty provider key. Fail loud."""
    for field_name, expected_alias in (
        ("provider_openai_key", "OPENAI_API_KEY"),
        ("provider_anthropic_key", "ANTHROPIC_API_KEY"),
    ):
        field = Settings.model_fields[field_name]
        names = _all_env_names_for_field(field_name, field)
        assert expected_alias in names, (
            f"{field_name} no longer accepts {expected_alias} via "
            f"validation_alias — operators who set that standard name "
            f"will silently get an empty key. Current aliases: {sorted(names)}"
        )
