"""Finite consumer operations over an explicitly admitted shelf or stack.

This facade carries no security authority. It never discovers model providers,
owns a borrowed handle, or creates a second namespace coordinator.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import replace
from threading import Lock
from typing import TYPE_CHECKING, TypeVar

from palace.index.embedder import Embedder
from palace.rerank import Reranker
from palace.writer_lock import WriterLockTimeout, writer_lock

from biblios.binding import ShelfBinding, validate_handle
from biblios.config import Settings
from biblios.control import CallControl, ConsumerError, Observation, ShelfView, Stage, controlled
from biblios.curation import (
    CopyIdentity,
    CurationRequest,
    CurationResult,
    DuplicateEntry,
    execute,
    stable_id,
)
from biblios.entries import Entry
from biblios.progress import (
    IndexStatus,
    index_status,
    recorded_identity_incomplete,
)
from biblios.search import Page, SearchResult, _page
from biblios.shelf import Shelf
from biblios.stack import Stack, StackResult, open_stack

if TYPE_CHECKING:
    from castle.namespace import ExcisionInventory, OperationStatus, SnapshotView

    from biblios.core import BringCurrentRequest, BringCurrentResult, StageAuthority
    from biblios.history import ContentReference, ExcisionProposal, ExcisionRequest, ExcisionResult
    from biblios.pipeline import ConsumerPipeline, ReuseDonor
    from biblios.progress import SettleResult

T = TypeVar("T")


class BoundShelf:
    """An admitted consumer lifetime; close never closes a borrowed handle."""

    def __init__(self, binding: ShelfBinding, shelf: Shelf) -> None:
        self.binding = binding
        self._shelf = shelf
        self._closed = False
        self._active = 0
        self._lifecycle = Lock()
        self._index_admitted = False
        self._index_seen = shelf.chunks_db.is_file()
        self._recovering = False
        """True only inside an explicit sweep, whose own reads pass a missing index."""

    @property
    def active_work(self) -> int:
        """Synchronous calls still holding supplied providers; zero before unmount."""
        with self._lifecycle:
            return self._active

    @contextmanager
    def _working(self, *, recover: bool = False) -> Iterator[None]:
        with self._shelf._handle.mutex:
            with self._lifecycle:
                self._admit(recover=recover)
                self._active += 1
        try:
            yield
        finally:
            with self._lifecycle:
                self._active -= 1

    @contextmanager
    def _writer(
        self,
        operation: str,
        *,
        stage: Stage = "index",
        control: CallControl = CallControl(),
        recover: bool = False,
    ) -> Iterator[None]:
        """Keep the lifetime active while waiting, without taking the reader mutex.

        ``recover`` is only the explicit sweep, which rebuilds a lost index from the
        accepted mirrors; every other call refuses a missing index."""
        with self._working(recover=recover), ExitStack() as stack:
            with self._shelf._handle.mutex:
                self._admit(write=True, recover=recover)
            if recover:
                self._recovering = True
                stack.callback(setattr, self, "_recovering", False)
            # A call cancelled or past its deadline before it starts refuses here, before
            # the writer lock or any store exists: no bootstrap effect.
            control.check(stage)
            try:
                stack.enter_context(writer_lock(self.binding.palace_root, operation=operation))
            except WriterLockTimeout as error:
                # Chained: the timeout says which lock and holder the caller waited on.
                raise ConsumerError("writer-busy", stage=stage) from error
            # Only admission errors above are effect-free; body failures are not.
            yield

    def _admit(self, *, write: bool = False, recover: bool = False) -> ShelfView:
        if self._closed:
            raise ConsumerError("closed", stage="binding")
        if write and self.binding.access != "read-write":
            raise ConsumerError("read-only", stage="binding")
        view = validate_handle(self.binding, self._shelf._handle)
        missing_index = not self._shelf.chunks_db.is_file()
        if (
            missing_index
            and not (recover or self._recovering)
            and (self._index_seen or self._recorded())
        ):
            # A refusal: the marker answered without opening a store, or the
            # authoritative counters did.
            raise ConsumerError("index-missing", stage="binding", effect="recovery-required")
        self._index_seen = self._index_seen or self._shelf.chunks_db.is_file()
        if not missing_index:
            try:
                incomplete = recorded_identity_incomplete(self._shelf.chunks_db)
                previously_built = self._index_admitted or self._recorded()
                if incomplete and (previously_built or self._recorded()):
                    raise ConsumerError(
                        "index-identity-incomplete", stage="binding", effect="recovery-required"
                    )
                self._index_admitted = self._index_admitted or not incomplete
            except ConsumerError:
                raise
            except Exception as error:
                raise ConsumerError(
                    "index-unreadable", stage="binding", effect="recovery-required"
                ) from error
        return view

    def _recorded(self) -> bool:
        """Whether this shelf holds, or may hold, accepted state (O(1)).

        The ``accepted-ever`` marker is an optimization, never authority: present, it
        refuses without opening a store (fail closed; an explicit sweep repairs a stale
        one); absent, the accepted counters decide, so a deleted marker cannot make
        accepted state read as never built.
        """
        from biblios.derived import accepted_ever, read_derived

        root = self.binding.palace_root
        if accepted_ever(root):
            return True
        store = read_derived(root)
        if store is None:
            return False
        try:
            return store.counter("state:accepted") > 0
        finally:
            store.close()

    def _accumulator(self) -> str:
        from biblios.derived import DerivedStore

        if not self._recorded():
            return "0"
        store = DerivedStore(self.binding.palace_root, read_only=True)
        try:
            row = store.conn.execute("SELECT value FROM view WHERE key='accumulator'").fetchone()
        finally:
            store.close()
        return "0" if row is None else row[0]

    def _store_terms(self) -> list:
        """The terms that identify the derived store itself rather than its contents:
        the consumer generation marker and the embedder and vector-backend selectors."""
        marker = self.binding.palace_root / "consumer-generation.json"
        generation = marker.read_text() if marker.is_file() else ""
        selectors = [
            hashlib.sha256((self.binding.palace_root / "meta" / name).read_bytes()).hexdigest()
            if (self.binding.palace_root / "meta" / name).is_file()
            else None
            for name in ("embedder.toml", "vector-backend.toml")
        ]
        return [generation, selectors]

    def _search_identity(self) -> tuple:
        """What a search must find unchanged across its provider calls: the admitted
        shelf, namespace epoch and lineage, database generation and store terms, but not
        the revision or the accepted-state accumulator. Entries curated or accepted while
        the query embeds do not refuse the answer; every returned hit is proven against
        its own current accepted record by the strict per-hit check instead."""
        with self._shelf._handle.mutex:
            view = self._admit()
            return (replace(view, revision=0, derived_view=""), self._store_terms())

    def _view(self) -> ShelfView:
        view = self._admit()
        # The served view is the incrementally maintained accumulator over each served
        # entry's (entry id, accepted stage): O(1), and unchanged by unrelated activity.
        # The observation epoch and gap term is the constant ``none`` until observation
        # exists.
        generation, selectors = self._store_terms()
        body = json.dumps(
            [self._accumulator(), "none", generation, selectors],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return replace(view, derived_view=hashlib.sha256(body).hexdigest())

    @property
    def view(self) -> ShelfView:
        with self._shelf._handle.mutex:
            try:
                return self._view()
            except ConsumerError:
                raise
            except Exception as error:
                raise ConsumerError("view-unreadable", stage="read") from error

    def _read(self, call: Callable[[], T], control: CallControl) -> T:
        with self._shelf._handle.mutex:
            self._admit()
            control.check("read")
            try:
                result = call()
            except ConsumerError:
                raise
            except Exception as error:
                raise ConsumerError("read-failed", stage="read") from error
            self._admit()
            control.check("read")
            return result

    def files(self, *, control: CallControl = CallControl()) -> tuple[Entry, ...]:
        return self._read(lambda: tuple(self._shelf.files()), control)

    def resolve(self, entry_id: str, *, control: CallControl = CallControl()) -> Entry:
        return self._read(lambda: self._shelf.resolve(entry_id), control)

    def download(self, entry_id: str, *, control: CallControl = CallControl()) -> bytes:
        def read() -> bytes:
            entry = self._shelf.resolve(entry_id)
            data = self._shelf.download(entry_id)
            if (
                entry.size != len(data)
                or entry.cas_id != "sha256:" + hashlib.sha256(data).hexdigest()
            ):
                raise ConsumerError("content-mismatch", stage="read")
            return data

        return self._read(read, control)

    def read(
        self,
        entry_id: str,
        *,
        page: int | None = None,
        control: CallControl = CallControl(),
    ) -> Page:
        def read() -> Page:
            from palace.index.embedder_config import load_embedder_config

            from biblios.mirrors import mirror_body
            from biblios.search import MirrorUnavailable, readable_mirror

            entry = self._shelf.resolve(entry_id)
            try:
                text, state = readable_mirror(self._shelf, entry)
            except MirrorUnavailable as error:
                raise ConsumerError(error.code, stage="read") from None
            if load_embedder_config(self.binding.palace_root).representation is not None:
                # A supplied representation owns its complete Markdown view, including
                # any front matter. It is not Drawbridge's provenance-envelope format.
                return _page(entry, text, page=page, mirror_state=state)
            return _page(entry, mirror_body(text), page=page, mirror_state=state)

        return self._read(read, control)

    def status(self, *, control: CallControl = CallControl()) -> IndexStatus:
        return self._read(lambda: index_status(self._shelf), control)

    def checkpoint(self) -> IndexStatus:
        """Publish a no-sidecar dynamic read checkpoint as the admitted writer."""
        from biblios.progress import prepare_readable

        with self._writer("consumer readable checkpoint"), self._shelf._handle.mutex:
            self._admit(write=True)
            try:
                return prepare_readable(self._shelf)
            except ConsumerError:
                raise
            except Exception as error:
                raise ConsumerError(
                    "checkpoint-failed", stage="index", effect="recovery-required"
                ) from error

    def settle_for_readers(self) -> SettleResult:
        """As the admitted writer, leave both accepted stores openable read-only with no
        sidecars; ``busy`` is a per-call answer, never a shelf-wide failure."""
        from biblios.progress import settle_for_readers

        with self._writer("consumer reader settle"), self._shelf._handle.mutex:
            self._admit(write=True)
            return settle_for_readers(self._shelf)

    def retained(self) -> tuple[ContentReference, ...]:
        from biblios.history import retained

        return retained(self)

    def read_retained(self, reference: ContentReference) -> bytes:
        from biblios.history import read_retained

        return read_retained(self, reference)

    def history_inventory(self) -> ExcisionInventory:
        from biblios.history import inventory

        return inventory(self)

    def snapshot(self, snapshot_id: str) -> SnapshotView:
        from biblios.history import snapshot

        return snapshot(self, snapshot_id)

    def preview_excision(self, request: ExcisionRequest) -> ExcisionProposal:
        from biblios.history import preview

        return preview(self, request)

    def excise(
        self,
        request: ExcisionRequest,
        *,
        control: CallControl = CallControl(),
    ) -> ExcisionResult:
        from biblios.history import excise

        return excise(self, request, control=control)

    def curate(
        self,
        request: CurationRequest,
        *,
        settings: Settings | None = None,
        control: CallControl = CallControl(),
        observer: Callable[[Observation], None] | None = None,
    ) -> CurationResult:
        """Apply composed source limits, or the default when settings are omitted."""
        return execute(self, request, settings=settings, control=control, observer=observer)

    def operation(self, operation_id: str) -> OperationStatus:
        from castle.namespace import namespace_operation

        return self._read(
            lambda: namespace_operation(self._shelf._handle, operation_id), CallControl()
        )

    def copy_request(
        self,
        *,
        operation_id: str,
        expected_revision: int,
        entry_id: str,
        parent_id: str,
        name: str,
    ) -> CurationRequest:
        """Capture a finite frozen copy inventory for retention and exact replay."""
        with self._shelf._handle.mutex:
            view = self._admit()
            if view.revision != expected_revision:
                raise ConsumerError("namespace-revision-conflict", stage="curation")
            live = self._shelf._live()
            if entry_id not in live:
                raise ConsumerError("namespace-entry-missing", stage="curation")
            try:
                selected = sorted(self._shelf._subtree(entry_id, live))
            except Exception as error:
                raise ConsumerError("namespace-entry-missing", stage="curation") from error
            identities = tuple(
                CopyIdentity(source, stable_id(self.binding.namespace_id, operation_id, source))
                for source in selected
            )
            return CurationRequest(
                operation_id,
                expected_revision,
                DuplicateEntry(entry_id, parent_id, name, identities),
            )

    def search(
        self,
        query: str,
        *,
        embedder: Embedder,
        reranker: Reranker,
        limit: int = 5,
        control: CallControl = CallControl(),
    ) -> SearchResult:
        if embedder is None or reranker is None:
            raise ConsumerError("provider-required", stage="search")
        with self._working():
            return self._search(
                query, embedder=embedder, reranker=reranker, limit=limit, control=control
            )

    def _search(
        self,
        query: str,
        *,
        embedder: Embedder,
        reranker: Reranker,
        limit: int,
        control: CallControl,
    ) -> SearchResult:
        try:
            before = self._search_identity()
        except ConsumerError:
            raise
        except Exception as error:
            raise ConsumerError("view-unreadable", stage="read") from error
        control.check("search")
        try:
            result = self._shelf.search(
                query,
                limit=limit,
                embedder=_EmbedderFence(embedder, control),
                reranker=_RerankerFence(reranker, control),
                strict=True,
            )
            if self._search_identity() != before:
                raise ConsumerError("stale-view", stage="search")
            control.check("search")
            self._observe(result.observations)
            return result
        except ConsumerError:
            raise
        except Exception as error:
            raise ConsumerError("search-failed", stage="search") from error

    def _observe(self, observations) -> None:
        """A writable binding marks the entries its readers' observations name, in the
        volatile store only (no writer lock); a read-only binding persists nothing. The
        observations themselves go back to a write-grade call in its request."""
        if not observations or self.binding.access != "read-write":
            return
        from biblios.derived import VolatileStore

        volatile = VolatileStore(self.binding.palace_root)
        with closing(volatile.conn):
            volatile.mark_dirty(
                sorted({item.entry_id for item in observations}), source="read-observation"
            )

    def bring_current(
        self,
        pipeline: ConsumerPipeline,
        request: BringCurrentRequest,
        *,
        authority: StageAuthority | None = None,
        control: CallControl = CallControl(),
        should_yield: Callable[[], bool] | None = None,
    ) -> BringCurrentResult:
        """Bring the named or discovered entries current through the one core (whose
        writer admission holds this call's one unit of active work).

        ``authority`` is the caller's per-call evidence (:class:`StageAuthority`);
        omitting it selects authority-free publication for this call. A caller that
        authenticates supplies it on every call and refuses omission itself."""
        from biblios.core import bring_current

        return bring_current(
            self,
            request,
            pipeline=pipeline,
            authority=authority,
            control=control,
            should_yield=should_yield,
        )

    def derive(
        self,
        pipeline: ConsumerPipeline,
        entry_id: str,
        *,
        operation_id: str,
        donor: ReuseDonor | None = None,
        retry_uncertain: bool = False,
        authority: StageAuthority | None = None,
        control: CallControl = CallControl(),
    ) -> BringCurrentResult:
        """Bring one entry current: a one-entry request to the core. ``donor`` nominates
        one captured view (:meth:`donor`) whose converted body and identical ordered
        embedding inputs may be reused for this entry's conversion."""
        from biblios.core import BringCurrentRequest

        return self.bring_current(
            pipeline,
            BringCurrentRequest(
                operation_id,
                (entry_id,),
                retry_uncertain=retry_uncertain,
                donors=None if donor is None else {entry_id: donor},
            ),
            authority=authority,
            control=control,
        )

    def donor(self, entry_id: str) -> ReuseDonor:
        """Capture an entry's actual accepted view as a reuse nominee."""
        from biblios.core import capture_donor

        return capture_donor(self, entry_id)

    def update_mirror(
        self,
        pipeline: ConsumerPipeline,
        entry_id: str,
        *,
        operation_id: str,
        markdown: str,
        expected_sha256: str | None,
        authority: StageAuthority | None = None,
        control: CallControl = CallControl(),
    ) -> BringCurrentResult:
        """Replace an entry's mirror with caller text over exactly the expected digest,
        and bring it current, through the core's update seam."""
        from biblios.core import BringCurrentRequest, UpdateSeam

        if type(markdown) is not str or not (
            expected_sha256 is None or (type(expected_sha256) is str and len(expected_sha256) == 64)
        ):
            raise ConsumerError("request-invalid", stage="index")
        seam = UpdateSeam(entry_id, expected_sha256, markdown.encode("utf-8"))
        return self.bring_current(
            pipeline,
            BringCurrentRequest(operation_id, (entry_id,), update=seam),
            authority=authority,
            control=control,
        )

    def grant_first_ingestion(
        self,
        entry_id: str,
        *,
        expected_cas_id: str,
        operation_id: str,
        expected_mirror_sha256: str | None = None,
    ) -> bool:
        """The explicit first-ingestion grant for one never-converted entry; with
        ``expected_mirror_sha256``, adoption of the existing mirror with exactly that
        digest (rebuilding a lost derived store)."""
        from biblios.core import grant_first_ingestion

        return grant_first_ingestion(
            self,
            entry_id,
            expected_cas_id=expected_cas_id,
            operation_id=operation_id,
            expected_mirror_sha256=expected_mirror_sha256,
        )

    def mark_dirty(self, entry_ids: Sequence[str], *, source: str) -> int:
        """Record entries that may have changed; the observer's one marking call."""
        from biblios.core import mark_dirty

        return mark_dirty(self, entry_ids, source=source)

    def close(self) -> None:
        with self._lifecycle:
            if self._active:
                raise ConsumerError("work-active", stage="binding")
            self._closed = True
        with self._shelf._handle.mutex:
            self._shelf.close()

    def __enter__(self) -> BoundShelf:
        self._admit()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _EmbedderFence:
    def __init__(self, provider: Embedder, control: CallControl) -> None:
        self.provider, self.control = provider, control

    @property
    def max_concurrency(self) -> int:
        return self.provider.max_concurrency

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return controlled(lambda: self.provider.embed(texts), self.control, "embedding")

    def probe(self) -> None:
        controlled(self.provider.probe, self.control, "embedding")

    def close(self) -> None:
        pass  # The injected provider always remains caller-owned.


class _RerankerFence:
    def __init__(self, provider: Reranker, control: CallControl) -> None:
        self.provider, self.control = provider, control

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        return controlled(lambda: self.provider.score(query, texts), self.control, "search")

    def probe(self) -> None:
        controlled(self.provider.probe, self.control, "search")

    def close(self) -> None:
        pass  # The injected provider always remains caller-owned.


class BoundStack:
    """Only the supplied admitted members, with one Palace union rerank."""

    def __init__(self, members: tuple[BoundShelf, ...], stack: Stack) -> None:
        self._members, self._stack = members, stack

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with ExitStack() as locks:
            for member in sorted(self._members, key=lambda item: item.binding.namespace_id):
                locks.enter_context(member._shelf._handle.mutex)
            yield

    def search(
        self,
        query: str,
        *,
        embedder: Embedder,
        reranker: Reranker,
        limit: int = 5,
        control: CallControl = CallControl(),
    ) -> StackResult:
        if embedder is None or reranker is None:
            raise ConsumerError("provider-required", stage="search")
        with ExitStack() as work:
            for member in self._members:
                work.enter_context(member._working())
            return self._search(
                query, embedder=embedder, reranker=reranker, limit=limit, control=control
            )

    def _search(
        self,
        query: str,
        *,
        embedder: Embedder,
        reranker: Reranker,
        limit: int,
        control: CallControl,
    ) -> StackResult:
        try:
            with self._locked():
                before = tuple(member._search_identity() for member in self._members)
            control.check("search")
            result = self._stack.search(
                query,
                limit=limit,
                embedder=_EmbedderFence(embedder, control),
                reranker=_RerankerFence(reranker, control),
                strict=True,
            )
            with self._locked():
                if tuple(member._search_identity() for member in self._members) != before:
                    raise ConsumerError("stale-view", stage="search")
                control.check("search")
            for member in self._members:
                member._observe(result.observations.get(member._shelf.shelf_id, ()))
            return result
        except ConsumerError:
            raise
        except Exception as error:
            raise ConsumerError("search-failed", stage="search") from error


def open_bound_stack(shelves: Sequence[BoundShelf]) -> BoundStack:
    members = tuple(shelves)
    if not members or len({item.binding.namespace_id for item in members}) != len(members):
        raise ConsumerError("invalid-members", stage="binding")
    try:
        for member in members:
            member._admit()
        return BoundStack(members, open_stack([member._shelf for member in members]))
    except ConsumerError:
        raise
    except Exception as error:
        raise ConsumerError("stack-binding-failed", stage="binding") from error
