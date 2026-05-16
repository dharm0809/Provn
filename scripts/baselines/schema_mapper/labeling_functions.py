"""Stage C — Snorkel labeling functions for the schema-mapper.

Twelve labeling functions covering the highest-frequency / highest-precision
heuristics surfaced during Phase 2 spec curation. WRENCH benchmarks (NeurIPS
2021) show diminishing returns past ~15 LFs once a LabelModel fuses them, so
we cap here.

Each LF returns either an int label-id (vote) or ABSTAIN (-1). LFs do NOT
need to vote on every field; abstention is fine and Snorkel's LabelModel
weights them by accuracy + coverage.

Convention: lf_<short_name>(field, ctx) where ctx is a dict carrying any
extra info (sibling values keyed by leaf-key, etc.). All LFs are pure.

Fusion (when run as a script):
    /tmp/schema-mapper-venv/bin/python labeling_functions.py \\
        --in out/data/synthesized_train.jsonl \\
        --out out/data/lf_labels.jsonl

writes one row per (variant_id, path) with the fused soft label vector.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any, Callable

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from canonical_schema import CANONICAL_LABELS, LABEL_TO_ID  # noqa: E402
from paths import flatten_json  # noqa: E402

ABSTAIN = -1


# ── Regex helpers (compile once) ─────────────────────────────────────────────

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")
_URL_RE = re.compile(r"^https?://", re.I)
_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_PROMPT_KEY_RE = re.compile(r"prompt[_-]?(token|tok)|input[_-]?(token|tok)|prompt_eval", re.I)
_COMPL_KEY_RE = re.compile(r"completion[_-]?(token|tok)|output[_-]?(token|tok)|eval[_-]?count(?!.*prompt)", re.I)
_TOTAL_KEY_RE = re.compile(r"^total[_-]?(token|tok)|^totaltoken|tokens[_-]?total$", re.I)
_CACHED_KEY_RE = re.compile(r"cache[_-]?(read|hit)|cached[_-]?(token|tok)|cache_read_input", re.I)
_CACHE_CREATE_RE = re.compile(r"cache[_-]?(write|creat|store)|cache_creation_input", re.I)
_FINISH_KEY_RE = re.compile(r"^(finish|stop|done|complet(e|ion))[_-]?(reason|cause)?$|^stopReason$|^doneReason$", re.I)
_TOOL_NAME_KEY_RE = re.compile(r"function\.name$|^tool[_-]?name$", re.I)
_TOOL_ARGS_KEY_RE = re.compile(r"function\.arguments|^tool[_-]?(args|arguments)", re.I)
_TOOL_TYPE_KEY_RE = re.compile(r"tool[_-]?(type)$|^type$", re.I)
_MODEL_KEY_RE = re.compile(r"^model$|^modelVersion$|^modelid$|^model_id$", re.I)
_MODEL_HASH_RE = re.compile(r"system_?fingerprint|model_?(hash|fingerprint|version)$", re.I)
_TIMING_KEY_RE = re.compile(r"(latency|duration)[_-]?(ms|us|s)?$|.*_(ms|us|seconds?)$|.*_time$", re.I)


# ── LFs ─────────────────────────────────────────────────────────────────────


def _last_path_seg(path: str) -> str:
    """Return the trailing segment of a path with [N] suffix stripped."""
    seg = path.rsplit(".", 1)[-1]
    return re.sub(r"\[\d+\]$", "", seg)


def lf_token_arithmetic(field, ctx) -> int:
    """Three int siblings where one == sum of the other two: that one is total_tokens."""
    val = field.value
    if not isinstance(val, int) or isinstance(val, bool):
        return ABSTAIN
    sib_ints = [v for v in ctx.get("sibling_int_values", []) if v != val]
    if len(sib_ints) >= 2:
        for i in range(len(sib_ints)):
            for j in range(i + 1, len(sib_ints)):
                if sib_ints[i] + sib_ints[j] == val and val > 1:
                    return LABEL_TO_ID["total_tokens"]
    return ABSTAIN


def lf_prompt_token_key(field, ctx) -> int:
    if isinstance(field.value, int) and not isinstance(field.value, bool) and _PROMPT_KEY_RE.search(_last_path_seg(field.path)):
        return LABEL_TO_ID["prompt_tokens"]
    return ABSTAIN


def lf_completion_token_key(field, ctx) -> int:
    if isinstance(field.value, int) and not isinstance(field.value, bool) and _COMPL_KEY_RE.search(_last_path_seg(field.path)):
        return LABEL_TO_ID["completion_tokens"]
    return ABSTAIN


def lf_total_token_key(field, ctx) -> int:
    if isinstance(field.value, int) and not isinstance(field.value, bool) and _TOTAL_KEY_RE.search(_last_path_seg(field.path)):
        return LABEL_TO_ID["total_tokens"]
    return ABSTAIN


def lf_cached_token_key(field, ctx) -> int:
    if isinstance(field.value, int) and not isinstance(field.value, bool) and _CACHED_KEY_RE.search(field.path):
        return LABEL_TO_ID["cached_tokens"]
    return ABSTAIN


def lf_cache_creation_token_key(field, ctx) -> int:
    if isinstance(field.value, int) and not isinstance(field.value, bool) and _CACHE_CREATE_RE.search(field.path):
        return LABEL_TO_ID["cache_creation_tokens"]
    return ABSTAIN


def lf_finish_reason_key(field, ctx) -> int:
    """Enum-like string at a finish-reason-shaped key path."""
    if isinstance(field.value, str) and _FINISH_KEY_RE.match(_last_path_seg(field.path)):
        if len(field.value) <= 30 and " " not in field.value:
            return LABEL_TO_ID["finish_reason"]
    if field.value is None and _FINISH_KEY_RE.match(_last_path_seg(field.path)):
        return LABEL_TO_ID["finish_reason"]
    return ABSTAIN


def lf_response_id_uuid(field, ctx) -> int:
    """Top-level (depth=0) UUID-shaped string under a *_id-ish key."""
    if not isinstance(field.value, str) or field.depth != 0:
        return ABSTAIN
    seg = _last_path_seg(field.path)
    if seg in ("id", "responseId", "response_id", "request_id"):
        if _UUID_RE.match(field.value) or _HEX_RE.match(field.value) or field.value.startswith(("chatcmpl-", "msg_", "resp_")):
            return LABEL_TO_ID["response_id"]
    return ABSTAIN


def lf_url_in_citation_path(field, ctx) -> int:
    """URL value under a citations[*] / citation_url path."""
    if isinstance(field.value, str) and _URL_RE.match(field.value):
        if "citation" in field.path.lower() or "source" in field.path.lower() or field.key == "url":
            return LABEL_TO_ID["citation_url"]
    return ABSTAIN


def lf_iso_timestamp(field, ctx) -> int:
    if isinstance(field.value, str) and _ISO_TS_RE.match(field.value):
        if any(s in field.path.lower() for s in ("created", "timestamp", "time")):
            return LABEL_TO_ID["response_timestamp"]
    if isinstance(field.value, int) and field.value > 1_000_000_000 and field.value < 5_000_000_000:
        if field.key in ("created", "created_at", "createdAt", "timestamp"):
            return LABEL_TO_ID["response_timestamp"]
    return ABSTAIN


def lf_tool_call_name(field, ctx) -> int:
    if isinstance(field.value, str) and _TOOL_NAME_KEY_RE.search(field.path):
        return LABEL_TO_ID["tool_call_name"]
    return ABSTAIN


def lf_tool_call_arguments(field, ctx) -> int:
    if _TOOL_ARGS_KEY_RE.search(field.path):
        return LABEL_TO_ID["tool_call_arguments"]
    return ABSTAIN


def lf_model_key(field, ctx) -> int:
    if isinstance(field.value, str) and _MODEL_KEY_RE.match(_last_path_seg(field.path)):
        return LABEL_TO_ID["model"]
    return ABSTAIN


def lf_model_hash_key(field, ctx) -> int:
    if isinstance(field.value, str) and _MODEL_HASH_RE.search(field.path):
        return LABEL_TO_ID["model_hash"]
    return ABSTAIN


def lf_timing_value_key(field, ctx) -> int:
    if isinstance(field.value, (int, float)) and not isinstance(field.value, bool):
        if _TIMING_KEY_RE.search(field.path):
            return LABEL_TO_ID["timing_value"]
        if any(s in _last_path_seg(field.path) for s in ("duration", "latency_ms", "predict_time")):
            return LABEL_TO_ID["timing_value"]
    return ABSTAIN


def lf_long_natural_string_is_content(field, ctx) -> int:
    """Long natural-language string with spaces under a content-ish key."""
    if isinstance(field.value, str) and len(field.value) >= 30 and field.value.count(" ") >= 4:
        if any(s in _last_path_seg(field.path) for s in ("text", "content", "response", "generation", "outputText", "delta")):
            return LABEL_TO_ID["content"]
    return ABSTAIN


# ── Registry ─────────────────────────────────────────────────────────────────

LFS: list[Callable] = [
    lf_token_arithmetic,
    lf_prompt_token_key,
    lf_completion_token_key,
    lf_total_token_key,
    lf_cached_token_key,
    lf_cache_creation_token_key,
    lf_finish_reason_key,
    lf_response_id_uuid,
    lf_url_in_citation_path,
    lf_iso_timestamp,
    lf_tool_call_name,
    lf_tool_call_arguments,
    lf_model_key,
    lf_model_hash_key,
    lf_timing_value_key,
    lf_long_natural_string_is_content,
]
NUM_LABELS = len(CANONICAL_LABELS)


def apply_lfs_to_field(field, ctx) -> list[int]:
    """Return one vote per LF (-1 = ABSTAIN, else label id)."""
    return [lf(field, ctx) for lf in LFS]


def _build_field_context(obj: Any, field) -> dict:
    """Compute sibling stats for the LFs that need them."""
    parent_path = field.path.rsplit(".", 1)[0] if "." in field.path else ""
    sib_int_values: list[int] = []
    if parent_path:
        try:
            cur = obj
            for seg in re.split(r"\.|(\[\d+\])", parent_path):
                if not seg:
                    continue
                if seg.startswith("["):
                    cur = cur[int(seg[1:-1])]
                else:
                    cur = cur[seg]
            if isinstance(cur, dict):
                for v in cur.values():
                    if isinstance(v, int) and not isinstance(v, bool):
                        sib_int_values.append(v)
        except (KeyError, IndexError, TypeError):
            pass
    else:
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, int) and not isinstance(v, bool):
                    sib_int_values.append(v)
    return {"sibling_int_values": sib_int_values}


def lf_matrix_for_jsonl(in_path: pathlib.Path) -> tuple[np.ndarray, list[tuple[str, str]]]:
    """Run all LFs over every (variant, field) and build a Snorkel-shaped
    L matrix [N_fields, N_lfs]. Returns (L, idx_keys)."""
    L_rows: list[list[int]] = []
    idx_keys: list[tuple[str, str]] = []
    with in_path.open() as f:
        for line in f:
            row = json.loads(line)
            obj = row["raw"]
            for fld in flatten_json(obj):
                ctx = _build_field_context(obj, fld)
                votes = apply_lfs_to_field(fld, ctx)
                L_rows.append(votes)
                idx_keys.append((row.get("variant_id", "?"), fld.path))
    return np.asarray(L_rows, dtype=np.int64), idx_keys


def fuse_with_snorkel(L: np.ndarray) -> np.ndarray:
    """Return per-row probabilistic label vector [N_fields, NUM_LABELS]
    from Snorkel LabelModel.fit_predict_proba()."""
    from snorkel.labeling import LFAnalysis
    from snorkel.labeling.model import LabelModel

    model = LabelModel(cardinality=NUM_LABELS, verbose=False)
    model.fit(L_train=L, n_epochs=200, log_freq=50, seed=20260427)
    probs = model.predict_proba(L=L)
    return probs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", type=pathlib.Path, required=True,
                    help="JSONL produced by synthesize.py (synthesized_train.jsonl)")
    ap.add_argument("--out", dest="out_path", type=pathlib.Path, required=True)
    args = ap.parse_args()

    L, idx_keys = lf_matrix_for_jsonl(args.in_path)
    print(f"[stage C] L matrix: {L.shape}", file=sys.stderr)
    coverage = (L != ABSTAIN).mean()
    print(f"[stage C] LF coverage: {coverage:.2%}", file=sys.stderr)
    if L.size == 0:
        print("[stage C] empty L matrix — nothing to fuse", file=sys.stderr)
        return
    probs = fuse_with_snorkel(L)
    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    with args.out_path.open("w") as f:
        for (vid, path), row in zip(idx_keys, probs):
            f.write(json.dumps({
                "variant_id": vid, "path": path,
                "soft_label": row.tolist(),
            }) + "\n")
    print(f"[stage C] wrote {len(idx_keys)} fused soft labels to {args.out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
