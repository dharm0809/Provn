"""Anthropic request-translation coverage (12 cases).

Tests for translate_oai_chat_to_anthropic — the function that converts
OpenAI /v1/chat/completions bodies to Anthropic /v1/messages format.
"""
from __future__ import annotations
import pytest
from gateway.adapters.anthropic import translate_oai_chat_to_anthropic


def _translate(data: dict) -> dict:
    return translate_oai_chat_to_anthropic(data)


# ── 1. System message extraction ─────────────────────────────────────────────

def test_system_message_extracted_to_top_level() -> None:
    result = _translate({"model": "claude-3", "messages": [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]})
    assert result["system"] == "You are helpful."
    assert all(m["role"] != "system" for m in result["messages"])


def test_multiple_system_messages_joined() -> None:
    result = _translate({"model": "claude-3", "messages": [
        {"role": "system", "content": "Rule 1."},
        {"role": "system", "content": "Rule 2."},
        {"role": "user", "content": "Hi"},
    ]})
    assert "Rule 1." in result["system"]
    assert "Rule 2." in result["system"]


# ── 2. max_tokens default ─────────────────────────────────────────────────────

def test_max_tokens_defaults_when_absent() -> None:
    result = _translate({"model": "claude-3", "messages": [{"role": "user", "content": "Hi"}]})
    assert isinstance(result["max_tokens"], int)
    assert result["max_tokens"] > 0


def test_max_tokens_respected_when_provided() -> None:
    result = _translate({"model": "claude-3", "max_tokens": 512,
                         "messages": [{"role": "user", "content": "Hi"}]})
    assert result["max_tokens"] == 512


# ── 3. stop sequences ─────────────────────────────────────────────────────────

def test_stop_string_becomes_list() -> None:
    result = _translate({"model": "claude-3", "stop": "END",
                         "messages": [{"role": "user", "content": "Hi"}]})
    assert result["stop_sequences"] == ["END"]


def test_stop_list_passes_through() -> None:
    result = _translate({"model": "claude-3", "stop": ["END", "STOP"],
                         "messages": [{"role": "user", "content": "Hi"}]})
    assert result["stop_sequences"] == ["END", "STOP"]


# ── 4. reasoning_effort → thinking ────────────────────────────────────────────

def test_reasoning_effort_low_maps_to_thinking() -> None:
    result = _translate({"model": "claude-3", "reasoning_effort": "low",
                         "messages": [{"role": "user", "content": "Hi"}]})
    assert "thinking" in result
    assert result["thinking"]["type"] == "enabled"


def test_reasoning_effort_high_maps_to_higher_budget() -> None:
    low = _translate({"model": "claude-3", "reasoning_effort": "low",
                      "messages": [{"role": "user", "content": "Hi"}]})
    high = _translate({"model": "claude-3", "reasoning_effort": "high",
                       "messages": [{"role": "user", "content": "Hi"}]})
    assert high["thinking"]["budget_tokens"] > low["thinking"]["budget_tokens"]


def test_reasoning_effort_forces_temperature_one() -> None:
    result = _translate({"model": "claude-3", "reasoning_effort": "medium",
                         "temperature": 0.5,
                         "messages": [{"role": "user", "content": "Hi"}]})
    assert result["temperature"] == 1.0


# ── 5. tool definitions ───────────────────────────────────────────────────────

def test_tool_definitions_translated() -> None:
    result = _translate({"model": "claude-3", "messages": [{"role": "user", "content": "Hi"}],
                         "tools": [{"type": "function", "function": {
                             "name": "get_weather", "description": "Get weather",
                             "parameters": {"type": "object", "properties": {}}
                         }}]})
    assert "tools" in result
    assert result["tools"][0].get("name") == "get_weather"
    assert "input_schema" in result["tools"][0]


def test_tool_choice_auto_translated() -> None:
    result = _translate({"model": "claude-3", "messages": [{"role": "user", "content": "Hi"}],
                         "tool_choice": "auto"})
    assert result.get("tool_choice") == {"type": "auto"}


def test_tool_choice_specific_function_translated() -> None:
    result = _translate({"model": "claude-3", "messages": [{"role": "user", "content": "Hi"}],
                         "tool_choice": {"type": "function", "function": {"name": "get_weather"}}})
    assert result.get("tool_choice") == {"type": "tool", "name": "get_weather"}
