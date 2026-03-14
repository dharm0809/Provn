"""Unit tests for Merkle tree construction and proof verification."""

from gateway.crypto.merkle_tree import build_merkle_tree, get_inclusion_proof, verify_inclusion_proof, _hash_pair


def test_empty_tree():
    """Empty input returns empty root and levels."""
    root, levels = build_merkle_tree([])
    assert root == ""
    assert levels == []


def test_single_leaf():
    """Single leaf: root equals the leaf."""
    root, levels = build_merkle_tree(["abc123"])
    assert root == "abc123"
    assert len(levels) == 1
    assert levels[0] == ["abc123"]


def test_two_leaves():
    """Two leaves produce a single root."""
    root, levels = build_merkle_tree(["aaa", "bbb"])
    assert len(levels) == 2
    assert levels[0] == ["aaa", "bbb"]
    assert len(levels[1]) == 1
    assert root == _hash_pair("aaa", "bbb")


def test_four_leaves():
    """Four leaves produce 3 levels."""
    leaves = ["a", "b", "c", "d"]
    root, levels = build_merkle_tree(leaves)
    assert len(levels) == 3
    assert levels[0] == leaves
    assert len(levels[1]) == 2  # two intermediate nodes
    assert len(levels[2]) == 1  # root


def test_odd_leaves():
    """Odd number of leaves: last one promoted without duplication."""
    leaves = ["a", "b", "c"]
    root, levels = build_merkle_tree(leaves)
    assert len(levels) == 3
    # Level 1: hash(a,b) and c (promoted)
    assert levels[1][0] == _hash_pair("a", "b")
    assert levels[1][1] == "c"


def test_inclusion_proof_valid():
    """Inclusion proof verifies correctly for all leaves."""
    leaves = ["a", "b", "c", "d"]
    root, levels = build_merkle_tree(leaves)
    for i in range(4):
        proof = get_inclusion_proof(levels, i)
        assert verify_inclusion_proof(leaves[i], proof, root)


def test_inclusion_proof_invalid_leaf():
    """Wrong leaf hash fails verification."""
    leaves = ["a", "b", "c", "d"]
    root, levels = build_merkle_tree(leaves)
    proof = get_inclusion_proof(levels, 0)
    assert not verify_inclusion_proof("wrong", proof, root)


def test_inclusion_proof_invalid_root():
    """Wrong root hash fails verification."""
    leaves = ["a", "b", "c", "d"]
    root, levels = build_merkle_tree(leaves)
    proof = get_inclusion_proof(levels, 0)
    assert not verify_inclusion_proof(leaves[0], proof, "wrong_root")


def test_inclusion_proof_eight_leaves():
    """Proof works for larger tree (8 leaves = 4 levels)."""
    leaves = [f"leaf{i}" for i in range(8)]
    root, levels = build_merkle_tree(leaves)
    assert len(levels) == 4  # 8->4->2->1
    for i in range(8):
        proof = get_inclusion_proof(levels, i)
        assert verify_inclusion_proof(leaves[i], proof, root)
        assert len(proof) == 3  # log2(8) = 3


def test_proof_empty_tree():
    """Proof on empty tree returns empty."""
    proof = get_inclusion_proof([], 0)
    assert proof == []


def test_proof_out_of_bounds():
    """Out-of-bounds index returns empty proof."""
    _, levels = build_merkle_tree(["a", "b"])
    proof = get_inclusion_proof(levels, 5)
    assert proof == []


def test_deterministic():
    """Same leaves always produce same root."""
    leaves = ["x", "y", "z"]
    root1, _ = build_merkle_tree(leaves)
    root2, _ = build_merkle_tree(leaves)
    assert root1 == root2
