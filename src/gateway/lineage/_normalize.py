"""Shared record normalization for lineage readers.

Three jobs:
(a) Synthesize record_id for legacy records that pre-date the ID-pointer
    chain. Uses the first 32 chars of record_hash as a deterministic
    stable identifier. This is a string slice, not a hash computation.
(b) Promote Walacor blockchain envelope fields from the '$lookup as env'
    sub-array to top-level keys so the dashboard and compliance export
    have a uniform schema.
(c) Layer in OCM hash/anchor fields (DH, ES, SL, BlockId, TransId) from
    the separate /envelopes/hashes endpoint when a lookup map is provided.
    Anchor proof lives in that hashes collection, NOT in the data table —
    the $lookup envelope sub-array carries metadata (EId, UID, CreatedAt)
    but never BlockId/TransId/DH. Failing to consult /envelopes/hashes
    is why the gateway previously reported 0% anchored despite Walacor
    cryptographically anchoring every record on submit.
"""
from __future__ import annotations
from typing import Any


def _legacy_id(hash_str: str | None) -> str | None:
    if not hash_str:
        return None
    return f"legacy:{hash_str[:32]}"


def normalize_record(
    r: dict[str, Any],
    *,
    hash_lookup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Promote Walacor envelope + hash fields to top-level and synthesize
    record_id for legacy records.

    Args:
        r: A raw record from the data table, optionally enriched with an
            `env` sub-array (envelope-table lookup) and an `EId`.
        hash_lookup: Optional `{EId: hash_record}` map produced by
            `WalacorLineageReader._build_hash_lookup()`. When supplied,
            DH/ES/SL (and BlockId/TransId if present on the hash record)
            are pulled from here. This is the authoritative anchor source.

    Returns the same dict, mutated in place, with these added keys:
        walacor_block_id, walacor_trans_id, walacor_dh, walacor_block_level,
        walacor_created_at, walacor_es, walacor_sl.
    """
    if r.get("record_id") is None and r.get("record_hash"):
        r["record_id"] = _legacy_id(r["record_hash"])
    if r.get("previous_record_id") is None:
        r["previous_record_id"] = _legacy_id(r.get("previous_record_hash"))
    env_list = r.pop("env", None) or []
    env = env_list[0] if env_list else {}
    # Envelope-table fields (always populated by Walacor on submit).
    r["walacor_block_level"] = env.get("BL")
    r["walacor_created_at"] = env.get("CreatedAt")

    # OCM hash/anchor fields — sourced from /envelopes/hashes when supplied,
    # otherwise fall back to the envelope sub-array for backward
    # compatibility with older code paths.
    hash_rec: dict[str, Any] = {}
    eid = r.get("EId") or r.get("_walacor_eid")
    if hash_lookup and eid:
        hash_rec = hash_lookup.get(eid) or {}
    r["walacor_dh"] = hash_rec.get("DH") if hash_rec else env.get("DH")
    r["walacor_block_id"] = hash_rec.get("BlockId") if hash_rec else env.get("BlockId")
    r["walacor_trans_id"] = hash_rec.get("TransId") if hash_rec else env.get("TransId")
    r["walacor_es"] = hash_rec.get("ES")
    r["walacor_sl"] = hash_rec.get("SL")
    return r
