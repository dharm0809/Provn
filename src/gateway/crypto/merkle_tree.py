"""Merkle tree construction and inclusion proof generation.

Uses SHA3-512 to be consistent with the session chain hash algorithm.
"""

from __future__ import annotations

import logging
from hashlib import sha3_512

logger = logging.getLogger(__name__)


def _hash_pair(left: str, right: str) -> str:
    """Hash two child hashes to produce parent hash."""
    combined = (left + right).encode("utf-8")
    return sha3_512(combined).hexdigest()


def build_merkle_tree(leaves: list[str]) -> tuple[str, list[list[str]]]:
    """Build a Merkle tree from leaf hashes.

    Args:
        leaves: List of hex-encoded hash strings (SHA3-512).

    Returns:
        (root_hash, tree_levels) where tree_levels[0] = leaves, tree_levels[-1] = [root].
        Returns ("", []) for empty input.
    """
    if not leaves:
        return "", []

    # Level 0 = leaves
    levels: list[list[str]] = [list(leaves)]
    current = list(leaves)

    while len(current) > 1:
        next_level: list[str] = []
        for i in range(0, len(current), 2):
            if i + 1 < len(current):
                parent = _hash_pair(current[i], current[i + 1])
            else:
                # Odd node: promote as-is (no duplicate)
                parent = current[i]
            next_level.append(parent)
        levels.append(next_level)
        current = next_level

    root = current[0]
    return root, levels


def get_inclusion_proof(tree_levels: list[list[str]], leaf_index: int) -> list[dict[str, str]]:
    """Generate an inclusion proof (audit path) for a leaf.

    Args:
        tree_levels: Output from build_merkle_tree.
        leaf_index: Index of the leaf to prove.

    Returns:
        List of proof steps: [{"hash": sibling_hash, "position": "left"|"right"}, ...]
        Each step provides the sibling needed to recompute the parent.
    """
    if not tree_levels or leaf_index >= len(tree_levels[0]):
        return []

    proof: list[dict[str, str]] = []
    idx = leaf_index

    for level in tree_levels[:-1]:  # Skip root level
        if idx % 2 == 0:
            # Current is left child, sibling is right
            if idx + 1 < len(level):
                proof.append({"hash": level[idx + 1], "position": "right"})
        else:
            # Current is right child, sibling is left
            proof.append({"hash": level[idx - 1], "position": "left"})
        idx //= 2

    return proof


def verify_inclusion_proof(leaf_hash: str, proof: list[dict[str, str]], root_hash: str) -> bool:
    """Verify that a leaf is included in a Merkle tree given the proof and root.

    Args:
        leaf_hash: The hash of the leaf to verify.
        proof: The inclusion proof from get_inclusion_proof.
        root_hash: The expected root hash.

    Returns:
        True if the proof is valid.
    """
    current = leaf_hash
    for step in proof:
        if step["position"] == "left":
            current = _hash_pair(step["hash"], current)
        else:
            current = _hash_pair(current, step["hash"])
    return current == root_hash
