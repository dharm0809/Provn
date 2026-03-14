"""Tests for gateway.util.json_utils — verifies the orjson/stdlib shim behaves correctly."""

from gateway.util.json_utils import loads, dumps, dumps_bytes


def test_roundtrip():
    data = {"model": "qwen3:1.7b", "messages": [{"role": "user", "content": "hello"}]}
    assert loads(dumps(data)) == data


def test_bytes_roundtrip():
    data = {"key": "value", "number": 42}
    assert loads(dumps_bytes(data)) == data


def test_unicode():
    data = {"text": "Hello 世界 🌍"}
    assert loads(dumps(data)) == data


def test_nested():
    data = {"outer": {"inner": [1, 2, 3]}, "flag": True, "null": None}
    assert loads(dumps(data)) == data


def test_sort_keys():
    data = {"z": 1, "a": 2, "m": 3}
    serialised = dumps(data, sort_keys=True)
    # Keys should appear in alphabetical order.
    assert serialised.index('"a"') < serialised.index('"m"') < serialised.index('"z"')


def test_default_str():
    """default=str should serialise non-serialisable types via str()."""
    import datetime
    data = {"ts": datetime.date(2024, 1, 1)}
    result = loads(dumps(data, default=str))
    assert result["ts"] == "2024-01-01"


def test_dumps_bytes_returns_bytes():
    assert isinstance(dumps_bytes({"x": 1}), bytes)


def test_dumps_returns_str():
    assert isinstance(dumps({"x": 1}), str)


def test_loads_bytes_input():
    raw = b'{"hello": "world"}'
    assert loads(raw) == {"hello": "world"}


def test_json_decode_error_importable():
    from gateway.util.json_utils import JSONDecodeError
    import json as _stdlib_json
    # Both should be exception types that can be caught on bad input.
    try:
        loads("not valid json !!!")
    except JSONDecodeError:
        pass  # expected


def test_tool_hash_pattern():
    """Mirrors the exact call pattern used in orchestrator.py for tool hashing."""
    input_data = {"query": "orjson speed test", "results": 5}
    serialised = dumps(input_data, default=str, sort_keys=True)
    # Round-trip must preserve data.
    assert loads(serialised) == input_data
