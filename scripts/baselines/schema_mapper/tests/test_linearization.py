"""Tests for linearization.linearize_field — the encoder-input format."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from linearization import linearize_dict, linearize_field  # noqa: E402
from paths import FlatField  # noqa: E402


def _ff(path, key, value, siblings=(), depth=0):
    return FlatField(path=path, key=key, value=value, siblings=tuple(siblings), depth=depth)


def test_basic_linearization():
    f = _ff(
        path="usage.prompt_tokens",
        key="prompt_tokens",
        value=42,
        siblings=("completion_tokens", "total_tokens"),
        depth=2,
    )
    s = linearize_field(f)
    assert "path:usage.prompt_tokens" in s
    assert "key:prompt_tokens" in s
    assert "siblings:completion_tokens,total_tokens" in s
    assert "type:int" in s
    assert "value:42" in s


def test_siblings_sorted_alphabetically_even_if_passed_unsorted():
    f = _ff("u.x", "x", 1, siblings=("zebra", "alpha", "mango"))
    s = linearize_field(f)
    assert "siblings:alpha,mango,zebra" in s


def test_long_string_truncation():
    f = _ff("choices[0].message.content", "content", "a" * 200, siblings=("role",))
    s = linearize_field(f)
    assert "value:" in s
    # 32 chars max in the value summary
    assert "value:" + "a" * 32 in s
    assert "value:" + "a" * 33 not in s


def test_total_length_hard_cap_256():
    deep_path = ".".join([f"a{i}" for i in range(80)])
    f = _ff(deep_path, "a79", "x", siblings=tuple(f"sib{i}" for i in range(40)))
    s = linearize_field(f)
    assert len(s) <= 256


def test_list_value_rendered_as_count():
    f = _ff("prompt", "prompt", [], siblings=())
    s = linearize_field(f)
    assert "type:list" in s
    assert "value:list[0]" in s

    f2 = _ff("citations", "citations", ["http://a", "http://b", "http://c"], siblings=())
    s2 = linearize_field(f2)
    assert "value:list[3]" in s2


def test_dict_value_rendered_as_keys():
    f = _ff("usage", "usage", {"a": 1, "b": 2, "c": 3, "d": 4}, siblings=())
    s = linearize_field(f)
    assert "type:dict" in s
    # First 3 sorted keys
    assert "value:dict[a,b,c]" in s


def test_none_value():
    f = _ff("system_fingerprint", "system_fingerprint", None, siblings=())
    s = linearize_field(f)
    assert "type:null" in s
    assert "value:null" in s


def test_bool_value():
    f = _ff("done", "done", True, siblings=())
    s = linearize_field(f)
    assert "type:bool" in s
    assert "value:true" in s


def test_float_value_with_long_repr():
    f = _ff("usage.queue_time", "queue_time", 0.0123456789012345, siblings=())
    s = linearize_field(f)
    assert "type:float" in s
    # Truncated to 32 chars
    assert "value:" in s


def test_linearize_dict_round_trips_paths():
    obj = {
        "id": "abc",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    pairs = linearize_dict(obj)
    paths = {p for p, _ in pairs}
    assert paths == {"id", "usage.prompt_tokens", "usage.completion_tokens"}
    for p, s in pairs:
        assert f"path:{p}" in s
