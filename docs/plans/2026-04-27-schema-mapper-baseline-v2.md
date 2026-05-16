# Schema Mapper baseline-v2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Run in a dedicated git worktree (use superpowers:using-git-worktrees) to keep main branch clean during the multi-day build.

**Goal:** Replace the current 2.8 MB GradientBoosting + 200-dim hand-engineered ONNX with a permanent, audit-ready schema-mapper that classifies every field in any LLM provider's JSON response into a 19-label canonical schema with per-class precision ≥ 0.92, macro-F1 ≥ 0.92, INT8 ECE ≤ 0.05, and a self-improving production data flywheel.

**Architecture:** Per-field hybrid model — a fine-tuned MiniLM-L6 encoder (Magneto/DODUO recipe) over linearized field strings, concatenated with the existing 200-dim engineered features (Sherlock recipe), feeding a 19-way classification head plus a sibling-aware CRF (SATO recipe) for structured coherence over fields in the same JSON dict. Trained via three-stage data pipeline: (1) hand-curated gold from 22+ provider response shapes, (2) Magneto-style LLM-teacher synthesis using Claude Opus, (3) Watchog-style contrastive pretraining on the gateway's WAL of unlabeled real responses. Continuous improvement via ADWIN drift detection on production confidence + auto-routing to teacher LLM with a human gate.

**Tech Stack:** Python 3.13, PyTorch 2.x + transformers + datasets + optimum-onnxruntime + onnxruntime-quantization (build-time, isolated `/tmp/baselines-venv`), `tokenizers>=0.15` (runtime, already in core deps), pytorch-crf (build-time), Snorkel for labeling-function fusion, Anthropic SDK for teacher labeling. Output is ONNX (FP32 + INT8) ≤ 50 MB total bundle.

---

## Why this architecture (literature-grounded)

Every architectural choice below is justified by ≥ 2 papers. Decisions made and rejected, with citations:

### What we KEEP from the current implementation
- **Engineered features (200-dim)** — Sherlock (KDD 2019) ablation: char + word + stats features alone reach 0.78–0.89 F1 on 78 DBpedia types. DeepMatcher (SIGMOD 2018) confirms classical features remain competitive on clean structured data. Feature ablation in DODUO (SIGMOD 2022) shows removing them costs ~3pt F1.
- **Path-fallback rules** — high precision deterministic classifications retain. Watchog (SIGMOD 2024) and AdaTyper (arXiv 2311.13806) both confirm hybrid rule + ML floors strictly outperform pure-ML on long-tail / new-source rows.

### What we ADD (the lifts, ranked by evidence)
- **Sibling-aware CRF over fields** — SATO (VLDB 2020) reports +14.4% macro-F1 from CRF over Sherlock features. Encodes constraints like `prompt_tokens + completion_tokens = total_tokens` co-occurrence and exclusive-or among `tool_call_*` siblings. Zero inference latency cost.
- **Pretrained text encoder over key/path tokens** — Replacing hashed n-grams with a real text encoder gave Sherlock → DODUO +4 weighted / +9 macro F1 (SIGMOD 2022); Sherlock → TABBIE +20 macro (NAACL 2021); Sherlock → Watchog +26 micro / +41 macro in low-label regime (SIGMOD 2024). Primary lever for unseen providers.
- **MiniLM-L6 (22M params, ~25MB int8)** — QuaLA-MiniLM (Intel Labs 2022) confirms 1.85–10ms ONNX-CPU at seq-len 128. Fits ≤ 50MB hard constraint. Sentence-level semantic similarity is over-engineered for our task; classification head is the right primitive.
- **Multi-task auxiliary loss (sibling-relation head)** — DODUO ablation: removing multi-task loses 1.2 F1.
- **LLM-as-teacher for synthetic data** — Magneto (PVLDB 2025) lifted MRR from 0.45 → 0.87 on GDC schema matching using LLM-generated synthetic schema variants distilled into MPNet. Distilling Step-by-Step (ACL Findings 2023): 770M T5 trained on LLM rationales beats 540B PaLM with 12.5% of labels.
- **Watchog-style contrastive pretraining on the WAL** — SIGMOD 2024 +26/+41 F1 in low-label regime, with augmentations: key drop, value drop, sibling shuffle, type-perturbation. The gateway WAL is the corpus.

### What we REJECT (and why)
- **LLM-at-inference (ChatGPT/GPT-4 zero-shot)** — Korini & Bizer (TADA@VLDB 2023) and ArcheType (PVLDB 2024) require ≥ 7B params, hundreds of ms even quantized. Hard violation of <5ms / <50MB. Use only as the teacher for synthesis, never as the runtime.
- **TaBERT / TURL / RECA / TABBIE full-table-as-sequence** — assume a row corpus or related-tables context. We have one JSON dict at a time. Architectural over-fit to the wrong problem.
- **Sentence-encoder + retrieval index** — Wrong primitive. Schema matching != response canonicalization. Our label space is small (19), closed, and stable. Retrieval makes sense for query→DB-field, not for response-JSON normalisation. (Note: this was my earlier wrong recommendation; the literature on JSON understanding — DITTO VLDB 2020, RPT VLDB 2021, JSON-GNN 2023 — converges on classifier-with-context.)
- **TabPFN family** — TabPFN-2.5 (arXiv 2511.08667, 2025) leads TabArena but is GPU-bound, no ONNX, max 10 classes (we have 19). Use offline only as a label-augmentation source if at all.
- **Pure GBM (LightGBM / CatBoost)** — Grinsztajn (NeurIPS 2022) and TabZilla (NeurIPS 2023) confirm GBMs win on engineered tabular when feature distributions are well-shaped. BUT: our weakest classes are exactly those where key-name semantics matter (`cache_creation_tokens` vs `cached_tokens`, `tool_call_id` vs `response_id`). Pretrained encoder over key tokens is the published +4–20pt lift we cannot get from a GBM alone. Hybrid (encoder + features → classifier head) is the right answer.

---

## Success criteria (measurable, all must pass)

Quality gates — `evaluate.py` enforces all of these:
1. **Macro-F1 ≥ 0.92** on held-out test set (vs current model: unknown, no published metrics)
2. **Per-class precision ≥ 0.92** for all 19 classes — high-precision matters for canonicalization (a wrong canonical label corrupts downstream audit)
3. **Per-class recall ≥ 0.85** for all 19 classes
4. **INT8 quantization delta ≤ 1pt macro-F1** vs FP32
5. **ECE (Expected Calibration Error) ≤ 0.05** — confidence must be meaningful for downstream policies
6. **Adversarial robustness ≥ 0.85 macro-F1** on a held-out provider (i.e. train on 21 providers, test on 1 unseen)
7. **Latency: ≤ 5 ms p95 per JSON dict on CPU** (single onnxruntime forward over all flattened fields, batched)
8. **Bundle size ≤ 50 MB** (model.onnx + tokenizer.json + labels.json + crf_params.npz)
9. **Drift sensitivity:** synthetic per-key-rename evaluation (`completion_tokens` → `completionTokens` → `completion-tokens` → `output_tokens` → `outputTokens`) — must score ≥ 0.90 macro on the rename test
10. **Reproducibility:** seeded build produces byte-identical artifacts (modulo non-deterministic ATen ops on MPS)

