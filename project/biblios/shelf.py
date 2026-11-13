"""The shelf: one self-describing directory holding bytes, a namespace, mirrors and an index.

Layout::

    <shelf>/
      shelf.json       manifest: schema, shelf id, name, description, generation, mode
      castle/          Castle store: exact bytes, content addressed; journal.jsonl is the truth
      namespace/       Castle namespace: the shelf's file tree
      namespace.lock   Castle's domain lock (it must sit outside both roots)
      mirror/          one Drawbridge mirror per file entry, keyed by entry id (derived)
      palace/          the Palace store (derived); meta/embedder.toml is the declared identity

Every curation operation is one Castle namespace mutation and inherits its atomicity.
Palace is the only record of the shelf's embedding identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from castle import cas
from castle.journal import ensure_store
from castle.namespace import (
    EntryChange,
    EntryRecord,
    InitializeRequest,
    MutationRequest,
    MutationResult,
    NamespaceError,
    ReadRequest,
    ReadResult,
    SnapshotRecord,
    create_namespace,
    execute_namespace,
    namespace_operation_target,
    open_namespace,
    open_namespace_readonly,
    read_namespace,
)
from castle.namespace_environment import LocalSession
from castle.namespace_store import NamespaceHandle
from castle.store_state import read_store_identity
from drawbridge.identify import sniff_media_type
from palace.index.embedder import Embedder
from palace.rerank import Reranker
from palace.settings import initialize_store

from biblios.config import Settings
from biblios.entries import DIRECTORY_METADATA, Entry, EntryMetadata, path_of, view
from biblios.errors import (
    BibliosError,
    DuplicateContentError,
    NamespaceFormatError,
    SealedShelfError,
)
from biblios.progress import write_watermark

if TYPE_CHECKING:
    from biblios.binding import ShelfBinding
    from biblios.core import BringCurrentResult
    from biblios.progress import EntryProgress, IndexStatus
    from biblios.seal import SealRecord
    from biblios.search import Page, SearchResult
    from biblios.verify import VerifyReport

SHELF_SCHEMA = "biblios.shelf/1"
# Castle's entry-id form; anything else names no entry.
_ENTRY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# Entry records per change-discovery page.
CHANGE_PAGE = 1000
"""Schema identifier written into every shelf manifest."""

MANIFEST_NAME = "shelf.json"
SEAL_NAME = "seal.json"
CASTLE_DIR = "castle"
NAMESPACE_DIR = "namespace"
NAMESPACE_LOCK = "namespace.lock"
MIRROR_DIR = "mirror"
PALACE_DIR = "palace"


@dataclass(frozen=True, slots=True)
class ShelfManifest:
    """What a shelf declares about itself in ``shelf.json``.

    The manifest carries no embedding identity. Palace's declared selector and the
    stamp it writes at build time are the only record of a shelf's identity; a copy
    here would be a second source of truth.
    """

    shelf_id: str
    name: str
    description: str
    generation: int = 0
    mode: str = "dynamic"
    schema: str = SHELF_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SHELF_SCHEMA:
            raise ValueError(f"unsupported shelf schema: {self.schema!r}")
        if not self.shelf_id:
            raise ValueError("a shelf needs a non-empty shelf id")
        if self.mode not in {"dynamic", "sealed"}:
            raise ValueError(f"unknown shelf mode: {self.mode!r}")
        if self.generation < 0:
            raise ValueError("generation cannot be negative")

    def dumps(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    def write(self, path: Path) -> None:
        """Publish by rename, so a reader never sees a half-written manifest."""
        staged = path.with_name(path.name + ".tmp")
        staged.write_text(self.dumps())
        os.replace(staged, path)

    @classmethod
    def read(cls, path: Path) -> ShelfManifest:
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise BibliosError(f"shelf manifest unreadable: {path}") from error
        fields = {"shelf_id", "name", "description", "generation", "mode", "schema"}
        if not isinstance(document, dict) or set(document) != fields:
            raise BibliosError(f"shelf manifest does not carry the {SHELF_SCHEMA} fields")
        try:
            return cls(**document)
        except (TypeError, ValueError) as error:
            raise BibliosError(f"shelf manifest refused: {error}") from error


@dataclass(frozen=True, slots=True)
class Mutation:
    """The typed outcome of one curation operation, as Castle recorded it.

    ``entry_id`` is the entry the operation was about: the created entry, the renamed or
    moved or replaced entry, the root of a copy, or the removed entry. A retry carrying
    the same operation id and the same request returns the same record with ``replayed``
    set; ``operation_id`` here is Castle's, derived from the caller's id and the request.
    The current tree is read separately with ``stat`` or ``resolve``.
    """

    operation_id: str
    kind: str
    entry_id: str
    affected_entry_ids: tuple[str, ...]
    revision_before: int
    revision_after: int
    replayed: bool


def _mutation(result: MutationResult, args: dict[str, Any], *, replayed: bool) -> Mutation:
    if result.kind == "duplicate":
        # The copied root is the identity minted for the source root; Castle's canonical
        # request does not keep the caller's list order.
        entry_id = next(
            item["new_id"] for item in args["identities"] if item["source_id"] == args["entry_id"]
        )
    else:
        entry_id = args["entry_id"]
    return Mutation(
        operation_id=result.operation_id,
        kind=result.kind,
        entry_id=entry_id,
        affected_entry_ids=tuple(result.affected_entry_ids),
        revision_before=result.revision_before,
        revision_after=result.revision_after,
        replayed=replayed,
    )


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _uuid() -> str:
    return str(uuid.uuid4())


def _content_id(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sniff(data: bytes, name: str) -> str:
    with tempfile.TemporaryDirectory(prefix="biblios-sniff-") as temporary:
        staged = Path(temporary) / name
        staged.write_bytes(data)
        media_type, _flags = sniff_media_type(staged)
    return media_type


def _split(path: str) -> list[str]:
    components = [part for part in path.strip("/").split("/") if part]
    return [unicodedata.normalize("NFC", part) for part in components]


class Shelf:
    """An open shelf. Use as a context manager; every method is one library call."""

    def __init__(
        self,
        root: Path,
        manifest: ShelfManifest,
        handle: NamespaceHandle,
        *,
        layout: ShelfBinding | None = None,
        owns_handle: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.manifest = manifest
        self._handle = handle
        self._layout = layout
        self._owns_handle = owns_handle
        self._closed = False
        # Held by a running reconcile step for its whole run and by a seal for its whole
        # transition, so neither can start while the other is writing derived state.
        self._step_lock = threading.Lock()
        info = handle.info()
        self._namespace_id = info.namespace_id
        self._root_id = info.root_id
        self._cas = info.cas

    # ---- layout -------------------------------------------------------------------

    @property
    def shelf_id(self) -> str:
        return self.manifest.shelf_id

    @property
    def castle_root(self) -> Path:
        return self._handle.cas_root

    @property
    def namespace_root(self) -> Path:
        return self._handle.root

    @property
    def mirror_root(self) -> Path:
        return self._layout.mirror_root if self._layout is not None else self.root / MIRROR_DIR

    @property
    def palace_store(self) -> Path:
        return self._layout.palace_root if self._layout is not None else self.root / PALACE_DIR

    @property
    def chunks_db(self) -> Path:
        return self.palace_store / "index" / "chunks.sqlite"

    def mirror_path(self, entry_id: str) -> Path:
        return self.mirror_root / f"{entry_id}.md"

    @property
    def sealed(self) -> bool:
        """A sealed shelf is a static artifact: read-only until it is unsealed."""
        return self.manifest.mode == "sealed"

    def _refuse_if_sealed(self) -> None:
        self._ensure_open()
        if self.sealed:
            raise SealedShelfError(f"shelf {self.shelf_id} is sealed; unseal it before writing")
        if self._handle.read_only:
            raise SealedShelfError("this shelf handle is read-only")

    def close(self) -> None:
        if self._owns_handle:
            self._handle.close()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise BibliosError("shelf handle is closed")

    def __enter__(self) -> Shelf:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- namespace plumbing -----------------------------------------------------------

    def _live(self) -> dict[str, EntryRecord]:
        """Every live entry record: the whole-sequence read, for whole-tree operations
        only. Castle validates each record as it is read; every page is pinned to one
        revision, so the set is one committed state (a commit between pages refuses it
        and it is read again, bounded)."""
        self._ensure_open()
        # Castle holds this re-entrant lock for the whole of a mutation, so a reader in
        # another thread sees only committed entry sets, never the vacated rows inside one.
        with self._handle.mutex:
            for attempt in range(3):
                probe = self._read("probe", {})
                try:
                    return {
                        change.entry.entry_id: change.entry
                        for change in self._changes(
                            0, probe.database_generation, at_revision=probe.revision
                        )
                        if change.live
                    }
                except NamespaceError as error:
                    if error.code != "namespace-revision-conflict" or attempt == 2:
                        raise
        raise AssertionError("unreachable")

    def _changes(
        self,
        from_revision: int,
        generation: str,
        *,
        at_revision: int | None = None,
        read: Callable[..., Any] | None = None,
    ) -> Iterator[EntryChange]:
        """Every entry record touched at or after ``from_revision`` in ``generation``,
        live or removed, in Castle's (revision, entry) order, one bounded page per read.
        Castle's cursor excludes the (revision, entry) it names, so with no entry it
        starts at that revision. ``read`` replaces the plain read (a counting adapter)."""
        read = read or self._read_result
        after_revision, after_entry = from_revision, None
        while True:
            page = read(
                "changes-since",
                {
                    "database_generation": generation,
                    "after_revision": after_revision,
                    "after_entry_id": after_entry,
                    "limit": CHANGE_PAGE,
                },
                at_revision,
            ).value
            yield from page.changes
            if page.next_revision is None:
                return
            after_revision, after_entry = page.next_revision, page.next_entry_id

    def _chain(
        self, record: EntryRecord, get: Callable[[str], EntryRecord] | None = None
    ) -> dict[str, EntryRecord]:
        """``record`` with its parent chain to the root, each parent read through
        ``get`` (by default one validated ``get-entry``): what a path needs, no more."""
        get = get or (lambda entry_id: self._read("get-entry", {"entry_id": entry_id}))
        records = {record.entry_id: record}
        parent = record.parent_id
        while parent is not None:
            if parent in records:
                raise BibliosError(f"entry {record.entry_id} has a cyclic path")
            records[parent] = get(parent)
            parent = records[parent].parent_id
        return records

    def _entry(self, entry_id: str) -> EntryRecord | None:
        """One live entry record, validated by Castle, or None when no live entry has
        this id."""
        if not isinstance(entry_id, str) or _ENTRY_ID.fullmatch(entry_id) is None:
            return None
        try:
            return self._read("get-entry", {"entry_id": entry_id})
        except NamespaceError as error:
            if error.code == "namespace-entry-missing":
                return None
            raise

    @property
    def revision(self) -> int:
        """The namespace revision: the shelf's authority, which the index follows."""
        return self._read("probe", {}).revision

    def _read(self, kind: str, args: dict[str, Any]) -> Any:
        return self._read_result(kind, args).value

    def _read_result(
        self, kind: str, args: dict[str, Any], at_revision: int | None = None
    ) -> ReadResult:
        self._ensure_open()
        request = ReadRequest(self._namespace_id, self._cas, at_revision, kind, args)
        return read_namespace(self._handle, request)

    def _request_id(self, operation_id: str | None, operation: str, **request: Any) -> str:
        """The Castle operation id for one public request.

        It is derived from the caller's operation id and every semantic argument of the
        request, so the same id with a different request is a different operation and can
        never be answered with a stored outcome it did not earn. Castle's fingerprint then
        binds that id to the exact request it committed.
        """
        if operation_id is None:
            return _uuid()
        canonical = json.dumps(
            {"operation_id": operation_id, "operation": operation, "request": request},
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = bytearray(hashlib.sha256(canonical.encode("utf-8")).digest()[:16])
        digest[6] = (digest[6] & 0x0F) | 0x40
        digest[8] = (digest[8] & 0x3F) | 0x80
        return str(uuid.UUID(bytes=bytes(digest)))

    def _replay(self, request_id: str) -> Mutation | None:
        """Answer a retry through public interfaces only, before any admission work.

        A request this release recorded is re-issued verbatim, so Castle's fingerprint
        check and stored result answer it whatever paths changed since; a tampered
        record fails that check and has no effect. An operation committed before the
        record existed is answered from Castle's public result and its targeted
        operation read, which names the entry the operation acted on from Castle's own
        validated stored request (the new root of a copy, the root of a subtree removal),
        so the answer is exact for every kind this shelf issues, whatever paths changed
        since, and nothing is executed again. An operation with no such entry is
        ``replay-root-unresolved``.
        """
        from biblios.control import ConsumerError
        from biblios.derived import VolatileStore

        self._refuse_if_sealed()
        status = namespace_operation_target(self._handle, request_id)
        if status.state != "committed" or status.result is None:
            return None
        volatile = VolatileStore(self.palace_store)
        try:
            stored = volatile.stored_request(request_id)
        finally:
            volatile.close()
        if stored is not None:
            recorded = json.loads(stored)
            request = MutationRequest(
                recorded["namespace_id"],
                self._cas,
                recorded["expected_revision"],
                request_id,
                recorded["kind"],
                recorded["args"],
            )
            try:
                result = execute_namespace(self._handle, request, {})
            except NamespaceError as error:
                raise ConsumerError("operation-conflict", stage="curation") from error
            return _mutation(result, recorded["args"], replayed=True)
        result = status.result
        if status.primary_entry_id is None:
            raise ConsumerError("replay-root-unresolved", stage="curation")
        return Mutation(
            operation_id=result.operation_id,
            kind=result.kind,
            entry_id=status.primary_entry_id,
            affected_entry_ids=tuple(result.affected_entry_ids),
            revision_before=result.revision_before,
            revision_after=result.revision_after,
            replayed=True,
        )

    def _mutate(
        self,
        kind: str,
        args: dict[str, Any],
        *,
        operation_id: str | None,
        content: dict[str, bytes] | None = None,
    ) -> Mutation:
        # The revision is read and the mutation executed under the handle's re-entrant lock,
        # so a step recording a conversion in another thread cannot slip between the two
        # and turn a valid curation call into a spurious revision conflict.
        with self._handle.mutex:
            self._refuse_if_sealed()
            revision = self._read("info", {}).revision
            request = MutationRequest(
                self._namespace_id, self._cas, revision, operation_id or _uuid(), kind, args
            )
            from biblios.curation import creation_intents
            from biblios.derived import VolatileStore

            needs_live = kind in ("duplicate", "delete-entry", "delete-subtree")
            intents = creation_intents(kind, args, self._live() if needs_live else {})

            # The exact request is recorded inside this mutex hold before Castle is
            # called, so a retry re-issues it verbatim (decision 4, curation replay).
            volatile = VolatileStore(self.palace_store)
            volatile.record_request(
                request.operation_id,
                json.dumps(
                    {
                        "namespace_id": request.namespace_id,
                        "expected_revision": request.expected_revision,
                        "kind": request.kind,
                        "args": request.args,
                    },
                    sort_keys=True,
                ),
            )
            if intents:
                # A creation's positive authority: the intent is written, inside this same
                # mutex hold, before Castle is called; the grant only after it commits.
                volatile.record_intents(request.operation_id, revision, list(intents))
            try:
                result = execute_namespace(self._handle, request, content or {})
            except BaseException:
                volatile.close()
                raise
        try:
            if intents:
                volatile.grant(request.operation_id, result)
        finally:
            volatile.close()
        return _mutation(result, args, replayed=False)

    def _record(self, path: str) -> EntryRecord:
        try:
            return self._read("resolve", {"components": _split(path)})
        except NamespaceError as error:
            if error.code == "namespace-entry-missing":
                raise BibliosError(f"no entry at {path!r}") from error
            raise

    def _parent_and_name(self, path: str) -> tuple[str, str]:
        components = _split(path)
        if not components:
            raise BibliosError("the shelf root cannot be created, replaced or removed")
        parent = self._read("resolve", {"components": components[:-1]})
        if parent.kind != "directory":
            raise BibliosError(f"not a directory: {'/'.join(components[:-1])!r}")
        return parent.entry_id, components[-1]

    def _set_metadata(self, entry_id: str, metadata: EntryMetadata) -> Mutation:
        return self._mutate(
            "set-metadata",
            {"entry_id": entry_id, "metadata": metadata.to_mapping()},
            operation_id=None,
        )

    def _subtree(self, entry_id: str, live: dict[str, EntryRecord]) -> list[str]:
        found = [entry_id]
        frontier = [entry_id]
        while frontier:
            current = frontier.pop()
            children = [e.entry_id for e in live.values() if e.parent_id == current]
            found.extend(children)
            frontier.extend(children)
        return found

    # ---- curation --------------------------------------------------------------------

    def add(
        self,
        path: str,
        data: bytes,
        *,
        media_type: str | None = None,
        tags: Sequence[str] = (),
        dates: dict[str, str] | None = None,
        allow_duplicate: bool = False,
        operation_id: str | None = None,
    ) -> Mutation:
        """Create a file entry at ``path`` holding ``data``.

        Bytes enter Castle once. When live entries already hold the same bytes the call
        raises :class:`DuplicateContentError` naming them unless ``allow_duplicate``. With
        no ``media_type`` the type is sniffed from the bytes by Drawbridge.
        """
        cas_id = _content_id(data)
        request_id = self._request_id(
            operation_id,
            "add",
            path=_split(path),
            cas_id=cas_id,
            media_type=media_type,
            tags=list(tags),
            dates=dict(dates or {}),
            allow_duplicate=allow_duplicate,
        )
        replayed = self._replay(request_id)
        if replayed is not None:
            return replayed
        if not allow_duplicate:
            holders = tuple(sorted(e.entry_id for e in self._live().values() if e.cas_id == cas_id))
            if holders:
                raise DuplicateContentError(holders)
        parent_id, name = self._parent_and_name(path)
        metadata = EntryMetadata(
            media_type=media_type if media_type is not None else _sniff(data, name),
            added_at=_now(),
            tags=tuple(tags),
            dates=dict(dates or {}),
        )
        entry_id = _uuid()
        return self._mutate(
            "create-file",
            {
                "entry_id": entry_id,
                "parent_id": parent_id,
                "name": name,
                "metadata": metadata.to_mapping(),
                "content": {"cas_id": cas_id, "size": len(data)},
            },
            operation_id=request_id,
            content={cas_id: data},
        )

    def mkdir(self, path: str, *, operation_id: str | None = None) -> Mutation:
        request_id = self._request_id(operation_id, "mkdir", path=_split(path))
        replayed = self._replay(request_id)
        if replayed is not None:
            return replayed
        parent_id, name = self._parent_and_name(path)
        return self._mutate(
            "create-directory",
            {
                "entry_id": _uuid(),
                "parent_id": parent_id,
                "name": name,
                "metadata": dict(DIRECTORY_METADATA),
            },
            operation_id=request_id,
        )

    def update(self, path: str, data: bytes, *, operation_id: str | None = None) -> Mutation:
        """Replace an entry's content under the same entry id, name and metadata.

        One ``replace-content`` mutation. Replacement is a new ingestion: the call grants
        the new source's conversion, and the derived record keeps naming the old bytes
        until it runs.
        """
        cas_id = _content_id(data)
        request_id = self._request_id(operation_id, "update", path=_split(path), cas_id=cas_id)
        replayed = self._replay(request_id)
        if replayed is not None:
            return replayed
        record = self._record(path)
        if record.kind != "file":
            raise BibliosError(f"not a file: {path!r}")
        return self._mutate(
            "replace-content",
            {"entry_id": record.entry_id, "content": {"cas_id": cas_id, "size": len(data)}},
            operation_id=request_id,
            content={cas_id: data},
        )

    def rename(self, path: str, name: str, *, operation_id: str | None = None) -> Mutation:
        request_id = self._request_id(operation_id, "rename", path=_split(path), name=name)
        replayed = self._replay(request_id)
        if replayed is not None:
            return replayed
        record = self._record(path)
        if record.parent_id is None:
            raise BibliosError("the shelf root cannot be renamed")
        return self._mutate(
            "relocate",
            {"entry_id": record.entry_id, "parent_id": record.parent_id, "name": name},
            operation_id=request_id,
        )

    def move(self, path: str, destination: str, *, operation_id: str | None = None) -> Mutation:
        """Move an entry to ``destination``, a full path naming its new parent and name."""
        request_id = self._request_id(
            operation_id, "move", path=_split(path), destination=_split(destination)
        )
        replayed = self._replay(request_id)
        if replayed is not None:
            return replayed
        record = self._record(path)
        parent_id, name = self._parent_and_name(destination)
        return self._mutate(
            "relocate",
            {"entry_id": record.entry_id, "parent_id": parent_id, "name": name},
            operation_id=request_id,
        )

    def copy(self, path: str, destination: str, *, operation_id: str | None = None) -> Mutation:
        """Duplicate an entry (and its subtree) under new entry ids at ``destination``.

        The copies carry the source's metadata and content address. The call grants
        each copied file one first mirror, converted from its source, never from an
        edited mirror.
        """
        request_id = self._request_id(
            operation_id, "copy", path=_split(path), destination=_split(destination)
        )
        replayed = self._replay(request_id)
        if replayed is not None:
            return replayed
        record = self._record(path)
        parent_id, name = self._parent_and_name(destination)
        identities = [
            {"source_id": source, "new_id": _uuid()}
            for source in self._subtree(record.entry_id, self._live())
        ]
        return self._mutate(
            "duplicate",
            {
                "entry_id": record.entry_id,
                "parent_id": parent_id,
                "name": name,
                "identities": identities,
            },
            operation_id=request_id,
        )

    def remove(self, path: str, *, operation_id: str | None = None) -> Mutation:
        """Logical deletion: the entry leaves the tree; the bytes stay in Castle.

        The call returns when the namespace commits. The mirror and the index rows are
        derived state and leave at the next reconcile, which drops what no live entry
        names; until then a search refuses the dead entry's hits.
        """
        request_id = self._request_id(operation_id, "remove", path=_split(path))
        replayed = self._replay(request_id)
        if replayed is not None:
            return replayed
        record = self._record(path)
        kind = "delete-subtree" if record.kind == "directory" else "delete-entry"
        return self._mutate(kind, {"entry_id": record.entry_id}, operation_id=request_id)

    # ---- reads -----------------------------------------------------------------------

    def ls(self, path: str = "") -> list[Entry]:
        with self._handle.mutex:
            record = self._record(path)
            if record.kind != "directory":
                raise BibliosError(f"not a directory: {path!r}")
            chain = self._chain(record)
            children = self._read("list-directory", {"entry_id": record.entry_id})
        return [view(child, {**chain, child.entry_id: child}, self._root_id) for child in children]

    def stat(self, path: str) -> Entry:
        with self._handle.mutex:
            record = self._record(path)
            return view(record, self._chain(record), self._root_id)

    def tags(self, path: str) -> tuple[str, ...]:
        entry = self.stat(path)
        if entry.metadata is None:
            raise BibliosError(f"not a file: {path!r}")
        return entry.metadata.tags

    def set_tags(
        self, path: str, tags: Sequence[str], dates: dict[str, str] | None = None
    ) -> Entry:
        with self._handle.mutex:
            self._refuse_if_sealed()
            record = self._record(path)
            if record.kind != "file":
                raise BibliosError(f"not a file: {path!r}")
            metadata = EntryMetadata.from_mapping(record.metadata)
            updated = replace(
                metadata,
                tags=tuple(tags),
                dates=dict(metadata.dates if dates is None else dates),
            )
            self._set_metadata(record.entry_id, updated)
        return self.resolve(record.entry_id)

    def resolve(self, entry_id: str) -> Entry:
        """A citation's entry id back to its current path; refuses a non-live entry."""
        with self._handle.mutex:
            record = self._entry(entry_id)
            if record is None or entry_id == self._root_id:
                raise BibliosError(f"no live entry {entry_id}")
            return view(record, self._chain(record), self._root_id)

    def files(self) -> Iterator[Entry]:
        """Every live file entry."""
        live = self._live()
        for record in live.values():
            if record.kind == "file":
                yield view(record, live, self._root_id)

    def path_of(self, entry_id: str) -> str:
        with self._handle.mutex:
            record = self._entry(entry_id)
            if record is None:
                raise BibliosError(f"no live entry {entry_id}")
            return path_of(entry_id, self._chain(record), self._root_id)

    def index_status(self) -> IndexStatus:
        from biblios.progress import index_status

        return index_status(self)

    # ---- sealing plumbing ---------------------------------------------------------------

    def _snapshot(self) -> tuple[SnapshotRecord, int]:
        """Take a Castle namespace snapshot; the record and the revision it produced."""
        with self._handle.mutex:
            self._refuse_if_sealed()
            request = MutationRequest(
                self._namespace_id,
                self._cas,
                self._read("info", {}).revision,
                _uuid(),
                "snapshot",
                {"snapshot_id": _uuid()},
            )
            result = execute_namespace(self._handle, request, {})
        assert result.snapshot is not None
        return result.snapshot, result.revision_after

    def _reopen_readonly(self) -> None:
        """Release the writable handle and its exclusive lock; hold a shared reader."""
        self._handle.close()
        self._handle = open_namespace_readonly(
            self.namespace_root, self.castle_root, _session(self.root, create=False, read_only=True)
        )

    def seal(self) -> SealRecord:
        from biblios.seal import seal

        return seal(self)

    def verify_seal(self) -> VerifyReport:
        from biblios.seal import verify_seal

        return verify_seal(self)

    # ---- derived layers ----------------------------------------------------------------

    def local_binding(self) -> ShelfBinding:
        """This open shelf as a write-grade binding over its own directories."""
        from biblios.binding import ShelfBinding

        if self._layout is not None:
            return self._layout
        scratch = self.root / "scratch"
        scratch.mkdir(exist_ok=True)
        return ShelfBinding(
            root=self.root,
            namespace_root=self.namespace_root,
            castle_root=self.castle_root,
            mirror_root=self.mirror_root,
            palace_root=self.palace_store,
            scratch_root=scratch,
            shelf_id=self.shelf_id,
            namespace_id=self._namespace_id,
            expected_cas=self._cas,
        )

    def grant_first_ingestion(self, path: str, *, operation_id: str | None = None) -> bool:
        """The explicit first-ingestion grant for the never-converted entry at ``path``,
        bound to its current content; the next :meth:`reconcile` naming it converts it.
        See :func:`biblios.grant_first_ingestion`."""
        import uuid

        from biblios.consumer import BoundShelf

        entry = self.stat(path)
        return BoundShelf(self.local_binding(), self).grant_first_ingestion(
            entry.entry_id,
            expected_cas_id=entry.cas_id,
            operation_id=operation_id or str(uuid.uuid4()),
        )

    def reconcile(
        self,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        progress: Callable[[EntryProgress], None] | None = None,
        should_yield: Callable[[], bool] | None = None,
        entry_ids: Sequence[str] | None = None,
        sweep: bool = False,
        operation_id: str | None = None,
    ) -> BringCurrentResult:
        """Bring the named entries, or the discovered changes, current through the core.

        Discovery covers granted creations, namespace changes and marked entries; a
        direct mirror edit nothing observed is found only by naming the entry or by
        ``sweep``, the explicit whole-shelf form.
        """
        from palace.index.embedder_config import resolve_embedder

        from biblios.consumer import BoundShelf
        from biblios.core import BringCurrentRequest, bring_current
        from biblios.errors import BibliosError
        from biblios.pipeline import ConsumerPipeline
        from biblios.representation import default_policy, drawbridge_converter, envelope_renderer

        if self.sealed:
            raise SealedShelfError("a sealed shelf is read-only; unseal it before reconciling")
        require_identity(self)
        owned = embedder is None
        if embedder is None:
            embedder, _ = resolve_embedder(self.palace_store, purpose="document")
        if not self._step_lock.acquire(blocking=False):
            raise BibliosError(f"a reconcile step is already running on shelf {self.shelf_id}")
        try:
            self._refuse_if_sealed()
            embedder.probe()
            pipeline = ConsumerPipeline(
                converter=drawbridge_converter(settings),
                renderer=envelope_renderer,
                input_view=None,
                policy=default_policy(),
                embedder=embedder,
                progress=progress,
            )
            request = BringCurrentRequest(
                operation_id or _uuid(),
                None if entry_ids is None else tuple(entry_ids),
                sweep=sweep,
            )
            return bring_current(
                BoundShelf(self.local_binding(), self),
                request,
                pipeline=pipeline,
                should_yield=should_yield,
            )
        finally:
            self._step_lock.release()
            if owned:
                embedder.close()

    def verify(self) -> VerifyReport:
        from biblios.verify import verify_shelf

        return verify_shelf(self)

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        rerank: bool = True,
        strict: bool = False,
    ) -> SearchResult:
        from biblios.search import search

        return search(
            self,
            query,
            limit=limit,
            embedder=embedder,
            reranker=reranker,
            rerank=rerank,
            strict=strict,
        )

    def read(self, entry_id: str, *, page: int | None = None) -> Page:
        from biblios.search import read

        return read(self, entry_id, page=page)

    def download(self, entry_id: str) -> bytes:
        from biblios.search import download

        return download(self, entry_id)


