"""ShadowRunner.evict_old_sessions bounds candidate-session memory growth."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.intelligence.shadow import ShadowRunner


class _StubDB:
    path = ":memory:"


def test_evict_old_sessions_drops_non_current_versions():
    runner = ShadowRunner(_StubDB())
    runner._sessions[("intent", "v1")] = object()
    runner._sessions[("intent", "v2")] = object()
    runner._sessions[("safety", "v1")] = object()

    n = runner.evict_old_sessions("intent", current_version="v2")
    assert n == 1
    assert ("intent", "v1") not in runner._sessions
    assert ("intent", "v2") in runner._sessions
    # Other models untouched
    assert ("safety", "v1") in runner._sessions


def test_evict_old_sessions_with_none_drops_all_for_model():
    runner = ShadowRunner(_StubDB())
    runner._sessions[("intent", "v1")] = object()
    runner._sessions[("intent", "v2")] = object()
    runner._sessions[("safety", "v9")] = object()

    n = runner.evict_old_sessions("intent", current_version=None)
    assert n == 2
    assert all(k[0] != "intent" for k in runner._sessions)
    assert ("safety", "v9") in runner._sessions


def test_evict_old_sessions_unknown_model_raises():
    runner = ShadowRunner(_StubDB())
    with pytest.raises(ValueError):
        runner.evict_old_sessions("not_a_real_model", current_version="v1")


def test_evict_old_sessions_no_match_returns_zero():
    runner = ShadowRunner(_StubDB())
    runner._sessions[("intent", "v3")] = object()
    assert runner.evict_old_sessions("intent", current_version="v3") == 0
    assert runner._sessions  # untouched
