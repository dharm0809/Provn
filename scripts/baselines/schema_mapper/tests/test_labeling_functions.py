"""Each LF gets a positive case (it should fire on this field) and a
negative case (it should ABSTAIN on this field)."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from canonical_schema import LABEL_TO_ID  # noqa: E402
from labeling_functions import (  # noqa: E402
    ABSTAIN,
    LFS,
    apply_lfs_to_field,
    lf_cache_creation_token_key,
    lf_cached_token_key,
    lf_completion_token_key,
    lf_finish_reason_key,
    lf_iso_timestamp,
    lf_long_natural_string_is_content,
    lf_model_hash_key,
    lf_model_key,
    lf_prompt_token_key,
    lf_response_id_uuid,
    lf_timing_value_key,
    lf_token_arithmetic,
    lf_tool_call_arguments,
    lf_tool_call_name,
    lf_total_token_key,
    lf_url_in_citation_path,
)
from paths import FlatField  # noqa: E402


def _ff(path, value, depth=0, sibling_keys=()):
    key = path.rsplit(".", 1)[-1]
    return FlatField(path=path, key=key, value=value, siblings=tuple(sibling_keys), depth=depth)


def test_lf_token_arithmetic_pos():
    f = _ff("usage.total_tokens", 15)
    assert lf_token_arithmetic(f, {"sibling_int_values": [10, 5, 15]}) == LABEL_TO_ID["total_tokens"]


def test_lf_token_arithmetic_neg():
    f = _ff("usage.total_tokens", 99)
    assert lf_token_arithmetic(f, {"sibling_int_values": [10, 5, 99]}) == ABSTAIN


def test_lf_prompt_token_key_pos_and_neg():
    pos = _ff("usage.prompt_tokens", 10)
    neg = _ff("choices[0].message.content", "hi")
    assert lf_prompt_token_key(pos, {}) == LABEL_TO_ID["prompt_tokens"]
    assert lf_prompt_token_key(neg, {}) == ABSTAIN


def test_lf_completion_token_key_pos_and_neg():
    pos = _ff("usage.completionTokens", 5)
    neg = _ff("usage.cached_tokens", 5)
    assert lf_completion_token_key(pos, {}) == LABEL_TO_ID["completion_tokens"]
    assert lf_completion_token_key(neg, {}) == ABSTAIN


def test_lf_total_token_key_pos_and_neg():
    pos = _ff("usage.total_tokens", 15)
    neg = _ff("usage.prompt_tokens", 10)
    assert lf_total_token_key(pos, {}) == LABEL_TO_ID["total_tokens"]
    assert lf_total_token_key(neg, {}) == ABSTAIN


def test_lf_cached_token_key_pos_and_neg():
    pos = _ff("usage.cache_read_input_tokens", 4)
    neg = _ff("usage.prompt_tokens", 10)
    assert lf_cached_token_key(pos, {}) == LABEL_TO_ID["cached_tokens"]
    assert lf_cached_token_key(neg, {}) == ABSTAIN


def test_lf_cache_creation_token_key_pos_and_neg():
    pos = _ff("usage.cache_creation_input_tokens", 4)
    neg = _ff("usage.prompt_tokens", 10)
    assert lf_cache_creation_token_key(pos, {}) == LABEL_TO_ID["cache_creation_tokens"]
    assert lf_cache_creation_token_key(neg, {}) == ABSTAIN


def test_lf_finish_reason_pos_and_neg():
    pos = _ff("choices[0].finish_reason", "stop")
    neg = _ff("choices[0].message.content", "Hello, world!")
    assert lf_finish_reason_key(pos, {}) == LABEL_TO_ID["finish_reason"]
    assert lf_finish_reason_key(neg, {}) == ABSTAIN


def test_lf_response_id_uuid_pos_and_neg():
    pos = _ff("id", "550e8400-e29b-41d4-a716-446655440000", depth=0)
    pos2 = _ff("id", "chatcmpl-AbCdEf123456", depth=0)
    neg = _ff("choices[0].message.tool_calls[0].id", "550e8400-e29b-41d4-a716-446655440000", depth=4)
    assert lf_response_id_uuid(pos, {}) == LABEL_TO_ID["response_id"]
    assert lf_response_id_uuid(pos2, {}) == LABEL_TO_ID["response_id"]
    assert lf_response_id_uuid(neg, {}) == ABSTAIN


def test_lf_url_citation_pos_and_neg():
    pos = _ff("citations[0]", "https://example.com/source-a")
    neg = _ff("model", "https://example.com/model-id")  # not citation path
    assert lf_url_in_citation_path(pos, {}) == LABEL_TO_ID["citation_url"]
    assert lf_url_in_citation_path(neg, {}) == ABSTAIN


def test_lf_iso_timestamp_pos_and_neg():
    pos = _ff("created_at", "2026-04-27T15:00:00Z")
    pos_int = _ff("created", 1714233600, depth=0)
    neg = _ff("model", "gpt-4o")
    assert lf_iso_timestamp(pos, {}) == LABEL_TO_ID["response_timestamp"]
    assert lf_iso_timestamp(pos_int, {}) == LABEL_TO_ID["response_timestamp"]
    assert lf_iso_timestamp(neg, {}) == ABSTAIN


def test_lf_tool_call_name_pos_and_neg():
    pos = _ff("tool_calls[0].function.name", "get_weather")
    neg = _ff("model", "gpt-4o")
    assert lf_tool_call_name(pos, {}) == LABEL_TO_ID["tool_call_name"]
    assert lf_tool_call_name(neg, {}) == ABSTAIN


def test_lf_tool_call_arguments_pos_and_neg():
    pos = _ff("tool_calls[0].function.arguments", '{"x":1}')
    neg = _ff("usage.prompt_tokens", 10)
    assert lf_tool_call_arguments(pos, {}) == LABEL_TO_ID["tool_call_arguments"]
    assert lf_tool_call_arguments(neg, {}) == ABSTAIN


def test_lf_model_pos_and_neg():
    pos = _ff("model", "gpt-4o-2024-08-06")
    neg = _ff("system_fingerprint", "fp_xyz")
    assert lf_model_key(pos, {}) == LABEL_TO_ID["model"]
    assert lf_model_key(neg, {}) == ABSTAIN


def test_lf_model_hash_pos_and_neg():
    pos = _ff("system_fingerprint", "fp_a1b2c3")
    neg = _ff("model", "gpt-4o")
    assert lf_model_hash_key(pos, {}) == LABEL_TO_ID["model_hash"]
    assert lf_model_hash_key(neg, {}) == ABSTAIN


def test_lf_timing_value_pos_and_neg():
    pos = _ff("metrics.latencyMs", 1234)
    pos2 = _ff("usage.queue_time", 0.012)
    neg = _ff("usage.prompt_tokens", 10)
    assert lf_timing_value_key(pos, {}) == LABEL_TO_ID["timing_value"]
    assert lf_timing_value_key(pos2, {}) == LABEL_TO_ID["timing_value"]
    assert lf_timing_value_key(neg, {}) == ABSTAIN


def test_lf_long_natural_string_is_content():
    pos = _ff("choices[0].message.content", "Hello there, this is a longer assistant reply with several words.")
    neg_short = _ff("choices[0].message.content", "ok")
    neg_path = _ff("usage.prompt_tokens", "Hello world this is also long")  # wrong path
    assert lf_long_natural_string_is_content(pos, {}) == LABEL_TO_ID["content"]
    assert lf_long_natural_string_is_content(neg_short, {}) == ABSTAIN
    assert lf_long_natural_string_is_content(neg_path, {}) == ABSTAIN


def test_apply_all_lfs_returns_one_per_lf():
    f = _ff("usage.prompt_tokens", 10)
    votes = apply_lfs_to_field(f, {"sibling_int_values": [10, 5]})
    assert len(votes) == len(LFS)
