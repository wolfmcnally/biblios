"""Bring the mirrors and the Palace index into agreement with the live namespace, one entry
at a time, with progress, a yield hook and a watermark.

The expected index is the set of live file entries whose recorded conversion ran over
their live bytes. Entries needing work are taken smallest first, so short files become
searchable before long ones; each is converted, recorded and indexed through one Palace
per-file update, which is its own transaction. Mirrors no live entry names are dropped.
A missing store is created by a Palace build over the mirror root before any new entry is
converted; a store left without a recorded identity by an interrupted bootstrap is rebuilt.
``should_yield`` is read between entries and between embedding slices; a true answer lets
the current entry's transaction finish and then stops the step. The watermark advances
only after a step that completes with Palace matching every live converted entry and no
other writer having changed the tree meanwhile. Identity discipline comes first: no
selector, no write; a stamp that disagrees with the selector, no write; a failed embedder
probe, no write.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from palace.index import _errors as palace_errors
from palace.index.build import build
from palace.index.embedder import Embedder
from palace.index.embedder_config import (
    embedder_config_path,
    identity_for,
    load_embedder_config,
    resolve_embedder,
)
from palace.index.schema import assert_identity
from palace.index.update import update

from biblios.config import Settings
from biblios.control import ConsumerError
from biblios.convert import convert_entry, recorded
from biblios.entries import Entry
from biblios.errors import BibliosError, IndexLagError, SealedShelfError, UndeclaredEmbedderError
from biblios.mirrors import (
    EDIT_CONVERTER,
    PENDING_CONVERSION,
    PENDING_REINDEX,
    MirrorInvalid,
    Provenance,
    classify_shelf,
    copy_evidence,
    digest,
    lane_for,
    read_mirror,
    retire_mirror,
    validate_mirror,
    write_mirror,
)
from biblios.progress import (
    EMBED_SLICE,
    EntryProgress,
    indexed_files,
    read_only_index,
    read_watermark,
    recorded_identity_incomplete,
    recorded_root,
    store_built,
    write_watermark,
)
from biblios.shelf import Mutation, Shelf

__all__ = [
    "ReconcileReport",
    "indexed_files",
    "reconcile",
    "recorded_identity_incomplete",
    "require_identity",
]

Progress = Callable[[EntryProgress], None]
ShouldYield = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    converted: tuple[str, ...]
    unconverted: tuple[str, ...]
    indexed: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged: tuple[str, ...]
    built: bool
    yielded: bool
    revision: int
    searchable_revision: int
    isolated: tuple[tuple[str, str], ...] = ()
    """Entries withheld for their mirror's state, each kept on disk as found."""


def require_identity(shelf: Shelf) -> None:
    """Refuse before any write unless the selector exists and matches a recorded stamp."""
    store = shelf.palace_store
    if not embedder_config_path(store).exists():
        raise UndeclaredEmbedderError(f"no embedder selector declared at {store}")
    if store_built(shelf):
        expected = identity_for(load_embedder_config(store))
        with read_only_index(shelf.chunks_db) as conn:
            assert_identity(conn, expected)


def reconcile(
    shelf: Shelf,
    settings: Settings,
    *,
    embedder: Embedder | None = None,
    progress: Progress | None = None,
    should_yield: ShouldYield | None = None,
    materialize: tuple[str, ...] = (),
) -> ReconcileReport:
    """Re-index changed mirrors and convert new data; never re-convert an existing mirror.

    ``materialize`` names copy operations created by this code whose entries may each
    receive one first mirror (see :func:`biblios.mirrors.copy_evidence`).
    """
    if shelf.sealed:
        raise SealedShelfError("a sealed shelf is read-only; unseal it before reconciling")
    require_identity(shelf)
    owned = embedder is None
    if embedder is None:
        embedder, _ = resolve_embedder(shelf.palace_store, purpose="document")
    try:
        embedder.probe()
        step = _Step(shelf, settings, embedder, progress, should_yield)
        step.materialize = tuple(materialize)
        return step.run()
    finally:
        if owned:
            embedder.close()


