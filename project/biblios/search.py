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
    mirror_state: str = "searchable"
    """``searchable``, or ``pending-reindex`` for validated bytes not yet indexed."""


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
    from biblios.mirrors import classify_shelf

    states = classify_shelf(shelf)
    status = index_status(shelf, states)
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
        if not current_hit(entry, indexed, states):
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


def current_hit(entry: Entry | None, indexed: dict[str, str], states: dict) -> bool:
    """A chunk is answered only for a live entry whose recorded conversion is current, whose
    mirror file holds exactly the recorded bytes, and whose mirror Palace holds at that very
    digest. A replacement, an edited mirror not yet re-indexed, or an isolated mirror is
    refused rather than answered with old rows."""
    state = states.get(entry.entry_id) if entry is not None else None
    return (
        entry is not None
        and entry.conversion_current
        and entry.metadata is not None
        and state is not None
        and state.searchable
        and indexed.get(entry.entry_id) == entry.metadata.mirror_sha256
    )


def readable_mirror(shelf: Shelf, entry: Entry) -> tuple[str, str]:
    """Classify the entry's mirror from one read and return ``(text, state)``.

    Searchable and validated pending-reindex bytes are readable; every other state
    refuses with its class. The returned text is exactly the bytes classified.
    """
    from biblios.mirrors import (
        PENDING_REINDEX,
        SEARCHABLE,
        MirrorInvalid,
        lane_for,
        read_mirror,
        refusal_for,
        validate_mirror,
    )
    from biblios.mirrors import digest as sha256

    if entry.metadata is None or not entry.metadata.converted:
        raise MirrorUnavailable("mirror-unavailable", entry.entry_id)
    if not entry.metadata.current_for(entry.cas_id):
        raise MirrorUnavailable("pending-conversion", entry.entry_id)
    lane = lane_for(shelf.palace_store)
    try:
        data = read_mirror(shelf.mirror_path(entry.entry_id), shelf.mirror_root)
        if data is None:
            raise MirrorInvalid("mirror-missing")
        found = sha256(data)
        if found == entry.metadata.mirror_sha256:
            return data.decode("utf-8"), SEARCHABLE
        text = validate_mirror(data, lane=lane)
        refused = refusal_for(shelf.palace_store, entry.entry_id, found)
        if refused is not None:
            raise MirrorInvalid(refused)
        return text, PENDING_REINDEX
    except MirrorInvalid as error:
        raise MirrorUnavailable(error.code, entry.entry_id) from None
    except UnicodeDecodeError:
        raise MirrorUnavailable("mirror-unicode-invalid", entry.entry_id) from None


class MirrorUnavailable(BibliosError):
    """An entry's mirror cannot be read in its current state; ``code`` is the class."""

    def __init__(self, code: str, entry_id: str) -> None:
        super().__init__(f"entry {entry_id} mirror unavailable: {code}")
        self.code = code


def read(shelf: Shelf, entry_id: str, *, page: int | None = None) -> Page:
    entry = shelf.resolve(entry_id)
    text, state = readable_mirror(shelf, entry)
    _header, body = parse_mirror_header(text)
    return _page(entry, body, page=page, mirror_state=state)


def _page(entry: Entry, body: str, *, page: int | None, mirror_state: str = "searchable") -> Page:
    pages = page_sections(body) or [text for _heading, text in sections(body)]
    if page is None:
        return Page(entry, None, body, len(pages), mirror_state)
    if not 1 <= page <= len(pages):
        raise BibliosError(f"entry {entry.entry_id} has no page {page}")
    return Page(entry, page, pages[page - 1], len(pages), mirror_state)


def download(shelf: Shelf, entry_id: str) -> bytes:
    entry = shelf.resolve(entry_id)
    if entry.cas_id is None:
        raise BibliosError(f"entry {entry_id} is not a file")
    return cas.require_object(shelf.castle_root, entry.cas_id).read_bytes()
