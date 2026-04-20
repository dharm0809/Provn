"""Tests for lineage record normalization: legacy record_id synthesis + Walacor envelope promotion."""
from __future__ import annotations
from gateway.lineage._normalize import normalize_record


def test_legacy_record_gets_synthesized_record_id() -> None:
    raw = {"record_hash": "a" * 128, "previous_record_hash": "b" * 128}
    normalized = normalize_record(raw)
    assert normalized["record_id"] == "legacy:" + "a" * 32
    assert normalized["previous_record_id"] == "legacy:" + "b" * 32


def test_legacy_synthesis_is_deterministic() -> None:
    raw = {"record_hash": "c" * 128}
    assert normalize_record(dict(raw))["record_id"] == normalize_record(dict(raw))["record_id"]


def test_new_record_passes_through_unchanged() -> None:
    raw = {"record_id": "rec-1", "previous_record_id": "rec-0"}
    normalized = normalize_record(raw)
    assert normalized["record_id"] == "rec-1"
    assert normalized["previous_record_id"] == "rec-0"


def test_first_record_in_session_has_no_previous() -> None:
    raw = {"record_hash": "d" * 128}
    assert normalize_record(raw)["previous_record_id"] is None


def test_walacor_envelope_promoted_to_top_level() -> None:
    raw = {
        "record_id": "rec-1",
        "env": [{"BlockId": "blk-1", "TransId": "tx-1", "DH": "dh-abc", "BL": 3, "CreatedAt": "2026-01-01"}],
    }
    r = normalize_record(raw)
    assert r["walacor_block_id"] == "blk-1"
    assert r["walacor_trans_id"] == "tx-1"
    assert r["walacor_dh"] == "dh-abc"
    assert r["walacor_block_level"] == 3
    assert r["walacor_created_at"] == "2026-01-01"
    assert "env" not in r


def test_wal_reader_emits_none_for_envelope_fields() -> None:
    raw = {"record_id": "rec-1"}  # no env key
    r = normalize_record(raw)
    assert r["walacor_block_id"] is None
    assert r["walacor_trans_id"] is None
    assert r["walacor_dh"] is None
    assert r["walacor_block_level"] is None
    assert r["walacor_created_at"] is None


def test_empty_env_list_emits_none_for_envelope_fields() -> None:
    raw = {"record_id": "rec-1", "env": []}
    r = normalize_record(raw)
    assert r["walacor_block_id"] is None


def test_record_without_hash_or_id_gets_none_record_id() -> None:
    raw = {"execution_id": "exec-1"}
    r = normalize_record(raw)
    assert r.get("record_id") is None
    assert r.get("previous_record_id") is None
