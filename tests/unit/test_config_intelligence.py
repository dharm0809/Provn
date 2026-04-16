from __future__ import annotations

import pytest

from gateway.config import Settings, get_settings


def test_bounds_rejected():
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        Settings(shadow_max_disagreement=1.5)
    with pytest.raises(ValueError):
        Settings(teacher_llm_sample_rate=-0.1)
    with pytest.raises(ValueError):
        Settings(verdict_retention_days=0)


def test_intelligence_defaults():
    get_settings.cache_clear()
    s = Settings()
    assert s.intelligence_enabled is True
    assert s.verdict_retention_days == 30
    assert s.distillation_min_divergences == 500
    assert s.shadow_sample_target == 1000
    assert s.shadow_min_accuracy_delta == 0.02
    assert s.shadow_max_disagreement == 0.40
    assert s.shadow_max_error_rate == 0.05
    assert s.auto_promote_models == ""
    assert s.teacher_llm_sample_rate == 0.01


def test_auto_promote_list():
    get_settings.cache_clear()
    s = Settings(auto_promote_models="intent,safety")
    assert s.auto_promote_models_list == ["intent", "safety"]
