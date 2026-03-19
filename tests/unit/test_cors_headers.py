"""Tests for CORS headers including Access-Control-Expose-Headers and dynamic origin checking."""

from unittest.mock import MagicMock

from gateway.main import _CORS_BASE_HEADERS, _get_cors_headers


class TestCorsBaseHeaders:
    def test_expose_headers_present(self):
        assert "Access-Control-Expose-Headers" in _CORS_BASE_HEADERS

    def test_expose_headers_includes_walacor(self):
        expose = _CORS_BASE_HEADERS["Access-Control-Expose-Headers"]
        assert "x-walacor-execution-id" in expose
        assert "x-walacor-attestation-id" in expose
        assert "x-walacor-chain-seq" in expose
        assert "x-walacor-policy-result" in expose
        assert "x-walacor-content-analysis" in expose
        assert "x-walacor-budget-remaining" in expose
        assert "x-walacor-budget-percent" in expose
        assert "x-walacor-model-id" in expose

    def test_no_allow_origin_in_base(self):
        """Base headers must not contain Allow-Origin — it is set dynamically."""
        assert "Access-Control-Allow-Origin" not in _CORS_BASE_HEADERS


class TestDynamicCorsOrigin:
    """Test _get_cors_headers origin checking with various config values."""

    @staticmethod
    def _make_request(origin: str | None = None) -> MagicMock:
        req = MagicMock()
        headers = {}
        if origin is not None:
            headers["origin"] = origin
        req.headers = headers
        return req

    def test_empty_config_no_allow_origin(self, monkeypatch):
        """Empty cors_allowed_origins -> same-origin only (no Allow-Origin header)."""
        monkeypatch.setenv("WALACOR_CORS_ALLOWED_ORIGINS", "")
        from gateway.config import get_settings
        get_settings.cache_clear()
        try:
            headers = _get_cors_headers(self._make_request("https://evil.com"))
            assert "Access-Control-Allow-Origin" not in headers
            assert "Access-Control-Allow-Methods" in headers
        finally:
            get_settings.cache_clear()

    def test_wildcard_config(self, monkeypatch):
        """cors_allowed_origins='*' -> wildcard Allow-Origin (backward compat)."""
        monkeypatch.setenv("WALACOR_CORS_ALLOWED_ORIGINS", "*")
        from gateway.config import get_settings
        get_settings.cache_clear()
        try:
            headers = _get_cors_headers(self._make_request("https://anything.com"))
            assert headers["Access-Control-Allow-Origin"] == "*"
        finally:
            get_settings.cache_clear()

    def test_matching_origin_reflected(self, monkeypatch):
        """Request Origin in allowlist -> reflected in Allow-Origin + Vary header."""
        monkeypatch.setenv("WALACOR_CORS_ALLOWED_ORIGINS", "https://app.example.com,https://admin.example.com")
        from gateway.config import get_settings
        get_settings.cache_clear()
        try:
            headers = _get_cors_headers(self._make_request("https://app.example.com"))
            assert headers["Access-Control-Allow-Origin"] == "https://app.example.com"
            assert headers.get("Vary") == "Origin"
        finally:
            get_settings.cache_clear()

    def test_non_matching_origin_blocked(self, monkeypatch):
        """Request Origin NOT in allowlist -> no Allow-Origin header."""
        monkeypatch.setenv("WALACOR_CORS_ALLOWED_ORIGINS", "https://app.example.com")
        from gateway.config import get_settings
        get_settings.cache_clear()
        try:
            headers = _get_cors_headers(self._make_request("https://evil.com"))
            assert "Access-Control-Allow-Origin" not in headers
        finally:
            get_settings.cache_clear()

    def test_no_origin_header_no_allow_origin(self, monkeypatch):
        """Request without Origin header -> no Allow-Origin even with allowlist."""
        monkeypatch.setenv("WALACOR_CORS_ALLOWED_ORIGINS", "https://app.example.com")
        from gateway.config import get_settings
        get_settings.cache_clear()
        try:
            headers = _get_cors_headers(self._make_request())
            assert "Access-Control-Allow-Origin" not in headers
        finally:
            get_settings.cache_clear()

    def test_trailing_slash_normalization(self, monkeypatch):
        """Trailing slashes are stripped for comparison."""
        monkeypatch.setenv("WALACOR_CORS_ALLOWED_ORIGINS", "https://app.example.com/")
        from gateway.config import get_settings
        get_settings.cache_clear()
        try:
            headers = _get_cors_headers(self._make_request("https://app.example.com"))
            assert headers["Access-Control-Allow-Origin"] == "https://app.example.com"
        finally:
            get_settings.cache_clear()
