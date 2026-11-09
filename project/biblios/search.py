"""Single-shelf search, and the citation path back to a path, a page and exact bytes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from castle import cas
from drawbridge.mirror import page_sections, parse_mirror_header, sections
from palace import search as palace_search
from palace.index import _errors as palace_errors
from palace.index.embedder import Embedder
from palace.index.embedder_config import embedder_config_path
from palace.rerank import Reranker, RerankStatus

from biblios.entries import Entry
from biblios.errors import BibliosError, UndeclaredEmbedderError
from biblios.progress import index_status, indexed_files
from biblios.shelf import Shelf


@dataclass(frozen=True, slots=True)
class Hit:
    shelf_id: str
    entry_id: str
    path: str
    heading: str | None
    chunk: str
    rrf_score: float
    bm25_score: float | None
    vec_distance: float | None
    rerank_score: float | None
    rerank_status: RerankStatus


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[Hit, ...]
    rerank_status: RerankStatus
    refused: tuple[str, ...]
    """Entry ids whose chunks were retrieved but are not live and current."""
    revision: int
    searchable_revision: int
    indexing: bool
    """The index has not matched the namespace in full since the last change; the hits
    are what the index holds now, not a complete answer."""
    """Entry ids of chunks that named no live entry; they are never returned as hits."""


@dataclass(frozen=True, slots=True)
class Page:
    """A mirror page: a ``## Page N`` section where the mirror has them, else a ``##`` section."""

    entry: Entry
    page: int | None
    text: str
    page_count: int


def search(
    shelf: Shelf,
    query: str,
    *,
    limit: int = 5,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    rerank: bool = True,
    strict: bool = False,
) -> SearchResult:
    store = shelf.palace_store
    if not embedder_config_path(store).exists():
        raise UndeclaredEmbedderError(f"no embedder selector declared at {store}")
    status = index_status(shelf)
    revision, searchable_revision, indexing = (
        status.revision,
        status.searchable_revision,
        status.lagging,
    )
    if not status.built:
        return SearchResult((), "disabled", (), revision, searchable_revision, indexing)
    if rerank:
        ranked = palace_search.search_reranked(
            query=query,
            store=store,
            limit=limit,
            rerank_in=max(limit, 20),
            embedder=embedder,
            reranker=reranker,
            strict=strict,
        )
        hits, status = ranked.hits, ranked.status
    else:
        hits = palace_search.search(query=query, store=store, limit=limit, embedder=embedder)
        status = "disabled"
    live = {entry.entry_id: entry for entry in shelf.files() if entry.metadata is not None}
    indexed = indexed_files(shelf)
    results: list[Hit] = []
    refused: list[str] = []
    for hit in hits:
        entry_id = Path(hit.path).stem
        entry = live.get(entry_id)
        if not current_hit(entry, indexed):
            refused.append(entry_id)
            continue
        assert entry is not None
        try:
            body = palace_search.expand(chunk_id=hit.chunk_id, store=store).body
        except palace_errors.IndexError:
            if strict:
                raise
            refused.append(entry_id)
            continue
        results.append(
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
                rerank_status=status,
            )
        )
    return SearchResult(
        hits=tuple(results),
        rerank_status=status,
        refused=tuple(refused),
        revision=revision,
        searchable_revision=searchable_revision,
        indexing=indexing,
    )


def current_hit(entry: Entry | None, indexed: dict[str, str]) -> bool:
    """A chunk is answered only for a live entry whose recorded conversion is current and
    whose mirror Palace holds at that very digest; a replacement whose new chunks are not
    yet committed is refused rather than answered with the old ones."""
    return (
        entry is not None
        and entry.searchable
        and entry.metadata is not None
        and indexed.get(entry.entry_id) == entry.metadata.mirror_sha256
    )


def read(shelf: Shelf, entry_id: str, *, page: int | None = None) -> Page:
    entry = shelf.resolve(entry_id)
    if entry.metadata is None or not entry.metadata.converted:
        raise BibliosError(f"entry {entry_id} has no mirror")
    _header, body = parse_mirror_header(shelf.mirror_path(entry_id))
    return _page(entry, body, page=page)


def _page(entry: Entry, body: str, *, page: int | None) -> Page:
    pages = page_sections(body) or [text for _heading, text in sections(body)]
    if page is None:
        return Page(entry=entry, page=None, text=body, page_count=len(pages))
    if not 1 <= page <= len(pages):
        raise BibliosError(f"entry {entry.entry_id} has no page {page}")
    return Page(entry=entry, page=page, text=pages[page - 1], page_count=len(pages))


def download(shelf: Shelf, entry_id: str) -> bytes:
    entry = shelf.resolve(entry_id)
    if entry.cas_id is None:
        raise BibliosError(f"entry {entry_id} is not a file")
    return cas.require_object(shelf.castle_root, entry.cas_id).read_bytes()
