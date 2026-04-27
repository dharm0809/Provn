"""Canonical path manipulation for schema-mapper training + runtime.

The two functions defined here are the SINGLE source of truth for:
  * how a JSON dict is flattened into per-leaf FlatField records
  * how a leaf path is reduced to its direct parent-object path
    (used by the CRF EXCLUSIVE_GROUPS check; see canonical_schema.py)

Both training-time code (provider-spec validation, linearization,
synthesize.py) and runtime code (gateway/schema/mapper.py via the
linearization stub) MUST import from here so the conventions stay
identical across the boundary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlatField:
    """One leaf in a flattened JSON dict.

    Attributes:
        path:     dotted-with-bracket-index path string, e.g.
                  ``choices[0].message.tool_calls[0].function.name``
        key:      trailing leaf segment with any [N] suffix stripped, e.g.
                  ``name`` for the path above; ``content`` for ``content[0]``
        value:    the leaf value as it appears in the source JSON
        siblings: sorted-alphabetically list of leaf keys directly under
                  the same parent object (excluding self). For arrays, the
                  parent object is the element dict (e.g. ``message`` for
                  ``choices[0].message.role``).
        depth:    integer depth (0 = top-level key)
    """

    path: str
    key: str
    value: Any
    siblings: tuple[str, ...]
    depth: int


_TRAILING_INDEX_RE = re.compile(r"\[\d+\]$")


def parent_object_path(path: str) -> str:
    """Strip the trailing leaf segment + any trailing [N] index.

    Examples:
        choices[0].message.content       -> choices[0].message
        content[0].thinking              -> content[0]
        content[1].text                  -> content[1]
        usage.prompt_tokens              -> usage
        prompt_tokens                    -> ""              (top-level)
        choices[0].message.tool_calls[0].function.name
                                         -> choices[0].message.tool_calls[0].function

    The "parent object path" is the path of the dict that DIRECTLY contains
    the leaf — for the CRF EXCLUSIVE_GROUPS check, two fields are mutually
    exclusive ONLY if their parent_object_path values are identical.
    """
    if not path:
        return ""
    last_dot = path.rfind(".")
    if last_dot == -1:
        return ""
    return path[:last_dot]


def flatten_json(obj: Any, _prefix: str = "", _depth: int = 0) -> list[FlatField]:
    """Walk a JSON-shaped Python value and return one FlatField per leaf.

    A "leaf" is anything that is not a non-empty dict and not a non-empty
    list. Scalars (str/int/float/bool/None) ARE leaves; empty lists and
    empty dicts ARE leaves (they have a value of `[]` / `{}` and a
    well-defined path). Only non-empty containers are recursed into.

    This convention preserves "this path exists and has empty contents"
    as a signal — the schema-mapper sees `prompt: []` or `tool_calls: null`
    and learns to label them appropriately (typically UNKNOWN).

    Sibling computation:
        For a leaf at path ``parent.key`` (or ``parent[N].key``), siblings
        are the OTHER leaf-or-non-empty-container keys directly under
        ``parent`` — i.e. the dict-keys at the same level, minus self.
        Array siblings cross over: ``content[0].thinking`` has the
        sibling list of content[0]'s OWN dict keys (e.g. {"signature",
        "thinking", "type"}, minus "thinking" itself).
    """
    out: list[FlatField] = []
    _walk(obj, _prefix, _depth, out)
    return out


def _walk(value: Any, prefix: str, depth: int, out: list[FlatField]) -> None:
    if isinstance(value, dict):
        if not value:
            # empty dict — emit no leaves (intentionally; matches the
            # provider-spec convention used by the curators)
            return
        siblings_at_this_level = tuple(sorted(value.keys()))
        for k, v in value.items():
            child_prefix = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and v:
                _walk(v, child_prefix, depth + 1, out)
            elif isinstance(v, list) and v:
                _walk(v, child_prefix, depth + 1, out)
            else:
                # leaf (incl. empty list/dict, None, scalars)
                key_no_index = _TRAILING_INDEX_RE.sub("", k)
                sibs = tuple(s for s in siblings_at_this_level if s != k)
                out.append(
                    FlatField(
                        path=child_prefix,
                        key=key_no_index,
                        value=v,
                        siblings=sibs,
                        depth=depth,
                    )
                )
    elif isinstance(value, list):
        if not value:
            return
        for i, item in enumerate(value):
            child_prefix = f"{prefix}[{i}]"
            if isinstance(item, dict) and item:
                _walk(item, child_prefix, depth + 1, out)
            elif isinstance(item, list) and item:
                _walk(item, child_prefix, depth + 1, out)
            else:
                # array of scalars — synthesize a leaf
                # parent object is the array's container; siblings are the
                # OTHER items in the array (rare for our schema).
                out.append(
                    FlatField(
                        path=child_prefix,
                        key=_last_segment_key(prefix),
                        value=item,
                        siblings=(),
                        depth=depth,
                    )
                )
    else:
        # top-level scalar — emit a single leaf with empty path? In our
        # provider-spec format the top-level is always a dict, so this
        # branch is defensive only.
        out.append(
            FlatField(path=prefix, key=prefix, value=value, siblings=(), depth=depth)
        )


def _last_segment_key(path: str) -> str:
    """Strip leading dotted prefix; return the last named segment."""
    if not path:
        return ""
    last_dot = path.rfind(".")
    seg = path if last_dot == -1 else path[last_dot + 1 :]
    return _TRAILING_INDEX_RE.sub("", seg)
