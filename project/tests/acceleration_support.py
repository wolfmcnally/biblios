"""A deterministic dense embedder for vector-acceleration proofs, importable by a child
process without importing the proof module itself."""

from __future__ import annotations

import hashlib

from palace.index.config import EMBED_DIM

from biblios.testing import HashEmbedder

QUERY = "lighthouse lens tide"


class DenseEmbedder(HashEmbedder):
    """Deterministic dense vectors seeded by the text, components in [0.5, 1.5) before
    normalising: distinct texts get distinct distances, inside the cache's proved range."""

    def embed(self, texts):
        self.calls.append(tuple(texts))
        vectors = []
        for text in texts:
            seed = hashlib.sha256(text.encode("utf-8")).digest()
            values: list[float] = []
            counter = 0
            while len(values) < EMBED_DIM:
                block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
                values.extend(0.5 + byte / 255 for byte in block)
                counter += 1
            values = values[:EMBED_DIM]
            norm = sum(value * value for value in values) ** 0.5
            vectors.append([value / norm for value in values])
        return vectors
