"""Adaptive Gateway — self-configuring intelligence layer."""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)


_ALLOWED_CLASS_PREFIXES = ("gateway.", "walacor.")

def load_custom_class(dotted_path: str) -> type:
    """Load a custom class by dotted path, restricted to allowed packages."""
    if not any(dotted_path.startswith(p) for p in _ALLOWED_CLASS_PREFIXES):
        raise ValueError(
            f"Custom class '{dotted_path}' not allowed — must start with one of: {_ALLOWED_CLASS_PREFIXES}"
        )
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def parse_custom_paths(csv: str) -> list[str]:
    """Parse comma-separated dotted paths, stripping whitespace."""
    return [p.strip() for p in csv.split(",") if p.strip()]
