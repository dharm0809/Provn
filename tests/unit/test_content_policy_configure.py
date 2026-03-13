"""Tests for dynamic content analyzer configuration."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from gateway.content.pii_detector import PIIDetector
from gateway.content.toxicity_detector import ToxicityDetector
from gateway.content.llama_guard import LlamaGuardAnalyzer
from gateway.content.base import Verdict

anyio_backend = ["asyncio"]


class TestPIIDetectorConfigure:
    def test_configure_changes_block_types(self):
        d = PIIDetector()
        d.configure([
            {"category": "email_address", "action": "block"},
            {"category": "credit_card", "action": "warn"},
        ])
        assert "email_address" in d._block_types
        assert "credit_card" not in d._block_types

    @pytest.mark.anyio
    async def test_configured_block_type_blocks(self):
        d = PIIDetector()
        d.configure([{"category": "email_address", "action": "block"}])
        result = await d.analyze("contact us at admin@example.com")
        assert result.verdict == Verdict.BLOCK

    @pytest.mark.anyio
    async def test_configured_pass_type_passes(self):
        d = PIIDetector()
        d.configure([{"category": "credit_card", "action": "pass"}])
        result = await d.analyze("card 4111111111111111")
        assert result.verdict == Verdict.PASS


class TestToxicityDetectorConfigure:
    def test_configure_changes_block_categories(self):
        d = ToxicityDetector()
        d.configure([
            {"category": "violence", "action": "block"},
            {"category": "child_safety", "action": "warn"},
        ])
        assert "violence" in d._block_categories
        assert "child_safety" not in d._block_categories

    @pytest.mark.anyio
    async def test_default_behavior_without_configure(self):
        d = ToxicityDetector()
        # child_safety should block by default
        result = await d.analyze("csam content here")
        assert result.verdict == Verdict.BLOCK


class TestLlamaGuardConfigure:
    def test_configure_changes_category_actions(self):
        d = LlamaGuardAnalyzer(ollama_url="http://localhost:11434")
        d.configure([
            {"category": "S1", "action": "block"},
            {"category": "S4", "action": "warn"},
        ])
        assert d._category_actions.get("S1") == "block"
        assert d._category_actions.get("S4") == "warn"
