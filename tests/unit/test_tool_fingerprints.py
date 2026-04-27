"""Pillar 2 — content fingerprinting + corroboration rule.

Verifies the §11.1 rule: never auto-stitch on a single low-entropy ``tc_hash``
match; require ``tr_hash``, ≥2 sequenced ``tc_hash``, ``traceparent``, or
shared ``caller_identity`` to declare a high/medium-confidence stitch.
"""

from __future__ import annotations

import sqlite3
import time

from gateway.pipeline.agent_reconstructor import ReconstructionEvent
from gateway.pipeline.tool_fingerprints import (
    StitchCandidate,
    find_stitches,
    fingerprints_from_recon_events,
    hash_tool_call,
    hash_tool_result,
)


# ── Hash determinism + cross-shape stability ──────────────────────────────────


def test_tool_call_hash_stable_across_arg_shapes():
    """OpenAI sends arguments as a JSON-encoded string; Anthropic as a dict.
    Both must produce the same tc_hash for the same semantic payload."""
    h_str = hash_tool_call("search", '{"q":"hi","limit":5}')
    h_dict = hash_tool_call("search", {"q": "hi", "limit": 5})
    h_reorder = hash_tool_call("search", {"limit": 5, "q": "hi"})
    assert h_str == h_dict == h_reorder


def test_different_tool_names_produce_different_hashes():
    a = hash_tool_call("search", {"q": "hi"})
    b = hash_tool_call("read", {"q": "hi"})
    assert a != b


def test_tool_result_hash_normalises_content():
    a = hash_tool_result({"x": 1, "y": 2})
    b = hash_tool_result({"y": 2, "x": 1})
    assert a == b


# ── Recon-event → fingerprint materialisation ────────────────────────────────


def test_fingerprints_from_events_separates_call_and_result():
    events = [
        ReconstructionEvent(
            kind="tool_call_observed", caller="c", turn_seq=1,
            tool_name="search", tool_call_id="t1", args_hash="aaaa",
        ),
        ReconstructionEvent(
            kind="tool_result_observed", caller="c", turn_seq=2,
            tool_call_id="t1", content_hash="bbbb",
        ),
        ReconstructionEvent(
            kind="new_user_turn", caller="c", turn_seq=3, content_hash="zzzz",
        ),
    ]
    rows = fingerprints_from_recon_events(
        events,
        tenant_id="t1", record_id="exec-1", caller_key="c",
        trace_id="tr1", agent_run_id="run-1", seen_at="2026-04-26T00:00:00Z",
    )
    assert len(rows) == 2  # new_user_turn doesn't fingerprint
    kinds = {r.kind for r in rows}
    assert kinds == {"tool_call", "tool_result"}
    call_row = next(r for r in rows if r.kind == "tool_call")
    assert call_row.tc_hash == "aaaa"
    assert call_row.tr_hash is None
    res_row = next(r for r in rows if r.kind == "tool_result")
    assert res_row.tr_hash == "bbbb"
    assert res_row.tc_hash is None


# ── §11.1 corroboration rule ─────────────────────────────────────────────────


def _row(**overrides):
    base = {
        "caller_key": None, "record_id": None,
        "tc_hash": None, "tr_hash": None, "trace_id": None,
    }
    base.update(overrides)
    return base


def test_single_tc_hash_match_is_low_confidence_only():
    candidates = find_stitches(
        tenant_id="t1", caller_key="me",
        tc_hashes=["A"], tr_hashes=[], trace_id=None,
        other_rows=[_row(caller_key="other", record_id="r2", tc_hash="A")],
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.confidence == "low"
    assert c.reasons == ("single_tc_hash",)


def test_two_sequenced_tc_hash_matches_promote_to_medium():
    candidates = find_stitches(
        tenant_id="t1", caller_key="me",
        tc_hashes=["A", "B"], tr_hashes=[], trace_id=None,
        other_rows=[
            _row(caller_key="other", tc_hash="A", record_id="r2"),
            _row(caller_key="other", tc_hash="B", record_id="r2"),
        ],
    )
    assert candidates[0].confidence == "medium"
    assert "multi_tc_hash_match" in candidates[0].reasons


def test_tr_hash_match_promotes_to_high():
    candidates = find_stitches(
        tenant_id="t1", caller_key="me",
        tc_hashes=["A"], tr_hashes=["X"], trace_id=None,
        other_rows=[
            _row(caller_key="other", tc_hash="A", record_id="r2"),
            _row(caller_key="other", tr_hash="X", record_id="r2"),
        ],
    )
    assert candidates[0].confidence == "high"
    assert "tr_hash_match" in candidates[0].reasons


def test_traceparent_overlap_alone_promotes_to_high():
    candidates = find_stitches(
        tenant_id="t1", caller_key="me",
        tc_hashes=[], tr_hashes=[], trace_id="abc",
        other_rows=[
            _row(caller_key="other", trace_id="abc", record_id="r2"),
        ],
    )
    # No tc/tr matches but traceparent overlap → high.
    assert len(candidates) == 1
    assert candidates[0].confidence == "high"
    assert "traceparent_match" in candidates[0].reasons


def test_self_caller_excluded():
    candidates = find_stitches(
        tenant_id="t1", caller_key="me",
        tc_hashes=["A", "B"], tr_hashes=[], trace_id=None,
        other_rows=[
            _row(caller_key="me", tc_hash="A", record_id="r1"),
            _row(caller_key="me", tc_hash="B", record_id="r1"),
        ],
    )
    assert candidates == []


def test_unrelated_callers_dont_match():
    candidates = find_stitches(
        tenant_id="t1", caller_key="me",
        tc_hashes=["A"], tr_hashes=["X"], trace_id="abc",
        other_rows=[
            _row(caller_key="other", tc_hash="Z", tr_hash="Y", trace_id="zzz"),
        ],
    )
    assert candidates == []


def test_results_sorted_high_then_medium_then_low():
    rows = [
        _row(caller_key="lo", tc_hash="A"),
        _row(caller_key="med", tc_hash="A"),
        _row(caller_key="med", tc_hash="B"),
        _row(caller_key="hi", tr_hash="X"),
    ]
    candidates = find_stitches(
        tenant_id="t1", caller_key="me",
        tc_hashes=["A", "B"], tr_hashes=["X"], trace_id=None,
        other_rows=rows,
    )
    confidences = [c.confidence for c in candidates]
    assert confidences == ["high", "medium", "low"]


# ── WAL persistence ──────────────────────────────────────────────────────────


def test_fingerprint_persists_to_table(tmp_path):
    from gateway.wal.writer import WALWriter

    db_path = str(tmp_path / "wal.db")
    writer = WALWriter(db_path)
    writer.start()
    try:
        writer.enqueue_write_fingerprint({
            "fp_id": "fp1",
            "tenant_id": "t1",
            "record_id": "exec-1",
            "caller_key": "ck",
            "tool_call_id": "t1",
            "tool_name": "search",
            "tc_hash": "deadbeef",
            "tr_hash": None,
            "trace_id": "tr-1",
            "agent_run_id": "run-1",
            "kind": "tool_call",
            "seen_at": "2026-04-26T00:00:00Z",
        })
        time.sleep(0.3)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM tool_fingerprints WHERE fp_id=?", ("fp1",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["tc_hash"] == "deadbeef"
        assert row["kind"] == "tool_call"
    finally:
        writer.stop()
