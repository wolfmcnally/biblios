"""Deterministic stand-ins for an embedder and a reranker.

They exist so the test suite and the demo script run without any embedding service and
without the network. They have no retrieval quality: a token-hash vector and a lexical
overlap score are stand-ins, not models. A real shelf declares a real embedder to Palace.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from palace.index.config import EMBED_DIM

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class HashEmbedder:
    """Token hashing into ``EMBED_DIM`` dimensions, L2-normalised; records every call."""

    max_concurrency = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.probe_failure: str | None = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * EMBED_DIM
            for token in _tokens(text):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % EMBED_DIM
                vector[index] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors

    def probe(self) -> None:
        if self.probe_failure is not None:
            from palace.index._errors import IndexError as PalaceIndexError

            raise PalaceIndexError(self.probe_failure)

    def close(self) -> None:
        return None


class OverlapReranker:
    """Scores a candidate by the share of query tokens it contains; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls.append((query, len(texts)))
        wanted = set(_tokens(query))
        scores: list[float] = []
        for text in texts:
            present = set(_tokens(text))
            scores.append(len(wanted & present) / len(wanted) if wanted else 0.0)
        return scores

    def probe(self) -> None:
        return None

    def close(self) -> None:
        return None
