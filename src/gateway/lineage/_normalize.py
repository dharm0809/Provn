"""Shared record normalization for lineage readers.

Two jobs:
(a) Synthesize record_id for legacy records that pre-date the ID-pointer
    chain. Uses the first 32 chars of record_hash as a deterministic
    stable identifier. This is a string slice, not a hash computation.
(b) Promote Walacor blockchain envelope fields (BlockId, TransId, DH, BL,
    CreatedAt) from the '$lookup as env' sub-array to top-level keys so
    the dashboard and compliance export have a uniform schema.
"""
from __future__ import annotations
from typing import Any


def _legacy_id(hash_str: str | None) -> str | None:
    if not hash_str:
        return None
    return f"legacy:{hash_str[:32]}"


def normalize_record(r: dict[str, Any]) -> dict[str, Any]:
    """Promote Walacor envelope fields to top-level and synthesize record_id
    for legacy records. Safe to call on already-new records (no-op for IDs)."""
    if r.get("record_id") is None and r.get("record_hash"):
        r["record_id"] = _legacy_id(r["record_hash"])
    if r.get("previous_record_id") is None:
        r["previous_record_id"] = _legacy_id(r.get("previous_record_hash"))
    env_list = r.pop("env", None) or []
    env = env_list[0] if env_list else {}
    r["walacor_block_id"] = env.get("BlockId")
    r["walacor_trans_id"] = env.get("TransId")
    r["walacor_dh"] = env.get("DH")
    r["walacor_block_level"] = env.get("BL")
    r["walacor_created_at"] = env.get("CreatedAt")
    return r
