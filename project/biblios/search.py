"""Single-shelf search, and the citation path back to a path, a page and exact bytes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from castle import cas
from drawbridge.mirror import page_sections, parse_mirror_header, sections
from palace import search as palace_search
from palace.index.embedder import Embedder
from palace.index.embedder_config import embedder_config_path
from palace.rerank import Reranker, RerankStatus

from biblios.entries import Entry
from biblios.errors import BibliosError, UndeclaredEmbedderError
from biblios.progress import index_status
from biblios.shelf import Shelf

if TYPE_CHECKING:
    from biblios.core import ReadObservation


@dataclass(frozen=True, slots=True)
class ManifestRow:
    """The accepted row of one served chunk and its membership proof.

    ``path`` is the audit path (hex SHA-256 nodes, nearest the leaf first) from this row's
    leaf to the ``chunk_root_sha256`` its stage signs; :func:`biblios.membership.verify`
    checks it against ``row_count`` and that root taken from the caller's own verified
    copy of the stage, never from the hit: membership binds a row to a root and a count,
    and only that stage binds them to the entry."""

    chunk_id: str
    position: int
    body_sha256: str
    input_sha256: str
    vector_sha256: str
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServedStage:
    """The accepted stage an answered entry was served from, with its exact canonical
    bytes. Biblios checked only consistency (the bytes hash to ``stage_sha256``, the
    accepted manifest hashes to the digest they carry, the mirror is exactly the stage's);
    authentication is the caller's check of ``stage_sha256`` against its own record."""

    shelf_id: str
    entry_id: str
    stage_sha256: str
    mirror_sha256: str
    content_sha256: str
    canonical: bytes


@dataclass(frozen=True, slots=True)
class Withheld:
    """An entry whose retrieved hits were withheld, why, and how many."""

    shelf_id: str
    entry_id: str
    category: str
    count: int


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
    stage_sha256: str
    mirror_sha256: str
    content_sha256: str
    manifest: ManifestRow


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[Hit, ...]
    rerank_status: RerankStatus
    withheld: tuple[Withheld, ...]
    """Entries whose chunks were retrieved but not served, each with its category."""
    stages: tuple[ServedStage, ...]
    """One per answered entry: the accepted stage its hits came from."""
    revision: int
    searchable_revision: int
    indexing: bool
    """The index has not matched the namespace in full since the last change; the hits
    are what the index holds now, not a complete answer."""
    observations: tuple[ReadObservation, ...] = ()
    """Withheld entries whose served rows or mirror disagreed with their accepted stage,
    for a write-grade call to verify (:class:`biblios.core.ReadObservation`). A search
    persists nothing; a writable binding's search also marks these entries."""


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
    status = index_status(shelf)
    revision, searchable_revision, indexing = (
        status.revision,
        status.searchable_revision,
        status.lagging,
    )
    if not status.built:
        return SearchResult((), "disabled", (), (), revision, searchable_revision, indexing)
    # One prepared search with per-hit evidence: each returned hit carries the exact
    # stored body, embedding-input digest and vector bytes read in the retrieval's own
    # snapshot, which the serving check compares with the entry's accepted manifest.
    prepared = palace_search.prepare_search(
        query=query,
        store=store,
        limit=limit,
        rerank_in=max(limit, 20),
        rerank_enabled=rerank,
        strict=strict,
        embedder=embedder,
        injected_reranker=reranker is not None,
        evidence=True,
    )
    ranked = palace_search.execute_prepared_search(prepared, reranker=reranker)
    status = ranked.status
    evidence = ranked.evidence or ()
    served = _ServingCheck(shelf)
    try:
        results = [
            served.hit(hit, status, evidence[index] if index < len(evidence) else None)
            for index, hit in enumerate(ranked.hits)
        ]
    finally:
        served.close()
    return SearchResult(
        hits=tuple(hit for hit in results if hit is not None),
        rerank_status=status,
        withheld=served.withheld(),
        stages=served.stages(),
        revision=revision,
        searchable_revision=searchable_revision,
        indexing=indexing,
        observations=served.observations(),
    )


OBSERVED = {
    "mirror-changed": "mirror-changed",
    "body-mismatch": "rows-mismatch",
    "proof-invalid": "rows-mismatch",
    "chunk-unaccepted": "missing-rows",
    "proof-missing": "missing-rows",
    "store-unreadable": "missing-rows",
    "input-mismatch": "rows-mismatch",
    "vector-mismatch": "rows-mismatch",
}
"""The withholding categories a write-grade call can verify, by observation category:
a mirror that is no longer the accepted bytes; a served body or an accepted per-hit row
or node that disagrees with the signed stage; and a served chunk with no accepted row,
node or readable Palace row. Record categories (not live, not accepted, a commit in
flight or during the read, an inconsistent stage, no derived store) are not reported."""


def path_statement(nodes: int) -> str:
    """The one keyed read of a hit's audit path: a primary-key seek per node (entry, lo,
    hi), so its cost depends on the path length, never on the entry's size."""
    keys = ", ".join("(?, ?)" for _ in range(nodes))
    return (
        f"SELECT lo, hi, hash FROM accepted_nodes WHERE entry_id=? AND (lo, hi) IN (VALUES {keys})"
    )


_EVIDENCE_STATUS = {
    "missing-local-row": "store-unreadable",
    "body-hash-mismatch": "body-mismatch",
    "missing-vector": "vector-mismatch",
    "malformed-vector": "vector-mismatch",
    "missing-input-digest": "input-mismatch",
}
"""Palace's per-hit statuses other than ``verified``, as withholding categories; any
status not named (a remote vector leg) withholds as ``evidence-missing``."""


def _evidence_refusal(evidence, entry_id: str, chunk_id: str, row) -> str | None:
    """Step 5: the withholding category for one hit's evidence, or ``None`` when its
    exact stored body, input digest and vector bytes match the accepted row."""
    import hashlib

    if evidence is None or evidence.chunk_id != chunk_id:
        return "evidence-missing"
    if evidence.status != "verified":
        return _EVIDENCE_STATUS.get(evidence.status, "evidence-missing")
    if evidence.path is None or Path(evidence.path).stem != entry_id:
        return "proof-invalid"
    if (
        not isinstance(evidence.body, str)
        or hashlib.sha256(evidence.body.encode("utf-8")).hexdigest() != row.body_sha256
    ):
        return "body-mismatch"
    if evidence.input_sha256 != row.input_sha256:
        return "input-mismatch"
    if (
        not isinstance(evidence.vector_f32, bytes)
        or hashlib.sha256(evidence.vector_f32).hexdigest() != row.vector_sha256
    ):
        return "vector-mismatch"
    return None


class _ServingCheck:
    """The interim per-hit check, in order, per query (plan decision 10).

    Per entry, once per call: (1) liveness and path from one validated Castle read of the
    entry and its parent chain (bounded by the hits, never by the shelf); (2) the
    entry's accepted record with an even ``served_seq``; (3) the accepted stage's exact
    canonical bytes hashing to the record's stage digest, naming this entry, its source
    and its mirror digest, and signing a membership root (a ``biblios.stage/1`` stage
    signs none: ``proof-missing``); and the mirror's exact bytes hashing to the stage's
    mirror digest (always hashed: a stat identity never decides what is served). Per hit:
    (4) the chunk's accepted row, read by key, and its audit path, read by key in one
    statement (O(log n) rows), reaching the signed root at the row's position of the
    signed count (``proof-missing`` when a node is absent, ``proof-invalid`` when the path
    does not reach it); (5) Palace's per-hit evidence, read in the retrieval's own
    snapshot (``palace.prepared.HitEvidence``), ``verified`` for this chunk of this entry,
    with its exact stored body hashing to the row's body hash, its committed
    embedding-input digest equal to the row's, and its exact stored vector bytes hashing
    to the row's vector hash; (6) ``served_seq`` re-read unchanged. Any failure withholds
    that entry's hits, reported with its category. Unverified: heading, scores and
    snippet. Residuals: a chunk omitted from the results and tampering with the keyword
    index alone are not detected per query, and reranking runs before this check.

    Granularity: a failure of steps 1-3 (including ``mirror-changed``) withholds every hit
    of the entry; a failure of steps 4-6 withholds only that hit, so the entry's other
    individually proven hits are still served. A served-row or mirror failure is also
    reported as a :class:`~biblios.core.ReadObservation`, a hint for a write-grade call
    to verify, never acted on by a reader.
    """

    def __init__(self, shelf: Shelf) -> None:
        from biblios.derived import DERIVED_NAME, DerivedStore

        self.shelf = shelf
        self.store = (
            DerivedStore(shelf.palace_store, read_only=True)
            if (shelf.palace_store / DERIVED_NAME).is_file()
            else None
        )
        self._verified: dict[str, tuple | str] = {}
        self._withheld: dict[str, list] = {}
        self._served: dict[str, ServedStage] = {}
        self._seq: dict[str, int] = {}
        """Each verified entry's even ``served_seq``, which an observation names."""

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    def withheld(self) -> tuple[Withheld, ...]:
        return tuple(
            Withheld(self.shelf.shelf_id, entry_id, category, count)
            for entry_id, (category, count) in sorted(self._withheld.items())
        )

    def observations(self) -> tuple[ReadObservation, ...]:
        """One per withheld entry whose category is a served-row or mirror mismatch."""
        from biblios.core import ReadObservation

        return tuple(
            ReadObservation(entry_id, OBSERVED[category], self._seq[entry_id])
            for entry_id, (category, _count) in sorted(self._withheld.items())
            if category in OBSERVED and entry_id in self._seq
        )

    def stages(self) -> tuple[ServedStage, ...]:
        return tuple(self._served[key] for key in sorted(self._served))

    def _withhold(self, entry_id: str, category: str) -> None:
        held = self._withheld.setdefault(entry_id, [category, 0])
        held[1] += 1

    def _entry(self, entry_id: str) -> Entry | None:
        from biblios.entries import view

        # One validated read of the hit's entry and its parent chain: bounded by the
        # hits, never by the shelf.
        with self.shelf._handle.mutex:
            record = self.shelf._entry(entry_id)
            if record is None or record.kind != "file":
                return None
            return view(record, self.shelf._chain(record), self.shelf._root_id)

    def _verify(self, entry_id: str) -> tuple | str:
        """Steps 1-3 for one entry: the served identity, or the withholding category."""
        from biblios.control import ConsumerError
        from biblios.core import _parse_stage
        from biblios.mirrors import MirrorInvalid, digest, read_mirror

        if self.store is None:
            return "derived-store-missing"
        entry = self._entry(entry_id)
        if entry is None:
            return "not-live"
        record = self.store.record(entry_id)
        if (
            record is None
            or record.state != "accepted"
            or not record.converted
            or record.accepted_stage_sha256 is None
            or record.source_cas_id != entry.cas_id
        ):
            return "not-accepted"
        if record.served_seq % 2:
            return "commit-in-flight"
        self._seq[entry_id] = record.served_seq
        row = self.store.conn.execute(
            "SELECT stage_sha256, canonical FROM accepted_stages WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if row is None or row[0] != record.accepted_stage_sha256:
            return "stage-inconsistent"
        canonical = bytes(row[1])
        try:
            stage = _parse_stage(canonical, row[0])
        except ConsumerError:
            return "stage-inconsistent"
        planned = stage.entry
        if (
            planned.entry_id != entry_id
            or planned.action != "publish"
            or planned.source_cas_id != entry.cas_id
            or planned.mirror_sha256 != record.mirror_sha256
        ):
            return "stage-inconsistent"
        if planned.chunk_root_sha256 is None:
            return "proof-missing"
        try:
            data = read_mirror(self.shelf.mirror_path(entry_id), self.shelf.mirror_root)
        except MirrorInvalid:
            # An unsafe or unreadable mirror withholds only its entry; any other
            # failure is not an availability decision and propagates with its trace.
            data = None
        if data is None or digest(data) != planned.mirror_sha256:
            return "mirror-changed"
        return entry, record, stage

    def _row(self, entry_id: str, chunk_id: str, stage) -> ManifestRow | str:
        """The hit's accepted row with its audit path, or the withholding category."""
        from biblios import membership

        found = self.store.conn.execute(
            "SELECT position, body_sha256, input_sha256, vector_sha256 FROM accepted_chunks "
            "WHERE entry_id=? AND chunk_id=?",
            (entry_id, chunk_id),
        ).fetchone()
        if found is None:
            return "chunk-unaccepted"
        position, body, inputs, vector = found
        count = stage.entry.row_count
        # Stored values are data, not trusted types: anything malformed withholds this
        # entry as an invalid proof and never aborts the rest of the answer.
        if (
            type(position) is not int
            or type(count) is not int
            or not 0 < count <= membership.MAX_ROWS
            or not 0 <= position < count
        ):
            return "proof-invalid"
        ranges = membership.path_ranges(position, count)
        nodes: dict[tuple[int, int], object] = {}
        if ranges:
            for lo, hi, value in self.store.conn.execute(
                path_statement(len(ranges)),
                (entry_id, *(bound for pair in ranges for bound in pair)),
            ):
                nodes[(lo, hi)] = value
        if any(key not in nodes for key in ranges):
            return "proof-missing"
        if any(type(nodes[key]) is not bytes or len(nodes[key]) != 32 for key in ranges):
            return "proof-invalid"
        path = tuple(nodes[key].hex() for key in ranges)
        if not membership.verify(
            position=position,
            chunk_id=chunk_id,
            body_sha256=body,
            input_sha256=inputs,
            vector_sha256=vector,
            count=count,
            path=path,
            root_sha256=str(stage.entry.chunk_root_sha256),
        ):
            return "proof-invalid"
        return ManifestRow(chunk_id, position, body, inputs, vector, path)

    def hit(self, hit, status: RerankStatus, evidence) -> Hit | None:
        entry_id = Path(hit.path).stem
        if entry_id not in self._verified:
            self._verified[entry_id] = self._verify(entry_id)
        verified = self._verified[entry_id]
        if isinstance(verified, str):
            self._withhold(entry_id, verified)
            return None
        entry, record, stage = verified
        row = self._row(entry_id, hit.chunk_id, stage)
        if isinstance(row, str):
            self._withhold(entry_id, row)
            return None
        refused = _evidence_refusal(evidence, entry_id, hit.chunk_id, row)
        if refused is not None:
            self._withhold(entry_id, refused)
            return None
        body = evidence.body
        again = self.store.record(entry_id)
        if again is None or again.served_seq != record.served_seq:
            self._withhold(entry_id, "commit-during-read")
            return None
        self._served.setdefault(
            entry_id,
            ServedStage(
                self.shelf.shelf_id,
                entry_id,
                stage.stage_sha256,
                stage.entry.mirror_sha256,
                str(stage.entry.content_sha256),
                stage.canonical,
            ),
        )
        return Hit(
            shelf_id=self.shelf.shelf_id,
            entry_id=entry_id,
            path=entry.path,
            heading=hit.heading,
            chunk=body,
            rrf_score=hit.rrf_score,
            bm25_score=hit.bm25_score,
            vec_distance=hit.vec_distance,
            rerank_score=hit.rerank_score,
            rerank_status=status,
            stage_sha256=stage.stage_sha256,
            mirror_sha256=stage.entry.mirror_sha256,
            content_sha256=str(stage.entry.content_sha256),
            manifest=row,
        )


def readable_mirror(shelf: Shelf, entry: Entry) -> tuple[str, str]:
    """Classify the entry's mirror from one read and return ``(text, state)``.

    Searchable and validated pending-reindex bytes are readable; every other state
    refuses with its class. The returned text is exactly the bytes classified.
    """
    from biblios.derived import DERIVED_NAME, DerivedStore
    from biblios.mirrors import (
        PENDING_REINDEX,
        SEARCHABLE,
        MirrorInvalid,
        lane_for,
        read_mirror,
        validate_mirror,
    )
    from biblios.mirrors import digest as sha256

    record = None
    if (shelf.palace_store / DERIVED_NAME).is_file():
        store = DerivedStore(shelf.palace_store, read_only=True)
        try:
            record = store.record(entry.entry_id)
        finally:
            store.close()
    if record is None or not record.converted:
        raise MirrorUnavailable("mirror-unavailable", entry.entry_id)
    if record.source_cas_id != entry.cas_id:
        raise MirrorUnavailable("pending-conversion", entry.entry_id)
    lane = lane_for(shelf.palace_store)
    try:
        data = read_mirror(shelf.mirror_path(entry.entry_id), shelf.mirror_root)
        if data is None:
            raise MirrorInvalid("mirror-missing")
        found = sha256(data)
        if found == record.mirror_sha256:
            return data.decode("utf-8"), SEARCHABLE
        text = validate_mirror(data, lane=lane)
        refused = record.refused_for(found)
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
