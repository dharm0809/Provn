"""Adaptive Gateway — self-configuring intelligence layer."""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)


def load_custom_class(dotted_path: str) -> type:
    """Import a class by dotted path, e.g. 'mycompany.probes.DatadogProbe'."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def parse_custom_paths(csv: str) -> list[str]:
    """Parse comma-separated dotted paths, stripping whitespace."""
    return [p.strip() for p in csv.split(",") if p.strip()]
