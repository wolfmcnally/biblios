"""Finite consumer operations over an explicitly admitted shelf or stack.

This facade carries no security authority. It never discovers model providers,
owns a borrowed handle, or creates a second namespace coordinator.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from threading import Lock
from typing import TYPE_CHECKING, TypeVar

from palace.index.embedder import Embedder
from palace.rerank import Reranker

from biblios.binding import ShelfBinding, validate_handle
from biblios.config import Settings
from biblios.control import CallControl, ConsumerError, Observation, ShelfView, controlled
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
    indexed_files,
    read_watermark,
    recorded_identity_incomplete,
)
from biblios.search import Page, SearchResult, _page
from biblios.shelf import Shelf
from biblios.stack import Stack, StackResult, open_stack

if TYPE_CHECKING:
    from castle.namespace import ExcisionInventory, OperationStatus, SnapshotView

    from biblios.history import ContentReference, ExcisionProposal, ExcisionRequest, ExcisionResult
    from biblios.pipeline import ConsumerPipeline, DerivationStatus, ReuseDonor

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

    @property
    def active_work(self) -> int:
        """Synchronous calls still holding supplied providers; zero before unmount."""
        with self._lifecycle:
            return self._active

    @contextmanager
    def _working(self) -> Iterator[None]:
        with self._shelf._handle.mutex:
            with self._lifecycle:
                self._admit()
                self._active += 1
        try:
            yield
        finally:
            with self._lifecycle:
                self._active -= 1

    def _admit(self, *, write: bool = False) -> ShelfView:
        if self._closed:
            raise ConsumerError("closed", stage="binding")
        if write and self.binding.access != "read-write":
            raise ConsumerError("read-only", stage="binding")
        view = validate_handle(self.binding, self._shelf._handle)
        missing_index = not self._shelf.chunks_db.is_file()
        recorded_conversion = missing_index and any(
            entry.metadata is not None and entry.metadata.converted for entry in self._shelf.files()
        )
        if missing_index and (self._index_seen or recorded_conversion):
            raise ConsumerError("index-missing", stage="binding", effect="recovery-required")
        self._index_seen = self._index_seen or self._shelf.chunks_db.is_file()
        if not missing_index:
            try:
                incomplete = recorded_identity_incomplete(self._shelf.chunks_db)
                previously_built = self._index_admitted or read_watermark(self._shelf) >= 0
                if incomplete and (
                    previously_built
                    or any(
                        entry.metadata is not None and entry.metadata.converted
                        for entry in self._shelf.files()
                    )
                ):
                    raise ConsumerError(
                        "index-identity-incomplete", stage="binding", effect="recovery-required"
                    )
                self._index_admitted = self._index_admitted or not incomplete
            except ConsumerError:
                raise
            except Exception:
                raise ConsumerError(
                    "index-unreadable", stage="binding", effect="recovery-required"
                ) from None
        return view

    def _view(self) -> ShelfView:
        view = self._admit()
        index = indexed_files(self._shelf)
        mirrors = []
        for entry in self._shelf.files():
            if entry.metadata is not None and entry.metadata.converted:
                file = self._shelf.mirror_path(entry.entry_id)
                if file.is_symlink() or not file.resolve().is_relative_to(
                    self.binding.mirror_root.resolve()
                ):
                    raise ConsumerError("artifact-outside-domain", stage="read")
                digest = hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None
                if digest != entry.metadata.mirror_sha256:
                    raise ConsumerError("mirror-unavailable", stage="read")
                mirrors.append((entry.entry_id, entry.cas_id, entry.metadata.mirror_sha256, digest))
        marker = self.binding.palace_root / "consumer-generation.json"
        generation = marker.read_text() if marker.is_file() else ""
        selectors = [
            hashlib.sha256((self.binding.palace_root / "meta" / name).read_bytes()).hexdigest()
            if (self.binding.palace_root / "meta" / name).is_file()
            else None
            for name in ("embedder.toml", "vector-backend.toml")
        ]
        body = json.dumps(
            [sorted(index.items()), read_watermark(self._shelf), mirrors, generation, selectors],
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
            except Exception:
                raise ConsumerError("view-unreadable", stage="read") from None

    def _read(self, call: Callable[[], T], control: CallControl) -> T:
        with self._shelf._handle.mutex:
            self._admit()
            control.check("read")
            try:
                result = call()
            except ConsumerError:
                raise
            except Exception:
                raise ConsumerError("read-failed", stage="read") from None
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
            entry = self._shelf.resolve(entry_id)
            if not entry.searchable or entry.metadata is None:
                raise ConsumerError("mirror-unavailable", stage="read")
            file = self._shelf.mirror_path(entry_id)
            if (
                file.is_symlink()
                or not file.resolve().is_relative_to(self.binding.mirror_root.resolve())
                or hashlib.sha256(file.read_bytes()).hexdigest() != entry.metadata.mirror_sha256
            ):
                raise ConsumerError("mirror-unavailable", stage="read")
            from palace.index.embedder_config import load_embedder_config

            if load_embedder_config(self.binding.palace_root).representation is not None:
                # A supplied representation owns its complete Markdown view, including
                # any front matter. It is not Drawbridge's provenance-envelope format.
                return _page(entry, file.read_text(encoding="utf-8"), page=page)
            return self._shelf.read(entry_id, page=page)

        return self._read(read, control)

    def status(self, *, control: CallControl = CallControl()) -> IndexStatus:
        return self._read(lambda: index_status(self._shelf), control)

    def checkpoint(self) -> IndexStatus:
        """Publish a no-sidecar dynamic read checkpoint as the admitted writer."""
        from biblios.progress import prepare_readable

        with self._shelf._handle.mutex:
            self._admit(write=True)
            try:
                return prepare_readable(self._shelf)
            except ConsumerError:
                raise
            except Exception:
                raise ConsumerError(
                    "checkpoint-failed", stage="index", effect="recovery-required"
                ) from None

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
            except Exception:
                raise ConsumerError("namespace-entry-missing", stage="curation") from None
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
        before = self.view
        control.check("search")
        try:
            result = self._shelf.search(
                query,
                limit=limit,
                embedder=_EmbedderFence(embedder, control),
                reranker=_RerankerFence(reranker, control),
                strict=True,
            )
            if self.view != before:
                raise ConsumerError("stale-view", stage="search")
            control.check("search")
            return result
        except ConsumerError:
            raise
        except Exception:
            raise ConsumerError("search-failed", stage="search") from None

    def derive(
        self,
        pipeline: ConsumerPipeline,
        entry_id: str,
        *,
        operation_id: str,
        donor: ReuseDonor | None = None,
        retry_uncertain: bool = False,
        control: CallControl = CallControl(),
        observer: Callable[[Observation], None] | None = None,
    ) -> DerivationStatus:
        with self._working():
            return pipeline.derive(
                self,
                entry_id,
                operation_id=operation_id,
                donor=donor,
                retry_uncertain=retry_uncertain,
                control=control,
                observer=observer,
            )

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
                before = tuple(member.view for member in self._members)
            control.check("search")
            result = self._stack.search(
                query,
                limit=limit,
                embedder=_EmbedderFence(embedder, control),
                reranker=_RerankerFence(reranker, control),
                strict=True,
            )
            with self._locked():
                if tuple(member.view for member in self._members) != before:
                    raise ConsumerError("stale-view", stage="search")
                control.check("search")
                return result
        except ConsumerError:
            raise
        except Exception:
            raise ConsumerError("search-failed", stage="search") from None


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
    except Exception:
        raise ConsumerError("stack-binding-failed", stage="binding") from None
