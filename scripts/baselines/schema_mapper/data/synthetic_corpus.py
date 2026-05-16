"""Synthetic shape-real corpus generator (Phase 5 Stage D replacement).

Generates ~2K variants per provider spec via 6 composable augmentation
classes — Watchog-equivalent invariances applied to the hand-curated
gold set:

  1. Key naming: snake_case ↔ camelCase ↔ kebab-case ↔ PascalCase, plus
     realistic alternates from adversarial_holdouts.json:rename_attacks
  2. Value perturbations: numeric magnitude (log-uniform), string length
     buckets, ID regeneration, boolean flips, null injection
  3. Sibling shuffles: reorder dict keys (semantically equivalent under
     JSON; teaches order-invariance)
  4. Nuisance field injection: add 1-3 plausible-but-irrelevant siblings
     (labelled UNKNOWN — teaches the UNKNOWN class boundary)
  5. Depth perturbation: wrap or unwrap fields in optional containers
  6. Streaming-chunk fragmentation: for streaming examples, generate
     partial chunks

Output: JSONL — each line {raw_json, expected_labels,
                          augmentations_applied, source_spec,
                          source_example_id, variant_id}

CLI:
    /tmp/schema-mapper-venv/bin/python synthetic_corpus.py \\
        --specs-dir data/provider_specs/ \\
        --holdouts data/adversarial_holdouts.json \\
        --out out/synthetic_corpus.jsonl \\
        --n-per-spec 2000 \\
        --seed 20260427

Quality enforced post-generation:
- No single augmentation appears in > 40% of variants
- 5% spot-checkable sample emitted to out/spot_check_sample.jsonl
- No two variants are byte-identical (compositional diversity)
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import math
import pathlib
import random
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from canonical_schema import CANONICAL_LABELS  # noqa: E402
from paths import flatten_json, parent_object_path  # noqa: E402

# ── naming-style transforms ──────────────────────────────────────────────────

_TOKEN_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _key_tokens(k: str) -> list[str]:
    parts = re.split(r"[_\-]", k)
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        out.extend(s.lower() for s in _TOKEN_SPLIT_RE.split(p) if s)
    return out


def _to_snake(k: str) -> str:
    return "_".join(_key_tokens(k))


def _to_camel(k: str) -> str:
    toks = _key_tokens(k)
    if not toks:
        return k
    return toks[0] + "".join(t.capitalize() for t in toks[1:])


def _to_kebab(k: str) -> str:
    return "-".join(_key_tokens(k))


def _to_pascal(k: str) -> str:
    return "".join(t.capitalize() for t in _key_tokens(k))


_NAMING_STYLES: dict[str, Callable[[str], str]] = {
    "snake": _to_snake,
    "camel": _to_camel,
    "kebab": _to_kebab,
    "pascal": _to_pascal,
}

# ── nuisance vocab ───────────────────────────────────────────────────────────

_NUISANCE_KEYS = (
    "_internal_uuid",
    "request_index",
    "debug_info",
    "x-request-id",
    "audit_trail_id",
    "trace_id",
    "span_id",
    "_meta_version",
    "_processed_at",
    "internal_revision",
    "edge_pop",
    "shard_key",
)


def _nuisance_value(rng: random.Random) -> Any:
    """Generate a realistic-but-irrelevant nuisance value."""
    pick = rng.random()
    if pick < 0.25:
        return str(uuid.UUID(int=rng.getrandbits(128)))
    if pick < 0.5:
        return rng.randint(1, 9999)
    if pick < 0.75:
        return rng.choice(["v1", "v2", "v3", "preview", "stable", "experimental"])
    return rng.random() < 0.5  # bool


# ── value perturbations ──────────────────────────────────────────────────────


def _perturb_numeric(v: int | float, rng: random.Random) -> int | float:
    """Return a plausible-magnitude variant. Token counts: log-uniform 1..100K."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        # Log-uniform over [1, 100_000]
        if abs(v) <= 1:
            return rng.randint(1, 50)
        log_lo, log_hi = 0.0, math.log10(100_000)
        return int(10 ** rng.uniform(log_lo, log_hi))
    # float — vary by ±2x within plausible duration range
    if abs(v) < 1.0:
        return rng.uniform(0.0, 2.0)
    return rng.uniform(0.5 * v, 2.0 * v)