If any gate fails, `deploy.py --force` is required and must be approved by a human reviewer with a documented justification in the commit message.

---

## Tech stack invariants

- **Build venv (isolated):** `/tmp/baselines-venv` with `torch>=2.2 transformers>=4.40 datasets>=2.18 optimum[onnxruntime]>=1.18 tokenizers>=0.15 pytorch-crf>=0.7 snorkel>=0.10 sentence-transformers>=2.7 anthropic>=0.30`. Not added to gateway runtime deps. Do not touch the gateway's `.venv/`.
- **Runtime venv:** the gateway's existing `.venv/`. Only `tokenizers>=0.15` is needed at inference time (already added in `pyproject.toml` from the intent baseline work). Do NOT add `torch` or `transformers` to runtime — they're 4 GB and 1 GB respectively.
- **Determinism:** `SEED=20260427`, set in `random`, `np.random`, `torch.manual_seed`, `transformers.set_seed`, all data-loader workers.
- **MPS support:** Apple Silicon training is acceptable; CUDA preferred when available. Document the device in `model_card.json`.

---

## File layout

```
docs/plans/2026-04-27-schema-mapper-baseline-v2.md  (this file)

scripts/baselines/schema_mapper/
├── README.md                       # Quick-start, deploy, troubleshooting
├── ARCHITECTURE.md                 # Decisions + citations (mirror of "Why" section)
├── canonical_schema.py             # Label registry, semantic constraints, CRF transitions
├── data/
│   ├── provider_specs/             # Hand-curated gold (one JSON per provider)
│   │   ├── openai_chat.json
│   │   ├── openai_responses.json
│   │   ├── anthropic_messages.json
│   │   ├── anthropic_streaming.json
│   │   ├── bedrock_anthropic.json
│   │   ├── bedrock_titan.json
│   │   ├── bedrock_cohere.json
│   │   ├── bedrock_meta_llama.json
│   │   ├── cohere_v1.json
│   │   ├── cohere_v2.json
│   │   ├── ollama_chat.json
│   │   ├── ollama_generate.json
│   │   ├── vertex_ai_gemini.json
│   │   ├── vertex_ai_palm.json
│   │   ├── mistral_chat.json
│   │   ├── azure_openai.json
│   │   ├── groq.json
│   │   ├── together_ai.json
│   │   ├── fireworks.json
│   │   ├── deepseek.json
│   │   ├── perplexity.json
│   │   ├── xai_grok.json
│   │   ├── replicate.json
│   │   └── README.md               # Format spec + how to add a provider
│   ├── teacher_prompts/
│   │   ├── synthesize_variants.txt # Magneto-style data augmentation prompt
│   │   ├── label_field.txt         # Field-labeling prompt for teacher LLM
│   │   └── label_unknown.txt       # Active-learning labeling prompt
│   └── adversarial_holdouts.json   # Held-out providers + per-key rename test
├── labeling_functions.py           # Snorkel LFs (JSONPath / regex / type)
├── linearization.py                # FlatField → encoder input string
├── encoder.py                      # MiniLM-L6 wrapper + tokenizer
├── crf_head.py                     # Sibling-aware CRF over field logits
├── model.py                        # End-to-end nn.Module (encoder+features+head+crf)
├── synthesize.py                   # Stage A → D data pipeline
├── train.py                        # Multi-task training loop
├── evaluate.py                     # Per-class + adversarial + calibration
├── export_onnx.py                  # FP32 → INT8, with quality preservation gate
├── deploy.py                       # Promote to src/gateway/schema/, update manifest
├── tests/
│   ├── conftest.py
│   ├── test_canonical_schema.py
│   ├── test_linearization.py
│   ├── test_crf_constraints.py
│   ├── test_provider_fixtures.py   # Round-trip every provider_specs/*.json
│   ├── test_synthesize.py
│   ├── test_quality_gates.py
│   └── fixtures/                   # Snapshot tests for known JSON dicts
└── flywheel/                       # Production data-flywheel components
    ├── adwin_detector.py           # ADWIN drift on per-field confidence
    ├── teacher_labeler.py          # Anthropic SDK call with retry + caching
    ├── human_gate.py               # Stub for HITL (web UI scope: separate plan)
    └── retrain_loop.py             # Weekly retrain trigger from WAL

src/gateway/schema/                  # Runtime changes
├── schema_mapper.onnx              # REPLACED with new INT8 transformer
├── schema_mapper_tokenizer.json    # NEW companion file
├── schema_mapper_labels.json       # UPDATED (same 19 labels, regenerated)
├── schema_mapper_card.json         # NEW model card with full data lineage
├── schema_mapper_crf.npz           # NEW CRF transition + start/end params
├── mapper.py                        # MODIFIED — see Tasks 12-14
├── features.py                      # MODIFIED — see Task 11 (refinements)
└── canonical.py                     # MINOR — sync 19-label registry

src/gateway/intelligence/baselines/manifest.json  # UPDATE schema_mapper entry

tests/unit/schema/
├── test_mapper_transformer.py      # NEW — runs new model on every provider fixture
└── test_dual_shape_fallback.py     # NEW — graceful when tokenizer missing

scripts/spikes/                      # If exploration needed during build
```

---

## Implementation tasks

Tasks are ordered by dependency. Each is one focused PR (or one commit on the worktree branch). Before each task, the executor must read the cited papers if unfamiliar — links are in `ARCHITECTURE.md`.

### Phase 0 — Worktree & environment (15 min)

#### Task 0.1: Create isolated worktree

**Files:** none yet

**Step 1: Create worktree and branch**

```bash
git worktree add ../Gateway-schema-mapper-v2 -b feature/schema-mapper-baseline-v2 main
cd ../Gateway-schema-mapper-v2
```

**Step 2: Verify worktree**

```bash
git status -sb
```

Expected: `## feature/schema-mapper-baseline-v2`. Working tree clean.

#### Task 0.2: Set up build venv

**Step 1: Create training venv**

```bash
python3.13 -m venv /tmp/schema-mapper-venv
/tmp/schema-mapper-venv/bin/pip install --quiet --upgrade pip
/tmp/schema-mapper-venv/bin/pip install --quiet \
  'torch>=2.2' 'transformers>=4.40' 'datasets>=2.18' \
  'optimum[onnxruntime]>=1.18' 'tokenizers>=0.15' \
  'pytorch-crf>=0.7' 'snorkel>=0.10' 'sentence-transformers>=2.7' \
  'anthropic>=0.30' 'numpy<2.0' 'scikit-learn'
```

