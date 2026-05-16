"""MiniLM-L12 encoder for linearised FlatField strings.

Architecture:
  sentence-transformers/all-MiniLM-L12-v2 (33M params, hidden=384)
  → mean pooling over attention-masked tokens
  → 384-dim field embedding

We deliberately do NOT use microsoft/MiniLM-L12-H384-uncased: that's the
*base* model, only pretrained with masked LM. Its CLS-token pooling is
essentially uniform (cosine sim ~0.99 across totally unrelated text)
because CLS only learns useful sentence semantics during sentence-level
fine-tuning. With our 336-sample gold-only training set the encoder
can't be adapted in-place, so we need an encoder that already produces
differentiated sentence vectors.

sentence-transformers/all-MiniLM-L12-v2 is the same architecture, same
tokenizer (vocab 30522, identical special tokens), but fine-tuned on
1B+ sentence pairs with contrastive learning + mean pooling. Drop-in
compatible with the rest of the pipeline (tokenizer, hidden dim,
ONNX export shape).

Runtime gateway code uses the exported ONNX directly + a pure-numpy
CRF Viterbi decode, NOT this PyTorch wrapper.
"""
from __future__ import annotations

from typing import Sequence

import torch
import torch.nn as nn

DEFAULT_ENCODER_MODEL = "sentence-transformers/all-MiniLM-L12-v2"
ENCODER_HIDDEN = 384
MAX_SEQ_LEN = 128


class FieldEncoder(nn.Module):
    """MiniLM wrapper that consumes already-tokenized batches and returns
    mean-pooled embeddings (attention-masked).

    Tokenization is kept outside the nn.Module so the CRF + classifier
    head can be exported jointly with the encoder via a single
    `torch.onnx.export` call against the same `forward()` signature.
    """

    def __init__(self, pretrained_name: str = DEFAULT_ENCODER_MODEL) -> None:
        super().__init__()
        from transformers import AutoModel  # local import to keep CLI fast

        self.backbone = AutoModel.from_pretrained(pretrained_name)
        self.hidden_dim = self.backbone.config.hidden_size

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        out = self.backbone(input_ids=input_ids, attention_mask=attention_mask, return_dict=True)
        token_emb = out.last_hidden_state
        mask = attention_mask.unsqueeze(-1).float()
        return (token_emb * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)


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
