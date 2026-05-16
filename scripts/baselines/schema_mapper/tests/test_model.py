"""Smoke tests for the end-to-end SchemaMapper.

Loads a small encoder via HuggingFace (cached on first run) and verifies
the forward pass + loss + decode all run without shape errors. The
real quality validation lives in evaluate.py (Phase 7).
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from canonical_schema import LABEL_TO_ID  # noqa: E402
from crf_head import NUM_LABELS  # noqa: E402

# Encoder downloads MiniLM on first call. Skip by default; gate on env var.
import os

skip_if_no_download = pytest.mark.skipif(
    os.environ.get("SCHEMA_MAPPER_RUN_SLOW") != "1",
    reason="needs SCHEMA_MAPPER_RUN_SLOW=1 (downloads MiniLM on first run)",
)


@pytest.fixture(scope="module")
def model():
    if os.environ.get("SCHEMA_MAPPER_RUN_SLOW") != "1":
        pytest.skip("needs SCHEMA_MAPPER_RUN_SLOW=1")
    from model import SchemaMapper

    torch.manual_seed(0)
    return SchemaMapper(feature_dim=8, head_hidden=16)  # tiny for test speed


@skip_if_no_download
def test_forward_shapes(model):
    B, N, T = 2, 3, 16
    input_ids = torch.randint(0, 1000, (B * N, T))
    attn_mask = torch.ones(B * N, T, dtype=torch.long)
    features = torch.randn(B * N, 8)
    out = model(input_ids, attn_mask, features)
    assert out["embeddings"].shape == (B * N, model.encoder.hidden_dim)
    assert out["logits"].shape == (B * N, NUM_LABELS)


@skip_if_no_download
def test_loss_runs(model):
    B, N = 2, 3
    logits = torch.randn(B, N, NUM_LABELS, requires_grad=True)
    labels = torch.zeros(B, N, dtype=torch.long)
    mask = torch.ones(B, N, dtype=torch.bool)
    loss = model.compute_loss(
        logits_grouped=logits,
        labels_grouped=labels,
        mask=mask,
        sibling_pairs=None,
        sibling_labels=None,
    )
    assert torch.isfinite(loss["total"])
    loss["total"].backward()  # gradient flows


@skip_if_no_download
def test_decode_runs(model):
    B, N = 1, 4
    logits = torch.randn(B, N, NUM_LABELS)
    mask = torch.ones(B, N, dtype=torch.bool)
    decoded = model.decode(logits, mask)
    assert len(decoded) == 1
    assert len(decoded[0]) == 4


def test_sibling_relation_label_buckets():
    from model import SchemaMapper

    pt = LABEL_TO_ID["prompt_tokens"]
    ct = LABEL_TO_ID["completion_tokens"]
    tcid = LABEL_TO_ID["tool_call_id"]
    content = LABEL_TO_ID["content"]
    finish = LABEL_TO_ID["finish_reason"]

    # Same exclusive group (token_count) → same_kind
    assert SchemaMapper.sibling_relation_label(pt, ct) == 0
    # COOCCUR_BIAS pair (content + finish_reason) → related
    assert SchemaMapper.sibling_relation_label(content, finish) == 1
    # Unrelated
    assert SchemaMapper.sibling_relation_label(pt, tcid) == 2
