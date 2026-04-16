"""Intelligence layer for the gateway.

Original scope (Apr 2026): background LLM enrichment via local Ollama models.
Phase 25 adds a self-learning feedback loop on top: ONNX verdict capture,
shadow-mode candidate validation, and audit-chain anchored model promotion.
"""
from __future__ import annotations

from gateway.intelligence.types import ModelVerdict

__all__ = ["ModelVerdict"]
