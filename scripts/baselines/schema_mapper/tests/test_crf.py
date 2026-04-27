"""Tests for FieldCRF — covers exclusive-group enforcement and parameter
export shape stability."""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from canonical_schema import LABEL_TO_ID  # noqa: E402
from crf_head import NUM_LABELS, FieldCRF  # noqa: E402


def test_crf_initialises_and_decodes():
    torch.manual_seed(0)
    crf = FieldCRF()
    logits = torch.randn(2, 4, NUM_LABELS)
    mask = torch.ones(2, 4, dtype=torch.bool)
    decoded = crf.decode(logits, mask)
    assert len(decoded) == 2
    assert all(len(seq) == 4 for seq in decoded)
    for seq in decoded:
        for lab in seq:
            assert 0 <= lab < NUM_LABELS


def test_crf_blocks_two_prompt_tokens_in_same_parent():
    """token_count exclusive group: two siblings shouldn't both be prompt_tokens."""
    crf = FieldCRF()
    pt = LABEL_TO_ID["prompt_tokens"]
    unk = LABEL_TO_ID["UNKNOWN"]
    logits = torch.full((1, 4, NUM_LABELS), -50.0)
    logits[0, 0, pt] = 10.0       # strongest
    logits[0, 1, pt] = 9.0        # second-best — should be demoted
    logits[0, 2, unk] = 5.0
    logits[0, 3, unk] = 5.0
    mask = torch.ones(1, 4, dtype=torch.bool)
    decoded = crf.decode(logits, mask)[0]
    pt_positions = [i for i, l in enumerate(decoded) if l == pt]
    assert len(pt_positions) <= 1


def test_crf_export_params_shapes():
    crf = FieldCRF()
    params = crf.export_params()
    assert params["transitions"].shape == (NUM_LABELS, NUM_LABELS)
    assert params["start_transitions"].shape == (NUM_LABELS,)
    assert params["end_transitions"].shape == (NUM_LABELS,)
    assert len(params["labels"]) == NUM_LABELS


def test_crf_nll_is_finite():
    crf = FieldCRF()
    logits = torch.randn(2, 3, NUM_LABELS)
    labels = torch.zeros(2, 3, dtype=torch.long)
    mask = torch.ones(2, 3, dtype=torch.bool)
    nll = crf(logits, labels, mask)
    assert torch.isfinite(nll)
