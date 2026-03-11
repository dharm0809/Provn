"""Tests for CORS headers including Access-Control-Expose-Headers."""

from gateway.main import _CORS_HEADERS


class TestCorsHeaders:
    def test_expose_headers_present(self):
        assert "Access-Control-Expose-Headers" in _CORS_HEADERS

    def test_expose_headers_includes_walacor(self):
        expose = _CORS_HEADERS["Access-Control-Expose-Headers"]
        assert "x-walacor-execution-id" in expose
        assert "x-walacor-attestation-id" in expose
        assert "x-walacor-chain-seq" in expose
        assert "x-walacor-policy-result" in expose
        assert "x-walacor-content-analysis" in expose
        assert "x-walacor-budget-remaining" in expose
        assert "x-walacor-budget-percent" in expose
        assert "x-walacor-model-id" in expose
