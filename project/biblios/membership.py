"""Per-chunk membership in an accepted stage: an RFC 6962-shaped Merkle tree.

A stage signs ``chunk_root_sha256`` over its ordered rows and its ``row_count``; a served
hit carries its row and the audit path from that row's leaf to the root, so a caller who
verified the stage can prove the one row belongs to it, at its position, in O(log n),
without the entry's other rows. The tree binds a row to a root, never to an entry: the
entry, source and mirror are bound by the stage that signs the root.

Domain separation: a leaf is SHA-256 over ``0x00`` and the canonical encoding of
``["biblios.chunk-leaf/1", position, chunk_id, body_sha256, input_sha256, vector_sha256]``;
an interior node is SHA-256 over ``0x01``, its left child and its right child. The split
is RFC 6962's (the largest power of two below the size), with no duplicated odd node.
Nodes are named by the half-open range ``[lo, hi)`` of leaves they cover, so a tree of n
leaves has exactly 2n - 1 distinct nodes and every path has at most ceil(log2 n) nodes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from castle.namespace import canonical_bytes

LEAF_TAG = "biblios.chunk-leaf/1"
MAX_ROWS = 16384
"""The most rows a stage may carry (the policy's ``max_chunks`` bound): paths have at
most 14 nodes."""


def _digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def leaf_hash(
    position: int, chunk_id: str, body_sha256: str, input_sha256: str, vector_sha256: str
) -> bytes:
    encoded = canonical_bytes(
        [LEAF_TAG, position, chunk_id, body_sha256, input_sha256, vector_sha256]
    )
    return hashlib.sha256(b"\x00" + encoded).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """The largest power of two strictly below ``n`` (n >= 2)."""
    return 1 << ((n - 1).bit_length() - 1)


def tree_nodes(leaves: Sequence[bytes]) -> dict[tuple[int, int], bytes]:
    """Every node of the tree over ``leaves``, by its leaf range; O(n) hashes."""
    if not leaves:
        raise ValueError("an empty tree has no nodes")
    nodes: dict[tuple[int, int], bytes] = {}

    def build(lo: int, hi: int) -> bytes:
        if hi - lo == 1:
            value = leaves[lo]
        else:
            mid = lo + _split(hi - lo)
            value = node_hash(build(lo, mid), build(mid, hi))
        nodes[(lo, hi)] = value
        return value

    build(0, len(leaves))
    return nodes


def root_of(nodes: dict[tuple[int, int], bytes], count: int) -> bytes:
    return nodes[(0, count)]


def path_ranges(position: int, count: int) -> list[tuple[int, int]]:
    """The leaf ranges of the audit path for ``position`` in a tree of ``count`` leaves,
    nearest the leaf first; a pure function of the two, so the path can be fetched by key."""
    if not 0 <= position < count:
        raise ValueError("position outside the tree")
    ranges: list[tuple[int, int]] = []
    lo, hi = 0, count
    while hi - lo > 1:
        mid = lo + _split(hi - lo)
        if position < mid:
            ranges.append((mid, hi))
            hi = mid
        else:
            ranges.append((lo, mid))
            lo = mid
    ranges.reverse()
    return ranges


def root_from_path(leaf: bytes, position: int, count: int, path: Sequence[bytes]) -> bytes | None:
    """The root ``leaf`` and ``path`` reach at ``position`` of ``count``; ``None`` when the
    path has the wrong length for that shape."""
    if not 0 <= position < count or len(path) != len(path_ranges(position, count)):
        return None
    value = leaf
    for (lo, hi), sibling in zip(path_ranges(position, count), path, strict=True):
        value = node_hash(sibling, value) if hi <= position else node_hash(value, sibling)
    return value


def verify(
    *,
    position: int,
    chunk_id: str,
    body_sha256: str,
    input_sha256: str,
    vector_sha256: str,
    count: int,
    path: Iterable[str],
    root_sha256: str,
) -> bool:
    """Whether one row is the ``position``-th of ``count`` rows under ``root_sha256``.

    For a caller: take ``count`` and ``root_sha256`` from a stage whose canonical bytes
    it has verified as its own accepted record for the hit's entry, never from the hit.
    Any malformed input (a non-int or out-of-range position or count, a digest that is not
    64 lowercase hex characters, a path of the wrong length) is ``False``, never an
    exception."""
    if (
        type(position) is not int
        or type(count) is not int
        or not 0 < count <= MAX_ROWS
        or not 0 <= position < count
        or type(chunk_id) is not str
        or not all(_digest(value) for value in (body_sha256, input_sha256, vector_sha256))
        or not _digest(root_sha256)
    ):
        return False
    try:
        items = list(path)
    except TypeError:
        return False
    if len(items) != len(path_ranges(position, count)) or not all(map(_digest, items)):
        return False
    siblings = [bytes.fromhex(item) for item in items]
    leaf = leaf_hash(position, chunk_id, body_sha256, input_sha256, vector_sha256)
    reached = root_from_path(leaf, position, count, siblings)
    return reached is not None and reached.hex() == root_sha256
