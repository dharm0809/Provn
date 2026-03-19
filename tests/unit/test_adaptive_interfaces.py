"""Tests for adaptive gateway package foundation: ABCs, dataclasses, and class loader."""

from __future__ import annotations

import pytest

from gateway.adaptive import load_custom_class, parse_custom_paths
from gateway.adaptive.interfaces import (
    CapabilityProbe,
    IdentityValidator,
    ProbeResult,
    RequestClassifier,
    ResourceMonitor,
    ResourceStatus,
    StartupProbe,
    ValidationResult,
)


class TestLoadCustomClass:
    def test_load_custom_class_valid(self):
        cls = load_custom_class("gateway.adaptive.interfaces.StartupProbe")
        assert cls is StartupProbe

    def test_load_custom_class_invalid_module(self):
        with pytest.raises(ValueError, match="not allowed"):
            load_custom_class("nonexistent.module.SomeClass")

    def test_load_custom_class_invalid_class(self):
        with pytest.raises(AttributeError):
            load_custom_class("gateway.adaptive.interfaces.NonExistentClass")


class TestParseCustomPaths:
    def test_parse_basic(self):
        result = parse_custom_paths("a.B, c.D")
        assert result == ["a.B", "c.D"]

    def test_parse_empty(self):
        assert parse_custom_paths("") == []

    def test_parse_whitespace_only(self):
        assert parse_custom_paths("  ,  , ") == []


class TestDataclasses:
    def test_probe_result_dataclass(self):
        pr = ProbeResult(name="db", healthy=True, detail={"latency_ms": 5})
        assert pr.name == "db"
        assert pr.healthy is True
        assert pr.detail == {"latency_ms": 5}

    def test_validation_result_dataclass(self):
        vr = ValidationResult(
            valid=True, identity="alice", source="jwt", warnings=["clock skew"]
        )
        assert vr.valid is True
        assert vr.identity == "alice"
        assert vr.source == "jwt"
        assert vr.warnings == ["clock skew"]

    def test_resource_status_dataclass(self):
        rs = ResourceStatus(
            disk_free_pct=85.0,
            disk_healthy=True,
            active_requests=42,
            provider_error_rates={"openai": 0.01},
        )
        assert rs.disk_free_pct == 85.0
        assert rs.disk_healthy is True
        assert rs.active_requests == 42
        assert rs.provider_error_rates == {"openai": 0.01}


class TestABCs:
    def test_startup_probe_is_abstract(self):
        with pytest.raises(TypeError):
            StartupProbe()  # type: ignore[abstract]

    def test_request_classifier_is_abstract(self):
        with pytest.raises(TypeError):
            RequestClassifier()  # type: ignore[abstract]

    def test_identity_validator_is_abstract(self):
        with pytest.raises(TypeError):
            IdentityValidator()  # type: ignore[abstract]

    def test_resource_monitor_is_abstract(self):
        with pytest.raises(TypeError):
            ResourceMonitor()  # type: ignore[abstract]