class _SlicingEmbedder:
    """Sends one entry's texts to the real embedder in slices, reporting after each and
    reading the yield hook before each; the slice in flight always completes."""

    max_concurrency = 1

    def __init__(
        self, inner: Embedder, on_slice: Callable[[int, int], None], should_yield: ShouldYield
    ) -> None:
        self._inner = inner
        self._on_slice = on_slice
        self._should_yield = should_yield
        self.yield_requested = False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        total = len(texts)
        vectors: list[list[float]] = []
        for start in range(0, total, EMBED_SLICE):
            if self._should_yield():
                self.yield_requested = True
            vectors.extend(self._inner.embed(texts[start : start + EMBED_SLICE]))
            self._on_slice(min(start + EMBED_SLICE, total), total)
        return vectors

    def probe(self) -> None:
        self._inner.probe()

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _Step:
    def __init__(
        self,
        shelf: Shelf,
        settings: Settings,
        embedder: Embedder,
        progress: Progress | None,
        should_yield: ShouldYield | None,
    ) -> None:
        self.shelf = shelf
        self.settings = settings
        self.embedder = embedder
        self.progress = progress or (lambda row: None)
        self.should_yield = should_yield or (lambda: False)
        self.expected_revision = shelf.revision
        self.external = False
        self.materialize: tuple[str, ...] = ()
        self.isolated: list[tuple[str, str]] = []

    def _own(self, mutation: Mutation) -> None:
        if mutation.revision_before != self.expected_revision:
            self.external = True
        self.expected_revision = mutation.revision_after

    def _row(self, entry: Entry, stage: str, **counts: int | None) -> None:
        row = EntryProgress(self.shelf.shelf_id, entry.entry_id, entry.path, stage, **counts)
        self.progress(row)

    def run(self) -> ReconcileReport:
        shelf = self.shelf
        # One step at a time, and never one that was admitted while the shelf was dynamic
        # and resumes after a seal: the lock is the seal's too, and the mode is read again
        # under it before any derived write.
        if not shelf._step_lock.acquire(blocking=False):
            raise BibliosError(f"a reconcile step is already running on shelf {shelf.shelf_id}")
        try:
            shelf._refuse_if_sealed()
            return self._run()
        finally:
            shelf._step_lock.release()

    def _run(self) -> ReconcileReport:
        shelf = self.shelf
        shelf.mirror_root.mkdir(exist_ok=True)
        live = [entry for entry in shelf.files() if entry.metadata is not None]
        states = classify_shelf(shelf, live)
        materializable: set[str] = set()
        for operation_id in self.materialize:
            materializable |= copy_evidence(shelf, operation_id)
        current: dict[str, str] = {}
        needs_conversion: set[str] = set()
        # A replaced file is a new ingestion: the previous mirror's digest, to be retained.
        displaced: dict[str, str] = {}
        reindex: set[str] = set()
        isolated: list[tuple[str, str]] = []
        for entry in live:
            metadata = entry.metadata
            assert metadata is not None
            state = states.get(entry.entry_id)
            if state is None:
                if not metadata.current_for(entry.cas_id):
                    if shelf.mirror_path(entry.entry_id).exists():
                        # A file at a never-converted entry's path is a user's mirror.
                        isolated.append((entry.entry_id, "mirror-user-owned"))
                    else:
                        needs_conversion.add(entry.entry_id)
            elif state.state == PENDING_CONVERSION:
                needs_conversion.add(entry.entry_id)
                if state.digest is not None:
                    displaced[entry.entry_id] = state.digest
            elif state.searchable:
                current[entry.entry_id] = metadata.mirror_sha256
            elif state.state == PENDING_REINDEX:
                current[entry.entry_id] = state.digest
                reindex.add(entry.entry_id)
            elif state.state == "mirror-missing" and entry.entry_id in materializable:
                needs_conversion.add(entry.entry_id)
            else:
                isolated.append((entry.entry_id, state.state))
        self.isolated = isolated
        revision = shelf.revision

        def provenance(entry, reason: str) -> Provenance:
            metadata = entry.metadata if entry is not None else None
            recorded = metadata is not None and metadata.converted
            return Provenance(
                shelf.palace_store,
                reason,
                metadata.source_cas_id if recorded else None,
                metadata.mirror_sha256 if recorded else None,
                None,
                revision,
            )

        # A mirror no live entry keeps is retired (kept under a retained name), never
        # deleted; an isolated entry's mirror stays exactly where the user left it.
        keep = set(current) | needs_conversion | {entry_id for entry_id, _ in isolated}
        for stray in shelf.mirror_root.glob("*.md"):
            if stray.stem not in keep:
                retire_mirror(stray, provenance=provenance(None, "stray"))
        store = shelf.palace_store
        built = False
        converted: list[str] = []
        unconverted: list[str] = []
        indexed: list[str] = []
        removed: list[str] = []
        unchanged: list[str] = []
        yielded = False
        try:
            db = shelf.chunks_db
            if db.is_file() and recorded_identity_incomplete(db):
                shutil.rmtree(db.parent)
            elif db.is_file():
                # Palace keyed the files under the mirror root as it was when they were
                # indexed; a store that moved (a copy unsealed elsewhere) is rebuilt rather
                # than extended under a second root.
                root = recorded_root(shelf)
                if root is not None and root != str(shelf.mirror_root.resolve()):
                    shutil.rmtree(db.parent)
            if not db.is_file():
                build(
                    store=store,
                    watch_root_filter=shelf.mirror_root,
                    embedder=self.embedder,
                    log=_quiet,
                    progress=self._build_progress(live),
                )
                built = True
            index = indexed_files(shelf)
            stale = sorted(set(index) - keep)
            if stale:
                update(
                    store=store,
                    watch_root=shelf.mirror_root,
                    paths=[shelf.mirror_path(entry_id) for entry_id in stale],
                    embedder=self.embedder,
                    log=_quiet,
                )
                removed = stale
            work = [
                entry
                for entry in live
                if entry.entry_id in needs_conversion
                or (
                    entry.entry_id in current
                    and index.get(entry.entry_id) != current[entry.entry_id]
                )
            ]
            unchanged = sorted(
                entry_id for entry_id, digest in current.items() if index.get(entry_id) == digest
            )
            for entry in sorted(work, key=lambda item: (item.size, item.entry_id)):
                if self.should_yield():
                    yielded = True
                    break
                if entry.entry_id in needs_conversion:
                    self._row(entry, "converting")
                    result = convert_entry(shelf, entry, self.settings)
                    if result.converted:
                        try:
                            write_mirror(
                                shelf.mirror_path(entry.entry_id),
                                result.data,
                                expected=displaced.get(entry.entry_id),
                                provenance=provenance(entry, "source-replaced"),
                            )
                        except ConsumerError:
                            # A mirror appeared meanwhile: it is the user's; never replaced.
                            isolated.append((entry.entry_id, "mirror-changed-concurrently"))
                            continue
                    if not self._record(entry, result):
                        continue
                    if not result.converted:
                        unconverted.append(entry.entry_id)
                        current.pop(entry.entry_id, None)
                        retire_mirror(
                            shelf.mirror_path(entry.entry_id),
                            provenance=provenance(entry, "unconverted"),
                        )
                        if entry.entry_id in index:
                            update(
                                store=store,
                                watch_root=shelf.mirror_root,
                                paths=[shelf.mirror_path(entry.entry_id)],
                                embedder=self.embedder,
                                log=_quiet,
                            )
                            removed.append(entry.entry_id)
                        self._row(entry, "unconverted")
                        continue
                    converted.append(entry.entry_id)
                    current[entry.entry_id] = result.mirror_sha256
                if index.get(entry.entry_id) == current[entry.entry_id]:
                    if entry.entry_id in reindex and not self._record_edit(entry, index):
                        isolated.append((entry.entry_id, "mirror-changed-concurrently"))
                        current.pop(entry.entry_id, None)
                    else:
                        unchanged.append(entry.entry_id)
                    continue
                if entry.entry_id in reindex and not self._valid_edit(entry, current):
                    # The bytes changed or became invalid after classification: withhold
                    # the entry on both sides so it never fails the run.
                    isolated.append((entry.entry_id, "mirror-changed-concurrently"))
                    current.pop(entry.entry_id, None)
                    continue
                wrapper = self._index(entry)
                indexed.append(entry.entry_id)
                if entry.entry_id in reindex and not self._record_edit(entry, indexed_files(shelf)):
                    isolated.append((entry.entry_id, "mirror-changed-concurrently"))
                    current.pop(entry.entry_id, None)
                if wrapper.yield_requested:
                    yielded = True
                    break
        except palace_errors.IndexError as error:
            raise IndexLagError(str(error)) from error
        searchable_revision = read_watermark(shelf)
        if not yielded:
            expected = {
                entry.entry_id: current[entry.entry_id]
                for entry in live
                if entry.entry_id in current and entry.entry_id not in unconverted
            }
            withheld = {entry_id for entry_id, _ in isolated}
            after = {k: v for k, v in indexed_files(shelf).items() if k not in withheld}
            if after != expected:
                raise IndexLagError(
                    f"index names {sorted(after)} but the live converted entries are "
                    f"{sorted(expected)}"
                )
            # The watermark is written under the handle lock a seal holds for its whole
            # transition, and never over a shelf sealed meanwhile.
            with shelf._handle.mutex:
                final = shelf.revision
                settled = not any(state == "mirror-changed-concurrently" for _, state in isolated)
                if (
                    settled
                    and not shelf.sealed
                    and not self.external
                    and final == self.expected_revision
                ):
                    write_watermark(shelf, final)
                    searchable_revision = final
        return ReconcileReport(
            converted=tuple(converted),
            unconverted=tuple(unconverted),
            indexed=tuple(indexed),
            removed=tuple(removed),
            unchanged=tuple(unchanged),
            built=built,
            yielded=yielded,
            revision=self.expected_revision,
            searchable_revision=searchable_revision,
            isolated=tuple(isolated),
        )

    def _record(self, entry: Entry, result: Any) -> bool:
        """Commit the conversion against the entry as it is now, under the handle lock, so
        a tag or date another writer set since the step's snapshot is kept and a replaced
        or removed entry is left for the next step; False when nothing was recorded."""
        shelf = self.shelf
        with shelf._handle.mutex:
            try:
                fresh = shelf.resolve(entry.entry_id)
            except BibliosError:
                return False
            if fresh.cas_id != entry.cas_id or fresh.metadata is None:
                return False
            self._own(shelf.record_conversion(fresh, recorded(fresh, result)))
        return True

    def _valid_edit(self, entry: Entry, current: dict[str, str]) -> bool:
        """Validate an edited mirror's bytes before Palace reads the file itself."""
        try:
            data = read_mirror(self.shelf.mirror_path(entry.entry_id), self.shelf.mirror_root)
            if data is None or digest(data) != current[entry.entry_id]:
                return False
            validate_mirror(data, lane=lane_for(self.shelf.palace_store))
        except MirrorInvalid:
            return False
        return True

    def _record_edit(self, entry: Entry, index: dict[str, str]) -> bool:
        """Record a re-indexed user edit only when the file, Palace and the validated
        digest agree; otherwise nothing is recorded and the entry stays pending."""
        shelf = self.shelf
        with shelf._handle.mutex:
            try:
                fresh = shelf.resolve(entry.entry_id)
                data = read_mirror(shelf.mirror_path(entry.entry_id), shelf.mirror_root)
            except (BibliosError, MirrorInvalid):
                return False
            if data is None or fresh.cas_id != entry.cas_id or fresh.metadata is None:
                return False
            found = digest(data)
            try:
                validate_mirror(data, lane=lane_for(shelf.palace_store))
            except MirrorInvalid:
                return False
            if index.get(entry.entry_id) != found:
                return False
            metadata = fresh.metadata.with_conversion(
                converter=EDIT_CONVERTER,
                converter_version="",
                mirror_sha256=found,
                source_cas_id=fresh.cas_id,
                conversion=fresh.metadata.conversion,
            )
            self._own(shelf.record_conversion(fresh, metadata))
        return True

    def _index(self, entry: Entry) -> _SlicingEmbedder:
        started = time.monotonic()
        self._row(entry, "indexing", done=0, total=None)

        def on_slice(done: int, total: int) -> None:
            elapsed = time.monotonic() - started
            eta = round((total - done) * elapsed / done) if 0 < done < total else None
            self._row(entry, "indexing", done=done, total=total, eta_seconds=eta)

        wrapper = _SlicingEmbedder(self.embedder, on_slice, self.should_yield)
        update(
            store=self.shelf.palace_store,
            watch_root=self.shelf.mirror_root,
            paths=[self.shelf.mirror_path(entry.entry_id)],
            embedder=wrapper,
            log=_quiet,
        )
        self._row(entry, "searchable")
        return wrapper

    def _build_progress(self, live: list[Entry]) -> Callable[[Any], None]:
        by_id = {entry.entry_id: entry for entry in live}

        def report(event: Any) -> None:
            path = getattr(event, "path", None)
            entry = by_id.get(Path(str(path)).stem) if path else None
            if entry is not None:
                self._row(entry, "indexing")

        return report


def _quiet(_line: str) -> None:
    return None
