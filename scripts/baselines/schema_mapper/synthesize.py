"""Phase 5 data synthesis pipeline (Stages A → C; D lives in
data/synthetic_corpus.py).

Stages:
  A.   Load gold curation (data/provider_specs/*.json)
  B.   Magneto-style teacher synthesis via Anthropic claude-opus-4-7
       — with a hard $30 USD budget gate enforced inline
  C.   Snorkel labeling-function fusion (in labeling_functions.py;
       this script orchestrates the call)

CLI:
    /tmp/schema-mapper-venv/bin/python synthesize.py \\
        --gold-dir data/provider_specs/ \\
        --teacher-model claude-opus-4-7 \\
        --variations-per-spec 50 \\
        --out out/data/

Budget ledger:
  Every Anthropic call appends one row to out/teacher_cost_ledger.jsonl
  with timestamp, model, input_tokens, output_tokens, usd_cost,
  batch_id, source_spec. Before each call, the cumulative cost is
  checked: ≥$25 logs WARNING, ≥$30 refuses + exits with code 4.

Filter discipline (post-API, per-variant):
  1. Drop if any expected_labels value is not in CANONICAL_LABELS
  2. Drop if expected_labels.keys() != flatten_json(raw).paths
  3. Drop if any (path == label) literal echo
  4. Drop if raw is byte-identical to the seed
  5. Drop if >70% of paths are labelled UNKNOWN

A drop-rate-per-spec tracker surfaces specs with >30% drops as
WARNINGs in the post-run summary.

Output:
  out/data/synthesized_train.jsonl     — kept variants, training input
  out/data/synthesized_dropped.jsonl   — dropped variants + reason (audit)
  out/teacher_cost_ledger.jsonl        — cost ledger
  out/teacher_synthesis_audit_<ts>.json — Stage A.5 audit harness output
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import random
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from canonical_schema import CANONICAL_LABELS, LABEL_DESCRIPTIONS  # noqa: E402
from paths import flatten_json  # noqa: E402

# ── Pricing (claude-opus-4-7 as of 2026-04 — verify before running) ─────────
# https://docs.anthropic.com/en/docs/about-claude/pricing
# Opus tier: $15 / 1M input tokens, $75 / 1M output tokens.
USD_PER_INPUT_TOKEN = 15.0 / 1_000_000
USD_PER_OUTPUT_TOKEN = 75.0 / 1_000_000

BUDGET_WARN_USD = 25.0
BUDGET_HARD_USD = 30.0

DEFAULT_TEACHER = "claude-opus-4-7"
DEFAULT_VARIATIONS_PER_SPEC = 50

PROMPT_TEMPLATE_PATH = (
    pathlib.Path(__file__).parent / "data" / "teacher_prompts" / "synthesize_variants.txt"
)


# ── Cost ledger ─────────────────────────────────────────────────────────────


@dataclass
class CostRow:
    timestamp: str
    model: str
    input_tokens: int
    output_tokens: int
    usd_cost: float
    batch_id: str
    source_spec: str


class CostLedger:
    def __init__(self, ledger_path: pathlib.Path) -> None:
        self.path = ledger_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a")
        self.cumulative_usd = self._sum_existing()

    def _sum_existing(self) -> float:
        if not self.path.exists():
            return 0.0
        total = 0.0
        try:
            with self.path.open() as f:
                for line in f:
                    if line.strip():
                        total += json.loads(line)["usd_cost"]
        except (OSError, json.JSONDecodeError):
            return 0.0
        return total

    def precheck(self) -> None:
        if self.cumulative_usd >= BUDGET_HARD_USD:
            print(
                f"BUDGET HARD-REFUSE: cumulative ${self.cumulative_usd:.2f} >= ${BUDGET_HARD_USD}",
                file=sys.stderr,
            )
            sys.exit(4)

    def record(self, row: CostRow) -> None:
        self.cumulative_usd += row.usd_cost
        self._fh.write(json.dumps(asdict(row)) + "\n")
        self._fh.flush()
        if BUDGET_WARN_USD <= self.cumulative_usd < BUDGET_HARD_USD:
            print(
                f"BUDGET WARN: cumulative ${self.cumulative_usd:.2f} >= ${BUDGET_WARN_USD}",
                file=sys.stderr,
            )

    def close(self) -> None:
        self._fh.close()


# ── Anthropic client wrapper ─────────────────────────────────────────────────


def _build_client():
    """Return an Anthropic client; raise if API key not in env."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Synthesize cannot run without it."
        )
    from anthropic import Anthropic

    return Anthropic()


