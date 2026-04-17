"""Regression tests for `gateway.intelligence.worker` prompt templates.

Before the fix, the templates used single curly braces for JSON example
bodies (`{"topics": [...]}`). Because the code feeds these strings to
`str.format(prompt=..., response=...)`, Python treated the JSON braces
as format placeholders and raised `KeyError: '"topics"'` on every job.
The intelligence worker silently failed 100% of jobs, but the errors
were swallowed so it looked like a warning, not a broken feature.

This test renders each prompt with representative inputs and asserts
the result is a valid string containing both the caller's substituted
content AND the JSON example bytes intact.
"""
from __future__ import annotations

from gateway.intelligence.worker import (
    _CLASSIFY_PROMPT,
    _COMPLIANCE_PROMPT,
    _SUMMARY_PROMPT,
    _TOPICS_PROMPT,
)


def test_classify_prompt_formats_without_keyerror():
    rendered = _CLASSIFY_PROMPT.format(prompt="Hello, what is SHA3?")
    assert "Hello, what is SHA3?" in rendered
    # The JSON example must survive format() intact.
    assert '"category"' in rendered
    assert '"confidence"' in rendered


def test_topics_prompt_formats_without_keyerror():
    rendered = _TOPICS_PROMPT.format(prompt="question", response="answer")
    assert "question" in rendered
    assert "answer" in rendered
    assert '"topics"' in rendered


def test_compliance_prompt_formats_without_keyerror():
    rendered = _COMPLIANCE_PROMPT.format(prompt="Q", response="A")
    assert "Q" in rendered
    assert "A" in rendered
    assert '"flags"' in rendered


def test_summary_prompt_formats_without_keyerror():
    rendered = _SUMMARY_PROMPT.format(prompt="long conversation text")
    assert "long conversation text" in rendered
    assert '"summary"' in rendered


def test_all_prompts_survive_adversarial_curly_braces_in_prompt():
    """User-supplied prompts containing braces must not break format().

    Guards against a reverse-regression: if someone fixes brace escaping
    in the templates but then tries to format with a prompt that itself
    contains `{...}`, format() would treat those braces as placeholders.
    The fix is to use `.format()` with ONLY named kwargs, never **data
    expansion, so user-supplied values are treated as values not format
    strings. This test just verifies the current code path doesn't
    explode when the prompt contains braces.
    """
    tricky = "What does {important} mean in a {context}?"
    for template, kwargs in [
        (_CLASSIFY_PROMPT, {"prompt": tricky}),
        (_TOPICS_PROMPT, {"prompt": tricky, "response": tricky}),
        (_COMPLIANCE_PROMPT, {"prompt": tricky, "response": tricky}),
        (_SUMMARY_PROMPT, {"prompt": tricky}),
    ]:
        rendered = template.format(**kwargs)
        # The substituted prompt should appear verbatim (Python's format
        # does single-pass substitution — doesn't re-interpret values).
        assert "{important}" in rendered
        assert "{context}" in rendered