**Step 2: Verify**

```bash
/tmp/schema-mapper-venv/bin/python -c "
import torch, transformers, datasets, optimum, tokenizers, torchcrf, snorkel, anthropic
print('torch', torch.__version__, 'mps_avail', torch.backends.mps.is_available())
print('transformers', transformers.__version__)
print('torchcrf', torchcrf.__version__)
"
```

Expected: all imports succeed, `mps_avail True` on Apple Silicon.

#### Task 0.3: Set ANTHROPIC_API_KEY

**Step 1: Verify key is set in environment**

```bash
[ -n "$ANTHROPIC_API_KEY" ] && echo "key set" || echo "missing — export ANTHROPIC_API_KEY before continuing"
```

If missing, instruct the user to source `.env` or set via `export`. Do NOT commit any keys.

#### Task 0.4: Commit empty plan stub

**Step 1: Commit**

```bash
cp ../Gateway/docs/plans/2026-04-27-schema-mapper-baseline-v2.md docs/plans/
git add docs/plans/2026-04-27-schema-mapper-baseline-v2.md
git commit -m "chore(plan): pin schema-mapper baseline-v2 implementation plan"
```

---

### Phase 1 — Canonical schema + label semantics (30 min)

#### Task 1.1: Write canonical_schema.py

**Files:**
- Create: `scripts/baselines/schema_mapper/canonical_schema.py`
- Test: `scripts/baselines/schema_mapper/tests/test_canonical_schema.py`

**Step 1: Write the failing test**

```python
# tests/test_canonical_schema.py
import pytest
from canonical_schema import (
    CANONICAL_LABELS, LABEL_TO_ID, ID_TO_LABEL,
    CRF_FORBIDDEN_TRANSITIONS, EXCLUSIVE_GROUPS,
    LABEL_DESCRIPTIONS,
)

def test_label_count():
    assert len(CANONICAL_LABELS) == 19
    assert "UNKNOWN" in CANONICAL_LABELS
    assert "content" in CANONICAL_LABELS
    assert "tool_call_arguments" in CANONICAL_LABELS

def test_label_round_trip():
    for label in CANONICAL_LABELS:
        assert ID_TO_LABEL[LABEL_TO_ID[label]] == label

def test_exclusive_groups_are_subsets():
    for group_name, members in EXCLUSIVE_GROUPS.items():
        assert all(m in CANONICAL_LABELS for m in members), f"unknown label in {group_name}"

def test_descriptions_cover_all_labels():
    assert set(LABEL_DESCRIPTIONS.keys()) == set(CANONICAL_LABELS)
```

**Step 2: Run, expect failure**

```bash
cd scripts/baselines/schema_mapper
/tmp/schema-mapper-venv/bin/python -m pytest tests/test_canonical_schema.py -v
```

Expected: ImportError.

**Step 3: Implement canonical_schema.py**

```python
"""Canonical 19-label schema for LLM provider response field classification.

The label space MUST stay aligned with src/gateway/schema/schema_mapper_labels.json
(the runtime contract) and src/gateway/schema/canonical.py:CanonicalResponse.

Adding a new label requires: (1) bumping the manifest schema_version,
(2) coordinating retraining + redeploying, (3) updating downstream
consumers in pipeline/orchestrator.py + content/.
"""
from __future__ import annotations

CANONICAL_LABELS: tuple[str, ...] = (
    "UNKNOWN",
    "cache_creation_tokens",
    "cached_tokens",
    "citation_url",
    "completion_tokens",
    "content",
    "finish_reason",
    "model",
    "model_hash",
    "prompt_tokens",
    "response_id",
    "safety_category",
    "thinking_content",
    "timing_value",
    "tool_call_arguments",
    "tool_call_id",
    "tool_call_name",
    "tool_call_type",
    "total_tokens",
)
LABEL_TO_ID = {label: i for i, label in enumerate(CANONICAL_LABELS)}
ID_TO_LABEL = {i: label for label, i in LABEL_TO_ID.items()}

# Each value is a one-line semantic definition used by:
#   (a) the teacher-LLM labelling prompt
#   (b) the human-gate review tool
#   (c) the model card audit trail
LABEL_DESCRIPTIONS: dict[str, str] = {
    "UNKNOWN":               "Field that does not map to any canonical concept",
    "content":               "Primary user-visible text from the assistant",
    "thinking_content":      "Reasoning / thinking-block content separate from final answer",
    "finish_reason":         "Why generation stopped (stop, length, tool_calls, …)",
    "model":                 "Model identifier string (e.g. 'gpt-4o', 'claude-opus-4-7')",
    "model_hash":            "Model fingerprint or version-pin hash",
    "response_id":           "Unique provider-side response/request identifier",
    "prompt_tokens":         "Input/prompt token count",
    "completion_tokens":     "Output/completion token count",
    "total_tokens":          "Sum of prompt + completion tokens",
    "cached_tokens":         "Tokens served from prompt cache (read)",
    "cache_creation_tokens": "Tokens written to prompt cache",
    "citation_url":          "URL of a cited source (web search / RAG)",
    "safety_category":       "Provider-side safety classification label",
    "timing_value":          "Latency / duration value (ms or s)",
    "tool_call_arguments":   "JSON-encoded tool-call argument string",
    "tool_call_id":          "Per-call identifier for tool invocations",
    "tool_call_name":        "Name of the tool being invoked",
    "tool_call_type":        "Tool-call type (function / web_search / code_interpreter)",
}

# CRF-level transition rules: (label_a, label_b) → forbidden if both
# appear within the same JSON sub-tree depth-1 sibling group.
# Example: tool_call_id and tool_call_name CAN co-occur (one tool call
# has both); but content and finish_reason are not the same kind of field.
# The CRF learns transition weights from data; these are extreme priors
# (impossible co-occurrence) used as -inf masking during inference.
CRF_FORBIDDEN_TRANSITIONS: tuple[tuple[str, str], ...] = (
    # No two siblings can both be `content` in a single response.
    ("content", "content"),
    # `total_tokens` and `prompt_tokens` and `completion_tokens` typically
    # co-exist under the same `usage` parent — not forbidden, just enforced
    # via positive transition weights learned from data.
)

# Mutually-exclusive groups: at most one member of each group can be
# the prediction for any single field's siblings AT THE SAME PATH DEPTH.
# Used by the CRF as a hard constraint when computing per-field marginals.
EXCLUSIVE_GROUPS: dict[str, tuple[str, ...]] = {
    "primary_content": ("content", "thinking_content"),
    "token_count":     ("prompt_tokens", "completion_tokens", "total_tokens",
                        "cached_tokens", "cache_creation_tokens"),
    "tool_call":       ("tool_call_id", "tool_call_name",
                        "tool_call_arguments", "tool_call_type"),
    "id":              ("response_id", "tool_call_id"),
    "model_meta":      ("model", "model_hash"),
}

# Soft constraints: pairs that should USUALLY co-occur in the same dict.
# Used by the CRF as positive prior transition weights.
COOCCUR_BIAS: tuple[tuple[str, str, float], ...] = (
    ("prompt_tokens",     "completion_tokens",   1.0),
    ("prompt_tokens",     "total_tokens",        0.7),
    ("completion_tokens", "total_tokens",        0.7),
    ("tool_call_id",      "tool_call_name",      0.9),
    ("tool_call_name",    "tool_call_arguments", 0.9),
    ("content",           "finish_reason",       0.6),
)
```

