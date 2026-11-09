"""A stack: shelves searched together under one embedding identity.

A stack is a declaration the consumer holds over shelves it has opened, never a stored
object. Constructing one reads every member's embedding identity from its Palace store and
refuses, before any query runs, a member of another identity, a member with no declared
selector, or a member named twice. A member whose index lags or is not yet built is
admitted: the search hands Palace exactly the built stores and one query embedder for the
stack's identity, Palace retrieves each store in its own space, fuses the ranks and reranks
the union once, and the answer names every member still catching up rather than claiming
full coverage. Biblios does no fusion or reranking of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from palace import multistore
from palace import search as palace_search
from palace.index import _errors as palace_errors
from palace.index.config import EmbeddingIdentity
from palace.index.embedder import Embedder
from palace.index.embedder_config import embedder_config_path, identity_for, load_embedder_config
from palace.rerank import Reranker, RerankStatus

from biblios.entries import Entry
from biblios.errors import BibliosError, MixedIdentityError, UndeclaredEmbedderError
from biblios.progress import IndexStatus, index_status, indexed_files
from biblios.search import Hit, Page, current_hit
from biblios.shelf import Shelf


@dataclass(frozen=True, slots=True)
class StackResult:
    """The union of a stack search, in Palace's fused-then-reranked order."""

    hits: tuple[Hit, ...]
    rerank_status: RerankStatus
    refused: tuple[tuple[str, str], ...]
    """``(shelf id, entry id)`` pairs whose chunk named no live, current, indexed entry."""
    indexing: tuple[str, ...]
    """Shelf ids whose index has not matched their namespace in full: the answer is what
    their indexes hold now, and an unbuilt member contributed nothing."""
    status: dict[str, IndexStatus]


class Stack:
    """Shelves searched together. Build one with :func:`open_stack`."""

    def __init__(self, shelves: tuple[Shelf, ...], identity: EmbeddingIdentity) -> None:
        self._shelves = shelves
        self._identity = identity
        self._by_id = {shelf.shelf_id: shelf for shelf in shelves}
        self._by_store = {shelf.palace_store.resolve(): shelf for shelf in shelves}

    @property
    def shelves(self) -> tuple[Shelf, ...]:
        return self._shelves

    @property
    def identity(self) -> EmbeddingIdentity:
        """The embedding identity every member shares, as Palace resolves it."""
        return self._identity

    @property
    def shelf_ids(self) -> tuple[str, ...]:
        return tuple(shelf.shelf_id for shelf in self._shelves)

    def shelf(self, shelf_id: str) -> Shelf:
        try:
            return self._by_id[shelf_id]
        except KeyError:
            raise BibliosError(f"no shelf {shelf_id} in this stack") from None

    def status(self) -> dict[str, IndexStatus]:
        return {shelf.shelf_id: index_status(shelf) for shelf in self._shelves}

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        rerank: bool = True,
        strict: bool = False,
    ) -> StackResult:
        """Search the built members through Palace's multi-store search, one rerank over
        the union, naming every member whose index is still catching up."""
        status = self.status()
        indexing = tuple(
            shelf.shelf_id for shelf in self._shelves if status[shelf.shelf_id].lagging
        )
        built = [shelf for shelf in self._shelves if status[shelf.shelf_id].built]
        if not built:
            return StackResult((), "disabled", (), indexing, status)
        found = multistore.search_stores(
            query=query,
            stores=[shelf.palace_store for shelf in built],
            limit=limit,
            rerank_in=max(limit, 20),
            rerank_enabled=rerank,
            strict=strict,
            embedders={self._identity: embedder} if embedder is not None else None,
            reranker=reranker,
        )
        live: dict[str, dict[str, Entry]] = {}
        indexed: dict[str, dict[str, str]] = {}
        for shelf in built:
            live[shelf.shelf_id] = {
                entry.entry_id: entry for entry in shelf.files() if entry.metadata is not None
            }
            indexed[shelf.shelf_id] = indexed_files(shelf)
        hits: list[Hit] = []
        refused: list[tuple[str, str]] = []
        for store_hit in found.hits:
            shelf = self._by_store[Path(store_hit.store).resolve()]
            hit = store_hit.hit
            entry_id = Path(hit.path).stem
            entry = live[shelf.shelf_id].get(entry_id)
            if not current_hit(entry, indexed[shelf.shelf_id]):
                refused.append((shelf.shelf_id, entry_id))
                continue
            assert entry is not None
            try:
                body = palace_search.expand(chunk_id=hit.chunk_id, store=shelf.palace_store).body
            except palace_errors.IndexError:
                if strict:
                    raise
                refused.append((shelf.shelf_id, entry_id))
                continue
            hits.append(
                Hit(
                    shelf_id=shelf.shelf_id,
                    entry_id=entry_id,
                    path=entry.path,
                    heading=hit.heading,
                    chunk=body,
                    rrf_score=hit.rrf_score,
                    bm25_score=hit.bm25_score,
                    vec_distance=hit.vec_distance,
                    rerank_score=hit.rerank_score,
                    rerank_status=found.status,
                )
            )
        return StackResult(
            hits=tuple(hits),
            rerank_status=found.status,
            refused=tuple(refused),
            indexing=indexing,
            status=status,
        )

    def resolve(self, shelf_id: str, entry_id: str) -> Entry:
        return self.shelf(shelf_id).resolve(entry_id)

    def read(self, shelf_id: str, entry_id: str, *, page: int | None = None) -> Page:
        return self.shelf(shelf_id).read(entry_id, page=page)

    def download(self, shelf_id: str, entry_id: str) -> bytes:
        return self.shelf(shelf_id).download(entry_id)


def member_identity(shelf: Shelf) -> EmbeddingIdentity:
    """A shelf's declared identity as Palace resolves it; no selector, no identity."""
    store = shelf.palace_store
    if not embedder_config_path(store).exists():
        raise UndeclaredEmbedderError(f"shelf {shelf.shelf_id} declares no embedder selector")
    return identity_for(load_embedder_config(store))


def open_stack(shelves: Sequence[Shelf]) -> Stack:
    """Declare a stack over opened shelves, refusing before any query what cannot be
    searched as one; a lagging or unbuilt member is admitted and reported at search."""
    members = tuple(shelves)
    if not members:
        raise BibliosError("a stack needs at least one shelf")
    seen_ids: set[str] = set()
    seen_stores: set[Path] = set()
    identities: dict[str, EmbeddingIdentity] = {}
    for shelf in members:
        if shelf.shelf_id in seen_ids or shelf.palace_store.resolve() in seen_stores:
            raise BibliosError(f"shelf {shelf.shelf_id} is named twice in the stack")
        seen_ids.add(shelf.shelf_id)
        seen_stores.add(shelf.palace_store.resolve())
        identities[shelf.shelf_id] = member_identity(shelf)
    distinct = set(identities.values())
    if len(distinct) > 1:
        raise MixedIdentityError(identities)
    return Stack(members, next(iter(distinct)))