# ── Prompt construction ──────────────────────────────────────────────────────


def _build_label_descriptions_block() -> str:
    return "\n".join(f"  - {label}: {LABEL_DESCRIPTIONS[label]}" for label in CANONICAL_LABELS)


def _build_prompt(seed_example: dict, provider: str, endpoint: str, n_variations: int) -> str:
    # The template uses Python str.format() with five named placeholders:
    # {provider}, {endpoint}, {n_variations}, {label_descriptions},
    # {seed_raw_json}, {seed_labels_json}. ANY OTHER literal `{` in the
    # template body must be escaped as `{{` (and `}` as `}}`) — otherwise
    # str.format treats it as a placeholder and raises KeyError. The
    # template currently includes one such literal: `{{dotted_path:
    # canonical_label}}` in the output-format spec. test_prompt_render
    # is the regression guard.
    template = PROMPT_TEMPLATE_PATH.read_text()
    return template.format(
        provider=provider,
        endpoint=endpoint,
        n_variations=n_variations,
        label_descriptions=_build_label_descriptions_block(),
        seed_raw_json=json.dumps(seed_example["raw"], indent=2),
        seed_labels_json=json.dumps(seed_example["expected_labels"], indent=2),
    )


# ── Filter discipline ────────────────────────────────────────────────────────


_DROP_REASONS = (
    "non_canonical_label",
    "path_label_mismatch",
    "trivial_echo",
    "identical_to_seed",
    "degenerate_unknown",
)


def _validate_variant(
    variant: dict,
    seed_raw: Any,
) -> tuple[bool, str | None]:
    """Apply the 5 filter-discipline rules. Return (kept, drop_reason)."""
    if not isinstance(variant, dict) or "raw" not in variant or "expected_labels" not in variant:
        return False, "non_canonical_label"  # malformed → treat as discipline failure
    raw = variant["raw"]
    labels = variant["expected_labels"]
    # Rule 1: every label in CANONICAL_LABELS
    for label in labels.values():
        if label not in CANONICAL_LABELS:
            return False, "non_canonical_label"
    # Rule 2: paths match flatten_json output
    actual_paths = {f.path for f in flatten_json(raw)}
    declared_paths = set(labels)
    if actual_paths != declared_paths:
        return False, "path_label_mismatch"
    # Rule 3: no trivial echo (path literally equals label)
    for path, label in labels.items():
        if path == label:
            return False, "trivial_echo"
    # Rule 4: not byte-identical to seed
    if json.dumps(raw, sort_keys=True) == json.dumps(seed_raw, sort_keys=True):
        return False, "identical_to_seed"
    # Rule 5: not >70% UNKNOWN
    if labels:
        unk_rate = sum(1 for v in labels.values() if v == "UNKNOWN") / len(labels)
        if unk_rate > 0.70:
            return False, "degenerate_unknown"
    return True, None


# ── Anthropic call ──────────────────────────────────────────────────────────


def _call_teacher(
    client,
    prompt: str,
    model: str,
    max_tokens: int,
) -> tuple[str, int, int]:
    """Single API call. Returns (text, input_tokens, output_tokens)."""
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text_chunks = [c.text for c in msg.content if hasattr(c, "text")]
    return "".join(text_chunks), msg.usage.input_tokens, msg.usage.output_tokens