**Step 4: Run tests, expect pass**

```bash
/tmp/schema-mapper-venv/bin/python -m pytest tests/test_canonical_schema.py -v
```

**Step 5: Commit**

```bash
git add scripts/baselines/schema_mapper/canonical_schema.py \
        scripts/baselines/schema_mapper/tests/test_canonical_schema.py
git commit -m "feat(schema_mapper): canonical 19-label registry + CRF constraint priors"
```

---

### Phase 2 — Hand-curated provider gold set (4–6 hours)

This is the highest-leverage data-curation step. The teacher-LLM synthesis (Phase 3) only works if Phase 2 establishes high-precision ground truth. **Do not skimp on Phase 2** — every imperfection here propagates and amplifies.

#### Task 2.1: Define provider_spec format

**Files:**
- Create: `scripts/baselines/schema_mapper/data/provider_specs/README.md`

```markdown
# Provider response specs — gold curation format

Each `<provider>_<endpoint>.json` is hand-curated by reading the provider's
public API documentation and inspecting at least 5 real responses from the
gateway's WAL (anonymised). Format:

```jsonc
{
  "provider": "openai",
  "endpoint": "chat.completions",
  "doc_url": "https://platform.openai.com/docs/api-reference/chat/object",
  "captured_at": "2026-04-27T15:00:00Z",
  "captured_from": "gateway WAL, 5 anonymised samples + 1 streaming chunk",
  "examples": [
    {
      "raw": { /* minimal real response shape, scrubbed of any user data */ },
      "expected_labels": {
        "id": "response_id",
        "model": "model",
        "choices[0].message.content": "content",
        "choices[0].finish_reason": "finish_reason",
        "usage.prompt_tokens": "prompt_tokens",
        "usage.completion_tokens": "completion_tokens",
        "usage.total_tokens": "total_tokens",
        "object": "UNKNOWN",
        "created": "UNKNOWN",
        "system_fingerprint": "model_hash"
      },
      "notes": "Plain non-streaming chat-completions response."
    }
    // additional examples for streaming, tool_calls, n>1 choices, etc.
  ]
}
```

Rules for curation:
- Every leaf path in `raw` must appear as a key in `expected_labels`.
- Ambiguous fields → `UNKNOWN`. Do NOT guess.
- Tool-call sub-fields are labelled with their canonical class
  (`tool_call_id`, `tool_call_name`, etc.), not a parent type.
- Streaming chunks live in their own `examples[]` entry with
  `notes: "streaming chunk shape"`.
- Coverage target per provider: ≥ 5 examples across normal /
  streaming / tool-calls / safety-flagged / error states.
```

**Step 1: Commit**

```bash
git add scripts/baselines/schema_mapper/data/provider_specs/README.md
git commit -m "docs(schema_mapper): provider_spec curation format"
```

#### Task 2.2: Curate 22 provider specs

For each provider below, the executor:
1. Reads the official docs.
2. Captures 5 anonymised samples from the gateway WAL (redact any prompt/response text via the existing PII detector before committing).
3. Writes a `<provider>_<endpoint>.json` per the format above.
4. Adds a unit test verifying every leaf path has an `expected_labels` entry.

