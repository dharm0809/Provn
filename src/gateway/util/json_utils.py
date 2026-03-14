"""JSON utilities — uses orjson when available, falls back to stdlib json.

Drop-in replacements for json.loads / json.dumps / json.dumps(...).encode().
The dumps() signature accepts sort_keys and default kwargs for compatibility
with call-sites that hash tool input/output (orchestrator.py).
"""

from __future__ import annotations

try:
    import orjson as _orjson

    def loads(data: str | bytes) -> object:
        return _orjson.loads(data)

    def dumps(obj: object, *, sort_keys: bool = False, default=None) -> str:
        option = _orjson.OPT_SORT_KEYS if sort_keys else None
        if default is not None:
            return _orjson.dumps(obj, option=option, default=default).decode("utf-8")
        return _orjson.dumps(obj, option=option).decode("utf-8")

    def dumps_bytes(obj: object, *, sort_keys: bool = False, default=None) -> bytes:
        option = _orjson.OPT_SORT_KEYS if sort_keys else None
        if default is not None:
            return _orjson.dumps(obj, option=option, default=default)
        return _orjson.dumps(obj, option=option)

    #: JSONDecodeError re-exported so callers can catch it uniformly.
    JSONDecodeError = _orjson.JSONDecodeError  # type: ignore[attr-defined]

except ImportError:
    import json as _json

    def loads(data: str | bytes) -> object:  # type: ignore[misc]
        return _json.loads(data)

    def dumps(obj: object, *, sort_keys: bool = False, default=None) -> str:  # type: ignore[misc]
        return _json.dumps(obj, sort_keys=sort_keys, default=default)

    def dumps_bytes(obj: object, *, sort_keys: bool = False, default=None) -> bytes:  # type: ignore[misc]
        return _json.dumps(obj, sort_keys=sort_keys, default=default).encode("utf-8")

    JSONDecodeError = _json.JSONDecodeError
