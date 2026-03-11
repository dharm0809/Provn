"""Tests for enriched governance response headers."""

from unittest.mock import MagicMock

from gateway.pipeline.orchestrator import _add_governance_headers, _summarize_content_analysis, _compute_budget_percent
from gateway.pipeline.forwarder import build_governance_sse_event


class TestAddGovernanceHeaders:
    def test_existing_headers_still_set(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp, execution_id="exec-1", attestation_id="att-1", chain_seq=3, policy_result="pass")
        assert resp.headers["x-walacor-execution-id"] == "exec-1"
        assert resp.headers["x-walacor-attestation-id"] == "att-1"
        assert resp.headers["x-walacor-chain-seq"] == "3"
        assert resp.headers["x-walacor-policy-result"] == "pass"

    def test_new_content_analysis_header(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp, content_analysis="pii_warn")
        assert resp.headers["x-walacor-content-analysis"] == "pii_warn"

    def test_new_budget_headers(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp, budget_remaining=5000, budget_percent=82)
        assert resp.headers["x-walacor-budget-remaining"] == "5000"
        assert resp.headers["x-walacor-budget-percent"] == "82"

    def test_new_model_id_header(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp, model_id="qwen3:4b")
        assert resp.headers["x-walacor-model-id"] == "qwen3:4b"

    def test_none_values_not_set(self):
        resp = MagicMock()
        resp.headers = {}
        _add_governance_headers(resp)
        assert "x-walacor-content-analysis" not in resp.headers
        assert "x-walacor-budget-remaining" not in resp.headers
        assert "x-walacor-budget-percent" not in resp.headers
        assert "x-walacor-model-id" not in resp.headers


class TestBuildGovernanceSseEvent:
    def test_includes_new_fields(self):
        event = build_governance_sse_event(
            execution_id="exec-1", content_analysis="clean",
            budget_remaining=1000, budget_percent=50, model_id="qwen3:4b",
        )
        text = event.decode()
        assert "event: governance" in text
        assert '"content_analysis": "clean"' in text
        assert '"budget_remaining": 1000' in text
        assert '"budget_percent": 50' in text
        assert '"model_id": "qwen3:4b"' in text

    def test_omits_none_fields(self):
        event = build_governance_sse_event(execution_id="exec-1")
        text = event.decode()
        assert "content_analysis" not in text
        assert "budget_remaining" not in text


class TestSummarizeContentAnalysis:
    def test_empty_decisions(self):
        assert _summarize_content_analysis([]) == "clean"

    def test_block_decision(self):
        assert _summarize_content_analysis([{"action": "block", "verdict": "toxic"}]) == "blocked"

    def test_pii_warn(self):
        assert _summarize_content_analysis([{"action": "warn", "verdict": "pii_detected"}]) == "pii_warn"

    def test_toxicity_warn(self):
        assert _summarize_content_analysis([{"action": "warn", "verdict": "toxic"}]) == "toxicity_warn"

    def test_pass_decisions(self):
        assert _summarize_content_analysis([{"action": "pass", "verdict": "pass"}]) == "clean"


class TestComputeBudgetPercent:
    def test_none_remaining(self):
        s = MagicMock()
        s.token_budget_max_tokens = 10000
        assert _compute_budget_percent(None, s) is None

    def test_unlimited(self):
        s = MagicMock()
        s.token_budget_max_tokens = 10000
        assert _compute_budget_percent(-1, s) is None

    def test_normal(self):
        s = MagicMock()
        s.token_budget_max_tokens = 10000
        # 2000 remaining out of 10000 = 8000 used = 80%
        assert _compute_budget_percent(2000, s) == 80

    def test_zero_max(self):
        s = MagicMock()
        s.token_budget_max_tokens = 0
        assert _compute_budget_percent(5000, s) is None