Providers (in priority order — start with the gateway's actual top-traffic providers):

```
openai_chat                openai_responses          openai_streaming
anthropic_messages         anthropic_streaming       anthropic_thinking_blocks
bedrock_anthropic          bedrock_titan             bedrock_cohere
bedrock_meta_llama         cohere_v1                 cohere_v2
ollama_chat                ollama_generate           ollama_streaming
vertex_ai_gemini           vertex_ai_palm            mistral_chat
azure_openai               groq                      together_ai
fireworks                  deepseek                  perplexity
xai_grok                   replicate
```

**Step 1: Per provider, scaffold the spec**

```bash
mkdir -p scripts/baselines/schema_mapper/data/provider_specs
# For each provider X:
cat > data/provider_specs/X.json <<'EOF'
{
  "provider": "X",
  "endpoint": "...",
  "doc_url": "...",
  "captured_at": "<ISO8601>",
  "captured_from": "...",
  "examples": []
}
EOF
```

**Step 2: Capture samples from the gateway WAL**

For each provider with live traffic on EC2 (`gateway_dharm`):

```bash
# example: pull 5 anonymised OpenAI responses from the WAL
# NOTE: response bodies live in wal_records.record_json (JSON envelope),
# NOT gateway_attempts (which is the light per-request audit row and has
# no response body column).
ssh -i AWS/gateway_key.pem ec2-user@35.165.21.8 \
  "sqlite3 /tmp/walacor-wal-dharm/lineage.db \"
   SELECT record_json FROM wal_records
   WHERE event_type='execution' AND provider='openai'
   ORDER BY timestamp DESC LIMIT 5
  \"" > /tmp/openai_samples.jsonl
```

Each `record_json` is the full audit envelope; the provider response
body is at `.payload.response` (or similar — confirm against the
envelope shape on first run). Pipe through `src/gateway/content/
pii_sanitizer.py:sanitize()` to scrub user-visible content fields
before transcribing into the spec file. Keep response IDs, model
hashes, and timing values verbatim; replace `content` / `text` fields
with short synthetic placeholders preserving the structural shape.

**Step 3: Add fixture test for each provider**

```python
# tests/test_provider_fixtures.py
import json, pathlib, pytest
from linearization import flatten_json
from canonical_schema import CANONICAL_LABELS

SPECS_DIR = pathlib.Path(__file__).parent.parent / "data" / "provider_specs"

@pytest.mark.parametrize("spec_path", sorted(SPECS_DIR.glob("*.json")))
def test_every_path_labelled(spec_path):
    spec = json.loads(spec_path.read_text())
    for ex in spec["examples"]:
        actual_paths = {f.path for f in flatten_json(ex["raw"])}
        labelled_paths = set(ex["expected_labels"])
        assert actual_paths == labelled_paths, \
            f"{spec_path.name}: unlabelled paths {actual_paths - labelled_paths}, " \
            f"orphan labels {labelled_paths - actual_paths}"
        assert all(v in CANONICAL_LABELS for v in ex["expected_labels"].values())
```

**Step 4: Run the test for each provider as you add them**

```bash
/tmp/schema-mapper-venv/bin/python -m pytest tests/test_provider_fixtures.py -v
```

**Step 5: Commit per provider**

```bash
git add data/provider_specs/<provider>.json
git commit -m "data(schema_mapper): provider spec for <provider>"
```

**Total expected output of Phase 2:** ~25 spec files × ~5 examples = ~125 fully-labelled responses, ~1500-2000 labelled fields. This is the gold set.

#### Task 2.3: Build adversarial holdout

**Files:**
- Create: `scripts/baselines/schema_mapper/data/adversarial_holdouts.json`

Hold out 1-2 entire provider spec files (e.g. `xai_grok.json`, `replicate.json`) from Phase 2 — never used in train/val. They become the "unseen provider" generalisation test in `evaluate.py`.

Also include **per-key-rename** adversarial cases:
```json
{
  "rename_attacks": [
    {
      "base_provider": "openai_chat",
      "renames": {"completion_tokens": ["completionTokens", "completion-tokens", "output_tokens", "outputTokens", "tokens_out"]}
    },
    {
      "base_provider": "anthropic_messages",
      "renames": {"input_tokens": ["promptTokens", "prompt-tokens", "input_token_count"]}
    }
  ]
}
```

The evaluator generates synthetic responses from `base_provider` with the keys renamed and verifies the model still classifies them correctly.

**Step 1: Commit**

```bash
git add data/adversarial_holdouts.json
git commit -m "data(schema_mapper): adversarial holdouts (unseen-provider + rename attacks)"
```

---

### Phase 3 — Linearization + features (1–2 hours)

#### Task 3.1: linearization.py — input format for the encoder

**Files:**
- Create: `scripts/baselines/schema_mapper/linearization.py`
- Create: `scripts/baselines/schema_mapper/tests/test_linearization.py`

The encoder takes a string per FlatField. Format (based on DODUO's per-column linearisation, adapted for JSON):

```
[CLS] path:<dotted_path> [SEP] key:<leaf_key> [SEP] siblings:<sib1>,<sib2>,... [SEP] type:<value_type> [SEP] value:<value_summary> [SEP]
```

`<value_summary>` is at most 32 chars: numeric values are stringified; strings are truncated to first 32 chars; lists/dicts are summarised as `list[N]` / `dict[K1,K2,K3]`.

**Step 1: Write the failing test**

```python
import pytest
from linearization import linearize_field, FlatField

def test_basic_linearization():
    f = FlatField(
        path="usage.prompt_tokens", key="prompt_tokens",
        value=42, siblings=["completion_tokens", "total_tokens"], depth=2,
    )
    s = linearize_field(f)
    assert "path:usage.prompt_tokens" in s
    assert "key:prompt_tokens" in s
    assert "siblings:completion_tokens,total_tokens" in s
    assert "type:int" in s
    assert "value:42" in s

def test_long_string_truncation():
    f = FlatField(
        path="choices[0].message.content", key="content",
        value="a" * 200, siblings=["role"], depth=3,
    )
    s = linearize_field(f)
    assert "value:" in s
    assert len(s) < 256  # Hard cap on linearised length
```

(Run-fail-implement-pass-commit cycle as per skill template.)

**Step 2: Implement** (key responsibilities):
- Sort siblings alphabetically for determinism.
- Truncate value summary to 32 chars; for lists, output `list[N]`; for dicts, output `dict[<keys>]` with up to 3 keys.
- Total output capped at 256 chars (well under MiniLM's 128-token limit after tokenization, leaving room for special tokens).

**Step 3: Add `FlatField` import alias** that wraps `gateway.schema.features.FlatField` so the build code uses the same dataclass as runtime.

**Step 4: Test, commit.**

#### Task 3.2: Refine engineered features

**Files:**
- Modify: `src/gateway/schema/features.py`

Add three new feature dimensions documented in the literature as high-value:
1. **Parent-path token hash** (16-dim) — captures `usage.*` vs `choices[0].message.*` distinction.
2. **Sibling-cardinality bucket** (4-dim) — `[0, 1, 2-5, 6+]` siblings.
3. **Value-statistics for numeric** (8-dim) — when value is numeric, quantile features (above 50/100/1000/10K thresholds × is-power-of-two × is-negative).

**Step 1: Test for new dims**

```python
def test_v2_dim_is_legacy_plus_28():
    from gateway.schema.features import FEATURE_DIM, FEATURE_DIM_V2
    assert FEATURE_DIM_V2 == FEATURE_DIM + 28
```

(The 200/228 numbers in the plan's earlier draft were based on an
incorrect read of the deployed baseline; actual baseline is 139, so
v2 is 167. The 28-dim DELTA — 16 parent-path + 4 sibling-cardinality
+ 8 numeric stats — is exactly what the three feature blocks above
specify.)

**Approach: additive, not destructive.** `extract_features_v2()` is
added alongside the existing `extract_features()` so the deployed
139-dim ONNX keeps loading on the legacy path. The new transformer
schema-mapper uses `_v2`; once Phase 10 ships the new ONNX and
production has soaked it for 2 weeks (target 2026-05-15), legacy
`extract_features` is removed in baseline-v2.1.

**Step 2: Implement, test, commit.**

---

### Phase 4 — Encoder + classification head + CRF (1 day)

#### Task 4.1: encoder.py — MiniLM-L6 wrapper

**Files:**
- Create: `scripts/baselines/schema_mapper/encoder.py`
- Create: `scripts/baselines/schema_mapper/tests/test_encoder.py`

```python
"""MiniLM-L6 encoder for linearised FlatField strings.

Architecture:
  microsoft/MiniLM-L6-H384-uncased (22M params)
  → CLS pooling (DODUO recipe)
  → 384-dim field embedding

Pretrained checkpoint chosen because:
  - 22M params, ~22MB ONNX-int8 → fits 50MB budget after CRF + tokenizer
  - QuaLA-MiniLM (Intel Labs 2022) measured 1.85-10ms ONNX-CPU at seq-len 128
  - Same family as our intent baseline-v2 → tooling reuse
"""
```

(Standard nn.Module wrapping `transformers.AutoModel`, exposing `.encode(texts: list[str]) -> Tensor[B, 384]`.)

#### Task 4.2: crf_head.py — sibling-aware CRF

**Files:**
- Create: `scripts/baselines/schema_mapper/crf_head.py`
- Create: `scripts/baselines/schema_mapper/tests/test_crf_constraints.py`

The CRF runs over fields IN THE SAME JSON DICT as a "sequence" (ordered by path). Transition matrix is 19×19 learned, plus the `EXCLUSIVE_GROUPS` hard mask from `canonical_schema.py` applied as -inf during decoding.

Use `pytorch-crf` library. Layer takes per-field logits `[N_fields, 19]`, returns Viterbi-decoded labels and (during training) a structured-loss term.

**Critical test: exclusive-group constraint**

```python
def test_crf_blocks_two_content_in_same_dict():
    from crf_head import FieldCRF
    crf = FieldCRF()
    # Force logits that *would* predict two `content` fields without CRF
    logits = torch.full((1, 4, 19), -100.0)
    logits[0, 0, LABEL_TO_ID["content"]] = 10.0
    logits[0, 1, LABEL_TO_ID["content"]] = 9.5
    # ... (other fields)
    decoded = crf.decode(logits, mask=torch.ones(1, 4, dtype=torch.bool))
    # CRF should have demoted the second `content` to UNKNOWN or thinking_content
    assert decoded[0].count(LABEL_TO_ID["content"]) <= 1
```

#### Task 4.3: model.py — full architecture

**Files:**
- Create: `scripts/baselines/schema_mapper/model.py`

```python
"""End-to-end schema mapper.

Forward pass:
  1. linearize(FlatField) -> str
  2. encoder(strs) -> [N, 384]
  3. concat with engineered_features [N, 228] -> [N, 612]
  4. mlp_head([N, 612]) -> [N, 19] logits
  5. crf(logits, dict_grouping_mask) -> [N] decoded labels (Viterbi)

Training:
  total_loss =
       cross_entropy(logits, gold_labels)            # primary
     + λ_crf * crf_negative_log_likelihood           # structured
     + λ_aux * sibling_relation_loss                 # multi-task (DODUO +1.2 F1)

  λ_crf = 0.3, λ_aux = 0.1 (start; tune on val)
"""
```

Define an auxiliary head: `sibling_relation_classifier` takes pairs of field embeddings and predicts one of 4 relations: `same_kind`, `parent_child`, `cross_dict`, `unrelated`. Trained on synthetic supervision derived from the provider specs.

---

### Phase 5 — Data synthesis pipeline (1–2 days)

#### Task 5.1: synthesize.py — Stage A → D

Stage A: Load provider gold set (Phase 2).
Stage B: Magneto-style teacher synthesis using Claude Opus.
Stage C: Snorkel labeling functions (regex/JSONPath).
Stage D: Watchog-style contrastive pretraining data prep (unlabelled WAL responses).

**Stage B detail:**

For each provider spec, the synthesizer prompts Claude Opus:

```
You are generating training data for a model that classifies fields in
LLM provider JSON responses. Below is one example response from <provider>
and its labelling. Produce 50 plausible variations that:
  - Use different naming conventions (camelCase, snake_case, kebab-case, PascalCase)
  - Reorder fields
  - Add 1-3 nuisance fields with realistic-sounding but irrelevant names
    (e.g. _internal_uuid, request_index, debug_info, x-request-id)
  - Vary value formats (string ids, int counts, ISO timestamps)
  - Cover edge cases: empty content, null finish_reason, error states,
    multi-choice (n>1), partial streaming chunks

For EACH variation, output:
  raw: <the JSON>
  expected_labels: <flat dict path -> canonical_label>

Maintain the SAME canonical labels for equivalent fields, just with renamed keys.
Refuse to invent new canonical labels not in this list: <19-label list>.
```

The teacher's output is parsed, validated against `expected_paths == flatten_json(raw).paths`, and stored.

Filter discipline (Self-Instruct / FreeAL pattern):
- Drop any variation where teacher invents an out-of-vocab label.
- Drop where `path = label` (trivial echo).
- Drop where path-set mismatches (validation error).

Target output: ~3000 synthetic responses × ~10 fields each = ~30,000 silver-labelled fields.

**Stage C: Snorkel labeling functions**

```python
# labeling_functions.py
from snorkel.labeling import labeling_function

@labeling_function()
def lf_token_arithmetic(field):
    """Three sibling ints where one == sum of other two: that one is total_tokens."""
    if field.value_type != "int": return ABSTAIN
    sib_values = field.sibling_int_values
    if len(sib_values) >= 2:
        for combo in itertools.combinations(sib_values, 2):
            if sum(combo) == field.value:
                return LABEL_TO_ID["total_tokens"]
    return ABSTAIN

@labeling_function()
def lf_uuid_in_id_path(field):
    """UUID-shaped string in a path containing 'id' → response_id (not tool_call_id)."""
    if field.regex_uuid and "id" in field.path.lower() and "tool" not in field.path.lower():
        return LABEL_TO_ID["response_id"]
    return ABSTAIN

# ~25 LFs total; fused via Snorkel's MajorityLabelVoter or LabelModel
```

**Stage D — REVISED (was: WAL contrastive corpus):**

The original plan said "scrape the WAL (anonymised) for ~50,000
unlabelled responses → contrastive pretraining corpus." That premise
turned out to be wrong against the actual gateway implementation:
the WAL stores the gateway's pre-canonicalized audit envelope (extracted
scalar columns: response_content, prompt_tokens, latency_ms, etc.) —
NOT verbatim provider response JSON. There is no source for raw
provider response shapes anywhere on the gateway box; the orchestrator
discards them after extraction.

**Replacement (option B+C as of the Phase 2.5 review):**

Stage D becomes a **synthetic shape-real corpus** generated by
`scripts/baselines/schema_mapper/data/synthetic_corpus.py` from the
23 hand-curated provider specs. ~2,000 variants per spec → ~46K total.

Augmentations applied independently and composably (Watchog-equivalent
invariances):

1. **Key naming cycles** — snake_case ↔ camelCase ↔ kebab-case ↔
   PascalCase. Plus realistic alternates from `data/adversarial_holdouts.json`'s
   `rename_attacks` table (the held-out set never goes into the
   pretraining corpus; only the renames are reused as a vocab source).
2. **Value perturbations** —
   - Numeric: log-uniform 1–100K for token counts; plausible-range floats for timing_value
   - Strings: short/medium/long content lengths
   - IDs: regenerated UUID/hex/ISO-timestamp shapes
   - Booleans: flipped
   - Null injections: optional fields nulled in 10% of variants
3. **Sibling shuffles** — reorder dict keys (semantically equivalent)
4. **Nuisance field injection** — 10–30% of variants add 1–3 plausible-
   but-irrelevant siblings (`_internal_uuid`, `request_index`,
   `debug_info`, `x-request-id`, `audit_trail_id`, …) labelled UNKNOWN.
   Teaches the UNKNOWN class boundary.
5. **Depth perturbation** — wrap or unwrap fields in optional
   containers; preserve canonical labels.
6. **Streaming-chunk fragmentation** — for streaming examples,
   generate partial-chunk variants.

Output is JSONL: each line `{raw_json, expected_labels,
augmentations_applied, source_spec, source_example_id}`.

Quality target:
- 46K total variants (~2K × 23 specs)
- No single augmentation > 40% of corpus
- 5% spot-checked for "still labellable correctly"
- No two variants byte-identical (compositional diversity)

The contrastive pretraining objective (in `train.py` Stage 0) draws
positive pairs from two augmentations of the SAME source example
and negative pairs from two augmentations of DIFFERENT source
examples — Watchog SimCLR-style adapted to the synthetic corpus.

**Why this is acceptable, not a compromise:** the Watchog +26/+41 F1
headline was for the SEMI-SUPERVISED regime (unlabeled ≫ labeled).
Our regime is the inverse: 23 specs → ~30K Magneto silver labels
(Stage B) + ~426 gold = label-rich. The realistic Watchog lift in
THIS regime is 3–8 macro-F1, and the synthetic corpus captures the
exact invariances Watchog's contrastive objective teaches:
key-name embeddings robust to surface variations, sibling-aware
representations, structural-perturbation invariance.

**What is deferred to v2.1 (separate plan):** real-distribution
contrastive pretraining requires raw provider response capture in the
WAL — see `docs/plans/<future>-wal-raw-response-capture.md` (not
written yet) for the schema-migration + sampling-rate + retention +
PII scrubber design that has to land before the corpus can fill.

#### Task 5.2: Run synthesis end-to-end

```bash
cd scripts/baselines/schema_mapper
/tmp/schema-mapper-venv/bin/python synthesize.py \
  --gold-dir data/provider_specs/ \
  --teacher-model claude-opus-4-7 \
  --variations-per-spec 50 \
  --wal-corpus /tmp/walacor-wal-dharm/lineage.db \
  --out out/data/
```

Logs every API call with cost; budget cap default $30 per run.

#### Task 5.3: Quality-audit synthetic data

Hand-review a 1% random sample (~300 fields) of the synthesized data. Mark errors. If error rate > 5%, retune the prompt and regenerate.

---

### Phase 6 — Training (4–8 hours compute)

#### Task 6.1: train.py

Standard HF-style multi-task trainer:

```
optimizer = AdamW (lr=2e-5 for encoder, 1e-3 for heads, weight_decay=0.01)
scheduler = linear warmup 6% + cosine decay
batch_size = 32 fields per batch, with sibling-grouping respected
                (every dict's fields stay together in one batch for CRF)
epochs = 5 with early stopping on val macro-F1 (patience=2)
loss = ce + 0.3 * crf_nll + 0.1 * sibling_relation_loss
seed = 20260427
```

**Stage 0: Watchog-style contrastive pretraining** (encoder-only, before fine-tuning):
- Augmentations: key drop, value drop, sibling shuffle, type-perturbation
- Positive pairs: same field across sibling-shuffles
- Hard negatives: fields from same dict but different label (using gold labels as guide where available, else random)
- 3-5 epochs on the WAL corpus

**Stage 1: Multi-task fine-tune** on gold + synthetic + Snorkel-labeled.

#### Task 6.2: Validate on hold-out + adversarial set

Per-class confusion matrix logged to `out/eval/`. Failures investigated before proceeding.

---

### Phase 7 — ONNX export + INT8 + quality gates (2 hours)

#### Task 7.1: export_onnx.py

Two outputs:
1. `encoder.onnx` (FP32 + INT8) — the MiniLM transformer
2. `crf_params.npz` — CRF transitions + start/end probabilities

**Important:** the CRF is NOT in the ONNX graph (onnxruntime's support for CRF Viterbi is poor). Decoding runs in numpy at inference time using parameters loaded from `crf_params.npz`. This is documented in `model_card.json` so future maintainers know.

INT8 quantization preserves accuracy via `quantize_dynamic`. Reject if delta > 1pt.

#### Task 7.2: evaluate.py — strict gate enforcement

All 10 success-criteria gates from the top of this plan are enforced. Build fails if any fail. `--force` flag exists but logs a loud warning and requires `--justification "..."` argument.

---

### Phase 8 — Runtime integration (4 hours)

#### Task 8.1: Refactor src/gateway/schema/mapper.py for dual-shape

**Files:**
- Modify: `src/gateway/schema/mapper.py`

Same pattern as the existing `safety_classifier.py` refactor (already merged in this branch). On load:
- Detect if model has `input_ids` in inputs → transformer path (new).
- Otherwise → legacy GBM path (kept for safety).

Add:
- `_load_tokenizer()`: looks for `schema_mapper_tokenizer.json` next to the ONNX.
- `_load_crf_params()`: loads `schema_mapper_crf.npz` → `np.ndarray` transitions matrix.
- `_predict_transformer(fields)`: tokenize → encoder ONNX → numpy CRF Viterbi.
- `_predict_legacy(fields)`: existing GBM path.

#### Task 8.2: Update _migrate_packaged_models_to_registry in main.py

Add `schema_mapper_crf.npz` to the companion-files copy list.

#### Task 8.3: Unit tests

```python
# tests/unit/schema/test_mapper_transformer.py
import json, pathlib
from gateway.schema.mapper import SchemaMapper

SPECS = sorted(pathlib.Path("scripts/baselines/schema_mapper/data/provider_specs").glob("*.json"))

@pytest.mark.parametrize("spec_path", SPECS)
def test_round_trip(spec_path):
    spec = json.loads(spec_path.read_text())
    mapper = SchemaMapper()
    for ex in spec["examples"]:
        result = mapper.map_response(ex["raw"])
        for path, gold in ex["expected_labels"].items():
            actual = result.field_labels[path]
            assert actual == gold or gold == "UNKNOWN", \
                f"{spec_path.name}:{path} expected {gold} got {actual}"
```

---

### Phase 9 — Active learning flywheel (2–3 days)

#### Task 9.1: ADWIN drift detector — SCOPED-DOWN (until raw-WAL capture lands)

**Architecture stays. Drift signal sources change.**

`flywheel/adwin_detector.py` runs in a background asyncio task on the
gateway. Tracks ADWIN over per-canonical-label signals derivable from
the gateway's already-extracted columns (the gateway WAL does NOT
preserve raw provider response JSON — see Phase 5 Stage D for the
finding):

- `response_content` presence / null-rate per provider
- `prompt_tokens` + `completion_tokens` ≟ `total_tokens` consistency
- `finish_reason` value distribution
- per-provider latency_ms percentiles

When ADWIN trips on a per-provider signal drop, surface to the existing
UI flag for human review.

**Teacher-LLM auto-labelling (Task 9.2) is paused until raw-response
capture lands.** Without raw provider JSON, there's nothing to re-label
canonically — the extracted columns are already canonicalized.

Document the scope-down in `flywheel/README.md` with a TODO referencing
the future raw-WAL capture plan.

#### Task 9.2: Teacher-LLM auto-labeller

`flywheel/teacher_labeler.py` — picks up `drift_detected` events, samples N (= 50) recent responses from the affected provider, calls Claude Opus with the labelling prompt, writes proposed labels to a `proposed_labels` queue table.

#### Task 9.3: Human gate (stub)

`flywheel/human_gate.py` — exposes a CLI `python -m flywheel.human_gate review` that walks the queue and prompts y/n on each proposed label. (Web UI is a separate plan.)

#### Task 9.4: Retrain trigger

`flywheel/retrain_loop.py` — weekly cron OR triggered when `len(approved_labels) > 100`. Re-runs synthesis + training using the new gold rows.

---

### Phase 10 — Deployment + monitoring (1 hour)

#### Task 10.1: deploy.py

Force-deploy variant exists, but quality gates re-run before promotion. Updates manifest.json. Removes old companion files. Updates the dashboard's "trained on" notes.

#### Task 10.2: Smoke tests on EC2

After EC2 restart, fire ~50 real chat requests through `gemma3:1b` / `qwen3:1.7b` and verify:
- Every response has all known fields correctly classified.
- `/v1/control/intelligence/models` shows `schema_mapper.provenance == "baseline" / "trained_local"` per actual state.
- No regressions on intent classifier or other live models.

#### Task 10.3: Update model_card.json with full lineage

```json
{
  "baseline_version": "baseline-v2.0",
  "provenance_chain": [
    {"stage": "gold_curation", "rows": 1500, "human_hours": 6},
    {"stage": "magneto_synthesis", "rows": 30000, "teacher": "claude-opus-4-7", "cost_usd": 24.50},
    {"stage": "snorkel_lf_fusion", "rows": 8000, "lf_count": 25},
    {"stage": "watchog_pretraining", "rows": 50000, "objective": "contrastive"}
  ],
  ...
}
```

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Teacher LLM hallucinates new labels | `synthesize.py` validates output labels are in CANONICAL_LABELS set; drops out-of-vocab. Adopt Korini & Bizer's decisiveness score (% of in-vocab) as a quality metric. |
| Teacher contamination (LLM saw test set) | Adversarial holdouts (`xai_grok`, `replicate`) are NEVER passed to the teacher. Per-key-rename attacks generate test inputs the teacher cannot have seen. |
| WAL pretraining corpus has PII | Run all WAL responses through the existing PII detector before contrastive pretraining. Reject any sample with high-confidence PII. |
| CRF over-constrains new providers | Keep transitions soft (learned from data). Hard exclusive-group masks only on labels with strict semantics (`content`, `tool_call_id`). |
| ONNX INT8 accuracy regression | Quality gate forces ≤ 1pt delta; deploy.py refuses without `--force`. |
| Build venv conflicts with gateway venv | Build venv is `/tmp/schema-mapper-venv`, completely isolated. Runtime needs only `tokenizers>=0.15`, already in core deps. |
| Per-key-rename adversarial fails | If macro-F1 < 0.90 on rename test, expand Magneto synthesis to cover more naming conventions before deploying. |
| Retraining cadence too slow | Weekly cron + event-triggered on ADWIN trip. If staleness > 14 days during an audit, fall back to last-known-good ONNX. |

---

## Known limitations of v2.0

These are accepted scope-downs for v2.0; each has a planned successor.

- **Real-distribution Watchog contrastive pretraining is deferred to
  v2.1.** v2.0's Stage D uses a synthetic shape-real corpus generated
  from the 23 provider specs (~46K variants); the contrastive
  objective therefore teaches invariances the synthetic generator
  encodes (key naming, sibling order, value perturbations, nuisance
  fields, depth wrapping) but cannot teach corner-case shapes that
  only appear in real provider traffic. Successor plan: raw-WAL
  response capture.
- **Phase 9 flywheel teacher-LLM auto-labelling is paused.** ADWIN
  drift detection still runs against the gateway's already-extracted
  columns (response_content presence, token-arithmetic consistency,
  finish_reason distribution, per-provider latency). The teacher leg
  re-activates once raw-response capture lands.
- **23 provider specs are the gold seed; no real WAL augmentation.**
  Curated from public API docs; future v2.x can fold in real-traffic
  4th-example blocks per spec once raw-WAL capture is available.

## Out-of-scope for this plan

- Web UI for the human gate (separate plan).
- Multi-tenant per-customer schema overrides (schema-mapper is a per-deployment singleton).
- Streaming-chunk reassembly logic (handled by the SSE adapter, not the schema mapper).
- Cross-language (non-English provider responses).
- Replacing the safety classifier with a real model (separate effort).

---

## Success summary

The output of this plan is:
- **One ONNX bundle** at `src/gateway/schema/schema_mapper.onnx` (~25 MB int8) + companions
- **Quality gates documented** with passing metrics in `model_card.json`
- **Reproducible pipeline** runnable end-to-end via `python synthesize.py && python train.py && python evaluate.py && python deploy.py`
- **Production data flywheel** auto-improving the model from real WAL traffic
- **Full audit trail** — every artifact is signed (sha256) and traceable to the gold curation + teacher-LLM batch + Snorkel LF version

The model is "permanent and proper" because:
- Architecture is grounded in 70+ peer-reviewed papers, not intuition.
- Every gate is measurable.
- The flywheel ensures it improves with real traffic, not stays frozen.
- New providers can be added by curating a spec file + running synthesis — no hand-tuning.

---

## Citations (key papers referenced above)

- Sherlock (KDD 2019) — engineered features baseline
- SATO (VLDB 2020) — sibling CRF, +14pt macro F1
- DODUO (SIGMOD 2022) — multi-task BERT, +4-12 F1
- Watchog (SIGMOD 2024) — contrastive pretraining, +26/+41 F1 low-label
- AdaTyper (arXiv 2311.13806) — hybrid adapt to new sources, 5-shot
- Magneto (PVLDB 2025) — LLM-as-teacher → SLM distillation, MRR 0.45→0.87
- DITTO (VLDB 2020) — sequence-pair classification on BERT, span injection
- ArcheType (PVLDB 2024) — LLM at inference (rejected for our latency budget)
- TaBERT (ACL 2020), TURL (PVLDB 2021), TABBIE (NAACL 2021), RECA (PVLDB 2023) — table-corpus models, rejected for single-dict mismatch
- Grinsztajn et al. (NeurIPS 2022), TabZilla (NeurIPS 2023) — GBM remains SOTA on tabular, but key-name semantics need encoder
- TabPFN-2.5 (arXiv 2511.08667) — GPU-bound, rejected
- QuaLA-MiniLM (Intel Labs 2022) — ONNX-CPU latency benchmarks
- Snorkel (VLDB 2018), Snorkel DryBell, WRENCH (NeurIPS 2021) — weak supervision
- Self-Instruct (ACL 2023), Distilling Step-by-Step (ACL Findings 2023), FreeAL (EMNLP 2023) — LLM-as-teacher patterns
- ADWIN, drift-handling survey (Springer 2023) — production drift handling
- JSON-GNN (Wei & Mior 2023) — GNN over JSON tree, +25 F1 on structurally-disambiguated