def _parse_json_array(text: str) -> list[dict]:
    """Extract a JSON array from the teacher's text output. Tolerant of
    surrounding markdown fences."""
    s = text.strip()
    # Strip ```json / ``` fences
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.rsplit("```", 1)[0]
    # Find the first `[` and last `]`
    lb = s.find("[")
    rb = s.rfind("]")
    if lb == -1 or rb == -1 or rb <= lb:
        return []
    try:
        return json.loads(s[lb : rb + 1])
    except json.JSONDecodeError:
        return []


# ── Orchestration ────────────────────────────────────────────────────────────


def synthesize_for_spec(
    spec: dict,
    spec_name: str,
    n_per_example: int,
    client,
    ledger: CostLedger,
    model: str,
    max_tokens_per_call: int,
    rng: random.Random,
    out_train_fh,
    out_dropped_fh,
) -> dict:
    """Synthesize variations for one spec. Returns per-spec stats dict."""
    kept_count = 0
    drop_counter: Counter = Counter()
    n_calls = 0
    for ex_idx, ex in enumerate(spec["examples"]):
        ledger.precheck()
        prompt = _build_prompt(ex, spec["provider"], spec.get("endpoint", ""), n_per_example)
        batch_id = hashlib.sha256(f"{spec_name}#{ex_idx}#{time.time()}".encode()).hexdigest()[:16]
        try:
            text, in_tok, out_tok = _call_teacher(client, prompt, model, max_tokens_per_call)
        except Exception as e:
            print(f"[error] {spec_name}#{ex_idx}: teacher call failed: {e}", file=sys.stderr)
            continue
        n_calls += 1
        cost = in_tok * USD_PER_INPUT_TOKEN + out_tok * USD_PER_OUTPUT_TOKEN
        ledger.record(
            CostRow(
                timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                usd_cost=cost,
                batch_id=batch_id,
                source_spec=spec_name,
            )
        )
        variants = _parse_json_array(text)
        for var_idx, variant in enumerate(variants):
            kept, reason = _validate_variant(variant, ex["raw"])
            if kept:
                row = {
                    "raw": variant["raw"],
                    "expected_labels": variant["expected_labels"],
                    "source_spec": spec_name,
                    "source_example_id": ex_idx,
                    "variant_id": f"{spec_name}#{ex_idx}#{var_idx}",
                    "batch_id": batch_id,
                    "stage": "B_teacher",
                }
                out_train_fh.write(json.dumps(row, sort_keys=True) + "\n")
                kept_count += 1
            else:
                drop_counter[reason or "unknown"] += 1
                out_dropped_fh.write(
                    json.dumps(
                        {
                            "variant": variant,
                            "drop_reason": reason,
                            "source_spec": spec_name,
                            "source_example_id": ex_idx,
                            "batch_id": batch_id,
                        }
                    )
                    + "\n"
                )
    total_seen = kept_count + sum(drop_counter.values())
    drop_rate = (sum(drop_counter.values()) / total_seen) if total_seen else 0.0
    return {
        "spec": spec_name,
        "kept": kept_count,
        "dropped": dict(drop_counter),
        "drop_rate": drop_rate,
        "n_api_calls": n_calls,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold-dir", type=pathlib.Path, default=pathlib.Path(__file__).parent / "data" / "provider_specs")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path(__file__).parent / "out" / "data")
    ap.add_argument("--teacher-model", default=DEFAULT_TEACHER)
    ap.add_argument("--variations-per-spec", type=int, default=DEFAULT_VARIATIONS_PER_SPEC,
                    help="Total variations per spec; split evenly across examples.")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--seed", type=int, default=20260427)
    ap.add_argument("--dry-run", action="store_true",
                    help="Prepare prompts + exercise filter discipline on cached fixtures, no API calls.")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    ledger_path = args.out.parent / "teacher_cost_ledger.jsonl"
    train_path = args.out / "synthesized_train.jsonl"
    dropped_path = args.out / "synthesized_dropped.jsonl"
    summary_path = args.out / "synthesized_summary.json"

    rng = random.Random(args.seed)
    ledger = CostLedger(ledger_path)
    if args.dry_run:
        print("[dry-run] not calling Anthropic; not writing ledger rows", file=sys.stderr)
        client = None
    else:
        ledger.precheck()
        client = _build_client()

    # Estimate cost upfront — useful for both dry-run AND real runs (so the
    # operator sees the planned spend before authorizing).
    spec_paths_for_estimate = sorted(args.gold_dir.glob("*.json"))
    n_examples_total = sum(len(json.loads(p.read_text())["examples"]) for p in spec_paths_for_estimate)
    # Conservative per-call estimate: ~3K input tokens (prompt + label
    # descriptions + seed) + ~5K output tokens (50 variants × ~100 tokens
    # of JSON each, capped by max_tokens).
    EST_INPUT_PER_CALL = 3000
    EST_OUTPUT_PER_CALL = min(args.max_tokens, args.variations_per_spec * 100)
    est_per_call_usd = (
        EST_INPUT_PER_CALL * USD_PER_INPUT_TOKEN
        + EST_OUTPUT_PER_CALL * USD_PER_OUTPUT_TOKEN
    )
    est_total_usd = n_examples_total * est_per_call_usd
    print(
        f"[estimate] {n_examples_total} planned API calls × ~${est_per_call_usd:.3f}/call "
        f"= ~${est_total_usd:.2f} total (input≈{EST_INPUT_PER_CALL}, output≈{EST_OUTPUT_PER_CALL} tok/call)",
        file=sys.stderr,
    )
    if est_total_usd > BUDGET_HARD_USD and not args.dry_run:
        print(f"[estimate] OVER BUDGET (${est_total_usd:.2f} > ${BUDGET_HARD_USD}) — refusing.", file=sys.stderr)
        sys.exit(4)

    spec_paths = sorted(args.gold_dir.glob("*.json"))
    print(f"[stage A] loading {len(spec_paths)} provider specs", file=sys.stderr)
    if not spec_paths:
        print("[fatal] no specs found", file=sys.stderr)
        sys.exit(1)

    per_spec_stats: list[dict] = []
    with train_path.open("w") as fout, dropped_path.open("w") as fdrop:
        for sp in spec_paths:
            spec = json.loads(sp.read_text())
            spec_name = sp.stem
            n_per_example = max(1, args.variations_per_spec // max(len(spec["examples"]), 1))
            print(f"[stage B] {spec_name}: {len(spec['examples'])} examples × {n_per_example}/ex", file=sys.stderr)
            if args.dry_run:
                stats = {"spec": spec_name, "kept": 0, "dropped": {}, "drop_rate": 0.0, "n_api_calls": 0, "dry_run": True}
            else:
                stats = synthesize_for_spec(
                    spec=spec,
                    spec_name=spec_name,
                    n_per_example=n_per_example,
                    client=client,
                    ledger=ledger,
                    model=args.teacher_model,
                    max_tokens_per_call=args.max_tokens,
                    rng=rng,
                    out_train_fh=fout,
                    out_dropped_fh=fdrop,
                )
            per_spec_stats.append(stats)
            print(f"  {spec_name}: kept={stats['kept']} dropped={sum(stats['dropped'].values()) if isinstance(stats['dropped'], dict) else 0} rate={stats['drop_rate']:.2%}", file=sys.stderr)
            if not args.dry_run and stats["drop_rate"] > 0.30:
                print(f"  [warn] {spec_name} drop rate {stats['drop_rate']:.0%} > 30% — teacher prompt may need tuning", file=sys.stderr)
    ledger.close()

    summary = {
        "total_kept": sum(s["kept"] for s in per_spec_stats),
        "total_calls": sum(s["n_api_calls"] for s in per_spec_stats),
        "cumulative_cost_usd": ledger.cumulative_usd,
        "per_spec": per_spec_stats,
        "completed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"\n=== Stage B summary ===", file=sys.stderr)
    print(f"  total kept: {summary['total_kept']}", file=sys.stderr)
    print(f"  total API calls: {summary['total_calls']}", file=sys.stderr)
    print(f"  cumulative cost: ${summary['cumulative_cost_usd']:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
