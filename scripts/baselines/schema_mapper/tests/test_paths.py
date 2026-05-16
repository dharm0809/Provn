"""Regression tests for paths.parent_object_path and flatten_json.

The Anthropic content-blocks fixture is the canonical case: content[0]
and content[1] are at the same depth but have DIFFERENT parent objects
(content[0] vs content[1]), so EXCLUSIVE_GROUPS exclusion must NOT fire
between content[0].thinking and content[1].text.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from paths import FlatField, flatten_json, parent_object_path  # noqa: E402


def test_parent_path_strips_trailing_leaf():
    assert parent_object_path("choices[0].message.content") == "choices[0].message"
    assert parent_object_path("usage.prompt_tokens") == "usage"
    assert parent_object_path("prompt_tokens") == ""


def test_parent_path_keeps_array_index():
    # content[0].thinking → content[0] (index is part of the parent identity)
    assert parent_object_path("content[0].thinking") == "content[0]"
    assert parent_object_path("content[1].text") == "content[1]"


def test_anthropic_content_blocks_have_distinct_parents():
    """The architectural regression test.

    content[0].thinking and content[1].text are at the same depth in an
    Anthropic extended-thinking response. They share the EXCLUSIVE_GROUPS
    `primary_content` group ({content, thinking_content}). The CRF must
    NOT fire exclusion between them because their parent objects differ.
    """
    p1 = parent_object_path("content[0].thinking")
    p2 = parent_object_path("content[1].text")
    assert p1 != p2
    assert p1 == "content[0]"
    assert p2 == "content[1]"


def test_openai_message_blocks_share_parent():
    """Counter-case: choices[0].message.content and a hypothetical
    choices[0].message.thinking_content WOULD share parent — exclusion
    fires correctly here."""
    p1 = parent_object_path("choices[0].message.content")
    p2 = parent_object_path("choices[0].message.thinking_content")
    assert p1 == p2 == "choices[0].message"


def test_flatten_simple_dict():
    flat = flatten_json({"a": 1, "b": "x"})
    paths = {f.path for f in flat}
    assert paths == {"a", "b"}


def test_flatten_nested_with_array():
    obj = {
        "id": "abc",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}},
        ],
    }
    flat = flatten_json(obj)
    paths = {f.path for f in flat}
    assert paths == {
        "id",
        "choices[0].index",
        "choices[0].message.role",
        "choices[0].message.content",
    }


def test_flatten_anthropic_content_blocks_round_trip():
    """Round-trip the actual anthropic_messages thinking-block fixture."""
    spec_path = pathlib.Path(__file__).resolve().parent.parent / "data" / "provider_specs" / "anthropic_messages.json"
    spec = json.loads(spec_path.read_text())
    thinking_example = next(
        ex for ex in spec["examples"] if any(p.endswith(".thinking") for p in ex["expected_labels"])
    )
    flat_paths = {f.path for f in flatten_json(thinking_example["raw"])}
    assert flat_paths == set(thinking_example["expected_labels"])


def test_flatten_siblings_within_same_parent():
    obj = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    flat = flatten_json(obj)
    pt = next(f for f in flat if f.path == "usage.prompt_tokens")
    assert set(pt.siblings) == {"completion_tokens", "total_tokens"}
    assert pt.siblings == tuple(sorted(pt.siblings))  # deterministic order