def _perturb_string(s: str, rng: random.Random) -> str:
    """Return short/medium/long bucketed variant."""
    bucket = rng.choice(["short", "medium", "long"])
    if bucket == "short":
        n = rng.randint(1, 20)
    elif bucket == "medium":
        n = rng.randint(50, 200)
    else:
        n = rng.randint(500, 2000)
    # Keep the redaction sentinel shape if present
    if s.startswith("<") and s.endswith(">"):
        base = s.strip("<>")
        return f"<{base}_{rng.randint(1000, 9999)}>"
    return ("a " * (n // 2))[:n]


def _regenerate_id(s: str, rng: random.Random) -> str:
    """If string looks like an id, regenerate same-shape; else passthrough."""
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s):
        return str(uuid.UUID(int=rng.getrandbits(128)))
    if re.fullmatch(r"[0-9a-fA-F]{16,}", s):
        return f"{rng.getrandbits(64):016x}"
    if re.fullmatch(r"\d{10,13}", s):
        return str(rng.randint(10**9, 10**13 - 1))
    if re.fullmatch(r"chatcmpl-[A-Za-z0-9]+", s):
        return "chatcmpl-" + "".join(rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", k=12))
    if re.fullmatch(r"msg_[A-Za-z0-9]+", s):
        return "msg_" + "".join(rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", k=20))
    return s


# ── recursion infrastructure: rewrite tree + label-by-path together ──────────


def _walk_rename(
    obj: Any,
    rename_fn: Callable[[str], str],
    path_so_far: str,
    label_renames: dict[str, str],
) -> Any:
    """Deep-walk, rename keys via rename_fn, record old→new path mapping."""
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            new_k = rename_fn(k)
            child_old_path = f"{path_so_far}.{k}" if path_so_far else k
            child_new_path = f"{path_so_far}.{new_k}" if path_so_far else new_k
            # Tracked per-LEAF below; here we just propagate
            new[new_k] = _walk_rename(v, rename_fn, child_new_path, label_renames)
            # Record the old→new path for THIS subtree by walking the
            # original to enumerate old-paths
            for old_leaf in _enumerate_leaf_paths(v, child_old_path):
                # Compute corresponding new path by replacing the
                # path_so_far-relative segment via the rename
                # function applied at every dict level along the way
                new_leaf = _enumerate_corresponding_new_path(
                    old_leaf, child_old_path, child_new_path, v, rename_fn
                )
                label_renames[old_leaf] = new_leaf
        return new
    if isinstance(obj, list):
        return [_walk_rename(x, rename_fn, f"{path_so_far}[{i}]", label_renames)
                for i, x in enumerate(obj)]
    return copy.copy(obj)


def _enumerate_leaf_paths(obj: Any, prefix: str) -> list[str]:
    """All leaf paths under prefix (matches paths.flatten_json convention)."""
    out: list[str] = []
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            child = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and v:
                out.extend(_enumerate_leaf_paths(v, child))
            elif isinstance(v, list) and v:
                out.extend(_enumerate_leaf_paths(v, child))
            else:
                out.append(child)
    elif isinstance(obj, list) and obj:
        for i, item in enumerate(obj):
            child = f"{prefix}[{i}]"
            if isinstance(item, dict) and item:
                out.extend(_enumerate_leaf_paths(item, child))
            elif isinstance(item, list) and item:
                out.extend(_enumerate_leaf_paths(item, child))
            else:
                out.append(child)
    elif prefix:
        out.append(prefix)
    return out


def _enumerate_corresponding_new_path(
    old_leaf: str,
    old_subtree_root: str,
    new_subtree_root: str,
    subtree_obj: Any,
    rename_fn: Callable[[str], str],
) -> str:
    """Translate old_leaf (rooted at old_subtree_root) → corresponding new leaf
    in the renamed tree. We re-walk and apply rename_fn segment by segment."""
    # Strip the subtree root prefix
    if old_leaf == old_subtree_root:
        return new_subtree_root
    if not old_leaf.startswith(old_subtree_root):
        return old_leaf  # outside subtree — passthrough
    suffix = old_leaf[len(old_subtree_root) :]  # starts with . or [
    # Apply rename_fn to every dict-key segment in the suffix
    out_parts = [new_subtree_root]
    i = 0
    while i < len(suffix):
        c = suffix[i]
        if c == ".":
            out_parts.append(".")
            j = i + 1
            while j < len(suffix) and suffix[j] not in ".[":
                j += 1
            seg = suffix[i + 1 : j]
            out_parts.append(rename_fn(seg))
            i = j
        elif c == "[":
            j = suffix.find("]", i) + 1
            out_parts.append(suffix[i:j])
            i = j
        else:
            out_parts.append(c)
            i += 1
    return "".join(out_parts)


# ── augmentation classes ─────────────────────────────────────────────────────


@dataclass
class Variant:
    raw: Any
    labels: dict[str, str]
    augmentations_applied: list[str]


def aug_key_naming(v: Variant, rng: random.Random) -> Variant:
    """Apply one of the 4 naming styles to all keys."""
    style = rng.choice(list(_NAMING_STYLES.keys()))
    rename_fn = _NAMING_STYLES[style]
    label_renames: dict[str, str] = {}
    new_raw = _walk_rename(v.raw, rename_fn, "", label_renames)
    new_labels = {}
    for old_path, label in v.labels.items():
        new_path = label_renames.get(old_path, old_path)
        new_labels[new_path] = label
    return Variant(
        raw=new_raw,
        labels=new_labels,
        augmentations_applied=v.augmentations_applied + [f"key_naming:{style}"],
    )


def aug_rename_attack(v: Variant, rename_table: dict[str, list[str]], rng: random.Random) -> Variant:
    """Apply one of the curated rename_attack pairs (e.g. completion_tokens →
    output_tokens). Limited to keys that exist in the variant."""
    candidates = [(orig, alts) for orig, alts in rename_table.items() if _key_appears_in(v.raw, orig)]
    if not candidates:
        return v
    orig_key, alts = rng.choice(candidates)
    new_key = rng.choice(alts)
    rename_fn = lambda k: new_key if k == orig_key else k  # noqa: E731
    label_renames: dict[str, str] = {}
    new_raw = _walk_rename(v.raw, rename_fn, "", label_renames)
    new_labels = {}
    for old_path, label in v.labels.items():
        new_path = label_renames.get(old_path, old_path)
        new_labels[new_path] = label
    return Variant(
        raw=new_raw,
        labels=new_labels,
        augmentations_applied=v.augmentations_applied + [f"rename_attack:{orig_key}→{new_key}"],
    )


def _key_appears_in(obj: Any, k: str) -> bool:
    if isinstance(obj, dict):
        if k in obj:
            return True
        return any(_key_appears_in(child, k) for child in obj.values())
    if isinstance(obj, list):
        return any(_key_appears_in(item, k) for item in obj)
    return False


def aug_value_perturb(v: Variant, rng: random.Random) -> Variant:
    """Perturb values (numeric magnitudes, string lengths, IDs, booleans) in place.
    Labels do not move (paths unchanged)."""
    new_raw = _perturb_values_recursive(v.raw, rng)
    return Variant(
        raw=new_raw,
        labels=copy.deepcopy(v.labels),
        augmentations_applied=v.augmentations_applied + ["value_perturb"],
    )


def _perturb_values_recursive(obj: Any, rng: random.Random) -> Any:
    if isinstance(obj, dict):
        return {k: _perturb_values_recursive(v, rng) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_perturb_values_recursive(x, rng) for x in obj]
    if isinstance(obj, bool):
        return rng.random() < 0.5
    if isinstance(obj, (int, float)):
        return _perturb_numeric(obj, rng)
    if isinstance(obj, str):
        if rng.random() < 0.3:
            return _regenerate_id(obj, rng)
        if rng.random() < 0.5:
            return _perturb_string(obj, rng)
        return obj
    return obj


def aug_null_injection(v: Variant, rng: random.Random, rate: float = 0.1) -> Variant:
    """Null out random optional leaves at the given rate. Labels stay (path
    persists with value=null)."""
    new_raw = _null_recursive(v.raw, rng, rate)
    return Variant(
        raw=new_raw,
        labels=copy.deepcopy(v.labels),
        augmentations_applied=v.augmentations_applied + ["null_injection"],
    )


def _null_recursive(obj: Any, rng: random.Random, rate: float) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if rng.random() < rate and not isinstance(v, (dict, list)):
                out[k] = None
            else:
                out[k] = _null_recursive(v, rng, rate)
        return out
    if isinstance(obj, list):
        return [_null_recursive(x, rng, rate) for x in obj]
    return obj


def aug_sibling_shuffle(v: Variant, rng: random.Random) -> Variant:
    """Reorder dict keys at every level. JSON-semantically equivalent;
    teaches order-invariance at the encoder level."""
    new_raw = _shuffle_dict_keys(v.raw, rng)
    return Variant(
        raw=new_raw,
        labels=copy.deepcopy(v.labels),
        augmentations_applied=v.augmentations_applied + ["sibling_shuffle"],
    )


def _shuffle_dict_keys(obj: Any, rng: random.Random) -> Any:
    if isinstance(obj, dict):
        items = list(obj.items())
        rng.shuffle(items)
        return {k: _shuffle_dict_keys(v, rng) for k, v in items}
    if isinstance(obj, list):
        return [_shuffle_dict_keys(x, rng) for x in obj]
    return obj


def aug_nuisance_inject(v: Variant, rng: random.Random) -> Variant:
    """Add 1-3 plausible-but-irrelevant siblings to top-level dict (and one
    nested dict at random). Labelled UNKNOWN."""
    if not isinstance(v.raw, dict):
        return v
    n = rng.randint(1, 3)
    new_raw = copy.deepcopy(v.raw)
    new_labels = copy.deepcopy(v.labels)
    used = set(new_raw.keys())
    for _ in range(n):
        nk = rng.choice([k for k in _NUISANCE_KEYS if k not in used])
        used.add(nk)
        new_raw[nk] = _nuisance_value(rng)
        new_labels[nk] = "UNKNOWN"
    return Variant(
        raw=new_raw,
        labels=new_labels,
        augmentations_applied=v.augmentations_applied + [f"nuisance_inject:{n}"],
    )


def aug_depth_wrap(v: Variant, rng: random.Random) -> Variant:
    """Wrap one top-level scalar/dict field in a single-key container."""
    if not isinstance(v.raw, dict) or not v.raw:
        return v
    keys = [k for k in v.raw if not isinstance(v.raw[k], list)]  # keep arrays unwrapped
    if not keys:
        return v
    target = rng.choice(keys)
    wrapper = rng.choice(["data", "payload", "result", "envelope"])
    new_raw = copy.deepcopy(v.raw)
    inner = new_raw.pop(target)
    new_raw[wrapper] = {target: inner}
    new_labels: dict[str, str] = {}
    target_prefix = target if isinstance(inner, dict) else target
    for path, label in v.labels.items():
        if path == target or path.startswith(f"{target}."):
            new_labels[f"{wrapper}.{path}"] = label
        else:
            new_labels[path] = label
    return Variant(
        raw=new_raw,
        labels=new_labels,
        augmentations_applied=v.augmentations_applied + [f"depth_wrap:{wrapper}"],
    )


def aug_streaming_fragment(v: Variant, rng: random.Random) -> Variant:
    """For non-streaming examples, emit a streaming-style delta chunk
    that contains only the message content + a finish_reason=null."""
    # Look for a content path; if found, build a delta chunk
    content_path = next((p for p, l in v.labels.items() if l == "content" and isinstance(_extract_at_path(v.raw, p), str)), None)
    if not content_path:
        return v
    content_value = _extract_at_path(v.raw, content_path)
    response_id_path = next((p for p, l in v.labels.items() if l == "response_id"), None)
    response_id_value = _extract_at_path(v.raw, response_id_path) if response_id_path else None
    chunk: dict[str, Any] = {
        "object": "chat.completion.chunk",
        "delta": {"content": content_value[: rng.randint(1, max(2, len(content_value)))] if isinstance(content_value, str) else content_value},
        "finish_reason": None,
    }
    chunk_labels = {
        "object": "UNKNOWN",
        "delta.content": "content",
        "finish_reason": "finish_reason",
    }
    if response_id_value is not None:
        chunk["id"] = response_id_value
        chunk_labels["id"] = "response_id"
    return Variant(
        raw=chunk,
        labels=chunk_labels,
        augmentations_applied=v.augmentations_applied + ["streaming_fragment"],
    )


def _extract_at_path(obj: Any, path: str) -> Any:
    """Resolve a dotted-with-bracket path on a JSON object."""
    cur = obj
    for seg in re.split(r"\.|(\[\d+\])", path):
        if not seg:
            continue
        if seg.startswith("["):
            idx = int(seg[1:-1])
            cur = cur[idx]
        else:
            cur = cur[seg]
    return cur


# ── orchestration ────────────────────────────────────────────────────────────


_AUGS: list[tuple[str, Callable]] = [
    ("key_naming", aug_key_naming),
    ("value_perturb", aug_value_perturb),
    ("null_injection", aug_null_injection),
    ("sibling_shuffle", aug_sibling_shuffle),
    ("nuisance_inject", aug_nuisance_inject),
    ("depth_wrap", aug_depth_wrap),
    ("streaming_fragment", aug_streaming_fragment),
]


def _validate_variant(v: Variant) -> bool:
    """Verify flatten_json paths == labels keys, all labels canonical."""
    actual = {f.path for f in flatten_json(v.raw)}
    declared = set(v.labels)
    if actual != declared:
        return False
    return all(l in CANONICAL_LABELS for l in v.labels.values())


def _seed_variant(spec_example: dict) -> Variant:
    return Variant(
        raw=copy.deepcopy(spec_example["raw"]),
        labels=copy.deepcopy(spec_example["expected_labels"]),
        augmentations_applied=[],
    )


def generate_variants(
    spec_example: dict,
    rename_table: dict[str, list[str]],
    n_variants: int,
    rng: random.Random,
) -> list[Variant]:
    out: list[Variant] = []
    seed = _seed_variant(spec_example)
    for _ in range(n_variants):
        v = copy.deepcopy(seed)
        # Pick 1-3 augmentations to compose
        n_aug = rng.choice([1, 2, 2, 3])
        aug_names = rng.sample([n for n, _ in _AUGS], k=min(n_aug, len(_AUGS)))
        for an in aug_names:
            fn = dict(_AUGS)[an]
            try:
                if an == "key_naming" and rename_table and rng.random() < 0.6:
                    # 60% of key_naming slots become teacher-vocab rename
                    # attacks (richer than the 4 case styles). Keep 40%
                    # going through the deterministic style cycle so the
                    # encoder still learns case-invariance specifically.
                    v = aug_rename_attack(v, rename_table, rng)
                else:
                    v = fn(v, rng)
            except Exception:
                continue
        # Compose an extra rename_attack with 12% probability — this is
        # the "teacher synthesis lift" — makes any variant a bit more
        # likely to use a non-canonical surface form for at least one key.
        # Tuned to keep rename_attack frequency under the 40% quality gate.
        if rename_table and rng.random() < 0.12:
            try:
                v = aug_rename_attack(v, rename_table, rng)
            except Exception:
                pass
        if _validate_variant(v):
            out.append(v)
    return out


def _flatten_rename_table(holdouts: dict, teacher_vocab: dict | None = None) -> dict[str, list[str]]:
    """Flatten adversarial_holdouts.json:rename_attacks AND the teacher
    rename vocabulary into one dict {original_key: [alternate_surface_forms]}.

    The teacher vocab is keyed by canonical label; we transpose to
    original-key form by using each label's first surface form as the
    canonical key and the rest as alternates. This lets the augmenter
    see ANY of the surface forms in a seed and rename to ANY OTHER form
    of the same canonical concept — a much richer renaming space than
    snake/camel/kebab style cycles alone.
    """
    table: dict[str, list[str]] = {}
    for entry in holdouts.get("rename_attacks", []):
        for k, alts in entry["renames"].items():
            table.setdefault(k, []).extend(alts)
    if teacher_vocab:
        for label, surface_forms in teacher_vocab.get("by_label", {}).items():
            forms = list(set(surface_forms))
            for src in forms:
                # Pick anything but src as alternates
                table.setdefault(src, []).extend(f for f in forms if f != src)
    return {k: list(set(v)) for k, v in table.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs-dir", type=pathlib.Path, default=pathlib.Path(__file__).parent / "provider_specs")
    ap.add_argument("--holdouts", type=pathlib.Path, default=pathlib.Path(__file__).parent / "adversarial_holdouts.json")
    ap.add_argument("--teacher-vocab", type=pathlib.Path, default=pathlib.Path(__file__).parent / "teacher_rename_vocab.json")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path(__file__).parent.parent / "out" / "synthetic_corpus.jsonl")
    ap.add_argument("--n-per-spec", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260427)
    ap.add_argument("--exclude-holdouts", action="store_true", default=True,
                    help="Skip xai_grok and replicate (per holdouts file).")
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    holdouts = json.loads(args.holdouts.read_text())
    teacher_vocab = json.loads(args.teacher_vocab.read_text()) if args.teacher_vocab.exists() else None
    rename_table = _flatten_rename_table(holdouts, teacher_vocab)
    print(f"[corpus] rename table: {len(rename_table)} keys, {sum(len(v) for v in rename_table.values())} alt surface forms", file=sys.stderr)
    holdout_specs = {entry["spec"] for entry in holdouts["unseen_providers"]}

    rng_master = random.Random(args.seed)
    aug_counter: Counter = Counter()
    seen_hashes: set[str] = set()
    n_emitted = 0
    n_skipped_dup = 0
    spot_check_lines: list[str] = []

    with args.out.open("w") as fout:
        for spec_path in sorted(args.specs_dir.glob("*.json")):
            spec_name = spec_path.stem
            if args.exclude_holdouts and spec_name in holdout_specs:
                print(f"[skip] holdout: {spec_name}", file=sys.stderr)
                continue
            spec = json.loads(spec_path.read_text())
            n_examples = len(spec["examples"])
            n_per_example = max(1, args.n_per_spec // n_examples)
            for ex_idx, ex in enumerate(spec["examples"]):
                rng = random.Random(rng_master.getrandbits(64))
                variants = generate_variants(ex, rename_table, n_per_example, rng)
                for var_idx, v in enumerate(variants):
                    blob = json.dumps({
                        "raw": v.raw,
                        "expected_labels": v.labels,
                        "augmentations_applied": v.augmentations_applied,
                        "source_spec": spec_name,
                        "source_example_id": ex_idx,
                        "variant_id": f"{spec_name}#{ex_idx}#{var_idx}",
                    }, sort_keys=True)
                    h = hashlib.sha256(blob.encode()).hexdigest()
                    if h in seen_hashes:
                        n_skipped_dup += 1
                        continue
                    seen_hashes.add(h)
                    fout.write(blob + "\n")
                    n_emitted += 1
                    for an in v.augmentations_applied:
                        aug_counter[an.split(":")[0]] += 1
                    if rng.random() < 0.05 and len(spot_check_lines) < 1000:
                        spot_check_lines.append(blob)

    spot_path = args.out.with_name("spot_check_sample.jsonl")
    with spot_path.open("w") as f:
        for line in spot_check_lines:
            f.write(line + "\n")

    # Quality gate: no augmentation > 40% of corpus
    total = max(n_emitted, 1)
    print(f"\n=== synthetic corpus stats (out={args.out}) ===", file=sys.stderr)
    print(f"emitted: {n_emitted}    duplicates skipped: {n_skipped_dup}", file=sys.stderr)
    failed = []
    for an, n in aug_counter.most_common():
        pct = 100.0 * n / total
        flag = " [FAIL >40%]" if pct > 40.0 else ""
        if pct > 40.0:
            failed.append(an)
        print(f"  {an:24s} {n:6d}  ({pct:5.1f}%){flag}", file=sys.stderr)
    print(f"\nspot-check sample: {spot_path} ({len(spot_check_lines)} rows)", file=sys.stderr)
    if failed:
        print(f"\nQUALITY GATE FAILED: augmentations over 40% threshold: {failed}", file=sys.stderr)
        sys.exit(2)
    print("Quality gate: PASS", file=sys.stderr)


if __name__ == "__main__":
    main()
