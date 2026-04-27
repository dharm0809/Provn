"""MiniLM-L6 encoder for linearised FlatField strings.

Architecture:
  microsoft/MiniLM-L6-H384-uncased (22M params, hidden=384)
  → CLS pooling (DODUO recipe)
  → 384-dim field embedding

Pretrained checkpoint chosen because:
- 22M params, ~22 MB ONNX-int8 → fits 50 MB budget after CRF + tokenizer
- QuaLA-MiniLM (Intel Labs 2022) measured 1.85-10ms ONNX-CPU at seq-len 128
- Same family as our intent baseline-v2 → tooling reuse

The encoder is exposed for two consumers:
- training (`model.py` → forward pass over a batch of FlatField strings)
- export_onnx (`export_onnx.py` → graph export, then dynamic int8 quant)

Runtime gateway code uses the exported ONNX directly + a pure-numpy
CRF Viterbi decode, NOT this PyTorch wrapper.
"""
from __future__ import annotations

from typing import Sequence

import torch
import torch.nn as nn

DEFAULT_ENCODER_MODEL = "microsoft/MiniLM-L12-H384-uncased"
ENCODER_HIDDEN = 384
MAX_SEQ_LEN = 128


class FieldEncoder(nn.Module):
    """MiniLM wrapper that consumes already-tokenized batches and returns
    CLS-pooled embeddings.

    Tokenization is kept outside the nn.Module so the CRF + classifier
    head can be exported jointly with the encoder via a single
    `torch.onnx.export` call against the same `forward()` signature.
    """

    def __init__(self, pretrained_name: str = DEFAULT_ENCODER_MODEL) -> None:
        super().__init__()
        from transformers import AutoModel  # local import to keep CLI fast

        self.backbone = AutoModel.from_pretrained(pretrained_name)
        # MiniLM-L12-H384-uncased — actual hidden dim from config
        self.hidden_dim = self.backbone.config.hidden_size

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        out = self.backbone(input_ids=input_ids, attention_mask=attention_mask, return_dict=True)
        # CLS pooling: first token's last_hidden_state
        return out.last_hidden_state[:, 0, :]


def load_tokenizer(pretrained_name: str = DEFAULT_ENCODER_MODEL):
    """Return the HF AutoTokenizer for the encoder. Stored next to the ONNX
    at deploy time as schema_mapper_tokenizer.json."""
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(pretrained_name)


def tokenize_batch(tokenizer, texts: Sequence[str]) -> dict[str, torch.Tensor]:
    """Tokenize a list of linearised field strings into a padded batch."""
    enc = tokenizer(
        list(texts),
        padding=True,
        truncation=True,
        max_length=MAX_SEQ_LEN,
        return_tensors="pt",
    )
    return {"input_ids": enc["input_ids"], "attention_mask": enc["attention_mask"]}
