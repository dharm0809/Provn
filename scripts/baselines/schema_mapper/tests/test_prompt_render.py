"""Regression test: synthesize._build_prompt must render the teacher prompt
template without raising KeyError on any literal `{...}` in the body.

This test guards against the bug fixed in commit "fix(schema_mapper):
escape literal braces in synthesize_variants.txt" — earlier the line
`expected_labels: flat dict {dotted_path: canonical_label}` was an
unescaped `{...}`, which Python's str.format() treated as a placeholder.
The synthesis run crashed on the FIRST call to _build_prompt before
any Anthropic API call (so cost was $0.00 — the budget ledger was
never touched).
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from synthesize import _build_prompt  # noqa: E402


def test_prompt_renders_without_keyerror():
    seed = {
        "raw": {"id": "abc", "model": "gpt-4o", "usage": {"prompt_tokens": 5}},
        "expected_labels": {
            "id": "response_id",
            "model": "model",
            "usage.prompt_tokens": "prompt_tokens",
        },
    }
    prompt = _build_prompt(seed, provider="openai", endpoint="chat.completions", n_variations=10)

    # Placeholders filled
    assert "openai" in prompt
    assert "chat.completions" in prompt
    assert "10 plausible variations" in prompt or "10 objects" in prompt
    assert '"id": "abc"' in prompt
    assert '"model": "gpt-4o"' in prompt

    # The literal `{dotted_path: canonical_label}` survives the format
    # call (proved by `{{...}}` in the template, which collapses to `{...}`)
    assert "{dotted_path: canonical_label}" in prompt

    # Label-descriptions block was injected
    assert "tool_call_arguments" in prompt
    assert "response_timestamp" in prompt
    assert "UNKNOWN" in prompt


def test_prompt_renders_for_every_provider_spec():
    """Render the prompt against EVERY hand-curated provider spec to make
    sure no specific spec contains characters that interact badly with
    the template rendering."""
    import json

    specs_dir = pathlib.Path(__file__).resolve().parent.parent / "data" / "provider_specs"
    for spec_path in sorted(specs_dir.glob("*.json")):
        spec = json.loads(spec_path.read_text())
        for ex in spec["examples"]:
            prompt = _build_prompt(ex, provider=spec["provider"],
                                   endpoint=spec.get("endpoint", ""), n_variations=16)
            assert "openai" in prompt or "anthropic" in prompt or len(prompt) > 1000  # sanity


def test_prompt_template_brace_escapes_documented():
    """The brace-escaping convention is called out in the synthesize.py
    code comment near _build_prompt — guard against a future maintainer
    silently dropping that comment."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "synthesize.py").read_text()
    assert "must be escaped as `{{`" in src