def require_identity(shelf: Shelf) -> None:
    """Refuse before any write unless the selector exists and matches a recorded stamp."""
    from palace.index.embedder_config import (
        embedder_config_path,
        identity_for,
        load_embedder_config,
    )
    from palace.index.schema import assert_identity

    from biblios.errors import UndeclaredEmbedderError
    from biblios.progress import read_only_index, store_built

    store = shelf.palace_store
    if not embedder_config_path(store).exists():
        raise UndeclaredEmbedderError(f"no embedder selector declared at {store}")
    if store_built(shelf):
        expected = identity_for(load_embedder_config(store))
        with read_only_index(shelf.chunks_db) as conn:
            assert_identity(conn, expected)


def _session(root: Path, *, create: bool, read_only: bool = False) -> LocalSession:
    return LocalSession(
        root / NAMESPACE_DIR,
        root / CASTLE_DIR,
        root / NAMESPACE_LOCK,
        create_domain_lock=create,
        read_only=read_only,
    )


def init_shelf(root: Path, settings: Settings, *, name: str, description: str = "") -> Shelf:
    """Create a shelf at ``root`` and declare its embedding identity to Palace."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise BibliosError(f"shelf directory is not empty: {root}")
    manifest = ShelfManifest(shelf_id=_uuid(), name=name, description=description)
    manifest.write(root / MANIFEST_NAME)
    castle_root = root / CASTLE_DIR
    castle_root.mkdir()
    ensure_store(castle_root)
    cas.rebuild_index(castle_root)
    (root / MIRROR_DIR).mkdir()
    store = root / PALACE_DIR
    (store / "meta").mkdir(parents=True)
    initialize_store(store, settings.palace)
    namespace_root = root / NAMESPACE_DIR
    namespace_root.mkdir()
    session = _session(root, create=True)
    create_namespace(
        namespace_root,
        castle_root,
        session,
        InitializeRequest(
            namespace_id=_uuid(),
            root_id=_uuid(),
            expected_cas=read_store_identity(castle_root),
            policy={"policy_version": 1, "unicode_data_version": unicodedata.unidata_version},
            protection={"kind": "unprotected"},
        ),
    )
    shelf = open_shelf(root)
    write_watermark(shelf, shelf.revision)
    return shelf


def _open_existing(opener: Callable[..., NamespaceHandle], root: Path, session) -> NamespaceHandle:
    """Open an existing shelf's namespace; a format Castle refuses to open is the typed
    :class:`NamespaceFormatError` naming the operator's explicit migration. Nothing here
    migrates or alters the store."""
    try:
        return opener(root / NAMESPACE_DIR, root / CASTLE_DIR, session)
    except NamespaceError as error:
        if error.code == "namespace-format-unsupported":
            raise NamespaceFormatError(root / NAMESPACE_DIR) from error
        raise


def open_shelf(root: Path) -> Shelf:
    """Open an existing shelf; a sealed one opens read-only through Castle's read-only
    handle; a manifest of another schema, or a sealed manifest without its record, is refused.

    The mode is read before Castle's lock is held, so it is read again once the handle is
    open: a seal or unseal that landed in between changes the mode, and the handle opened
    for the old mode is closed and the open repeated rather than returned over the wrong
    artifact.
    """
    root = Path(root).resolve()
    for _attempt in range(3):
        manifest = ShelfManifest.read(root / MANIFEST_NAME)
        if manifest.mode == "sealed":
            if not (root / SEAL_NAME).is_file():
                raise BibliosError(
                    f"incomplete seal at {root}: the manifest is sealed but {SEAL_NAME} is "
                    "missing; unseal to repair"
                )
            handle = _open_existing(
                open_namespace_readonly, root, _session(root, create=False, read_only=True)
            )
        else:
            handle = _open_existing(open_namespace, root, _session(root, create=False))
        try:
            current = ShelfManifest.read(root / MANIFEST_NAME)
            if current.mode == manifest.mode and (
                current.mode != "sealed" or (root / SEAL_NAME).is_file()
            ):
                return Shelf(root, current, handle)
        except BaseException:
            # The handle holds Castle's lock and nothing else can close it: a refused
            # open must not leave it behind.
            handle.close()
            raise
        handle.close()
    raise BibliosError(f"shelf at {root} kept changing mode while it was being opened")
