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
    EntryRecord,
    InitializeRequest,
    MutationRequest,
    MutationResult,
    NamespaceError,
    ReadRequest,
    SnapshotRecord,
    create_namespace,
    execute_namespace,
    namespace_operation,
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
from biblios.errors import BibliosError, DuplicateContentError, SealedShelfError
from biblios.progress import write_watermark

if TYPE_CHECKING:
    from biblios.progress import EntryProgress, IndexStatus
    from biblios.reconcile import ReconcileReport
    from biblios.seal import SealRecord
    from biblios.search import Page, SearchResult
    from biblios.verify import VerifyReport

SHELF_SCHEMA = "biblios.shelf/1"
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

    def __init__(self, root: Path, manifest: ShelfManifest, handle: NamespaceHandle) -> None:
        self.root = Path(root).resolve()
        self.manifest = manifest
        self._handle = handle
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
        return self.root / CASTLE_DIR

    @property
    def namespace_root(self) -> Path:
        return self.root / NAMESPACE_DIR

    @property
    def mirror_root(self) -> Path:
        return self.root / MIRROR_DIR

    @property
    def palace_store(self) -> Path:
        return self.root / PALACE_DIR

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
        if self.sealed:
            raise SealedShelfError(f"shelf {self.shelf_id} is sealed; unseal it before writing")

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> Shelf:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- namespace plumbing -----------------------------------------------------------

    def _live(self) -> dict[str, EntryRecord]:
        # Castle holds this re-entrant lock for the whole of a mutation, so a reader in
        # another thread sees only committed entry sets, never the vacated rows inside one.
        with self._handle.mutex:
            return self._handle.entries()

    @property
    def revision(self) -> int:
        """The namespace revision: the shelf's authority, which the index follows."""
        return self._read("info", {}).revision

    def _read(self, kind: str, args: dict[str, Any]) -> Any:
        request = ReadRequest(self._namespace_id, self._cas, None, kind, args)
        return read_namespace(self._handle, request).value

    def _stored_request(self, operation_id: str) -> dict[str, Any] | None:
        """Castle's own record of a committed operation's canonical request, or None.

        Castle keeps the canonical request so that a retry can be checked against it;
        Biblios reads that record to re-issue the exact original request, so Castle's
        fingerprint check and stored result answer the retry.
        """
        status = namespace_operation(self._handle, operation_id)
        if status.state != "committed":
            return None
        row = self._handle.db.execute(
            "SELECT canonical_request FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return json.loads(row[0])

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
        """Answer a retry from Castle's record before any admission work.

        The original canonical request is re-issued verbatim, so Castle's fingerprint
        check and its stored result answer the retry, whether or not the paths it named
        still exist.
        """
        self._refuse_if_sealed()
        stored = self._stored_request(request_id)
        if stored is None:
            return None
        args = stored["args"]
        request = MutationRequest(
            stored["namespace_id"],
            self._cas,
            stored["expected_revision"],
            request_id,
            stored["kind"],
            args,
        )
        result = execute_namespace(self._handle, request, {})
        return _mutation(result, args, replayed=True)

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
            request = MutationRequest(
                self._namespace_id,
                self._cas,
                self._read("info", {}).revision,
                operation_id or _uuid(),
                kind,
                args,
            )
            return _mutation(
                execute_namespace(self._handle, request, content or {}), args, replayed=False
            )

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
            display_name=name,
            added_at=_now(),
            tags=tuple(tags),
            dates=dict(dates or {}),
        )
        return self._mutate(
            "create-file",
            {
                "entry_id": _uuid(),
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

        One ``replace-content`` mutation. The recorded conversion keeps naming the old
        bytes, which is how reconcile and verify see that the mirror is stale.
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

        The copies carry the source's metadata and content address; their mirrors and
        chunks are derived by the next reconcile.
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
        record = self._record(path) if _split(path) else self._live()[self._root_id]
        if record.kind != "directory":
            raise BibliosError(f"not a directory: {path!r}")
        children = self._read("list-directory", {"entry_id": record.entry_id})
        live = self._live()
        return [view(child, live, self._root_id) for child in children]

    def stat(self, path: str) -> Entry:
        return view(self._record(path), self._live(), self._root_id)

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
        live = self._live()
        if entry_id not in live or entry_id == self._root_id:
            raise BibliosError(f"no live entry {entry_id}")
        return view(live[entry_id], live, self._root_id)

    def files(self) -> Iterator[Entry]:
        """Every live file entry."""
        live = self._live()
        for record in live.values():
            if record.kind == "file":
                yield view(record, live, self._root_id)

    def path_of(self, entry_id: str) -> str:
        return path_of(entry_id, self._live(), self._root_id)

    def record_conversion(self, entry: Entry, metadata: EntryMetadata) -> Mutation:
        """Write the conversion fields; used by reconcile, which accounts for the revision
        this mutation advances."""
        return self._set_metadata(entry.entry_id, metadata)

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

    def reconcile(
        self,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        progress: Callable[[EntryProgress], None] | None = None,
        should_yield: Callable[[], bool] | None = None,
    ) -> ReconcileReport:
        from biblios.reconcile import reconcile

        return reconcile(
            self, settings, embedder=embedder, progress=progress, should_yield=should_yield
        )

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
    ) -> SearchResult:
        from biblios.search import search

        return search(self, query, limit=limit, embedder=embedder, reranker=reranker, rerank=rerank)

    def read(self, entry_id: str, *, page: int | None = None) -> Page:
        from biblios.search import read

        return read(self, entry_id, page=page)

    def download(self, entry_id: str) -> bytes:
        from biblios.search import download

        return download(self, entry_id)


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
            handle = open_namespace_readonly(
                root / NAMESPACE_DIR,
                root / CASTLE_DIR,
                _session(root, create=False, read_only=True),
            )
        else:
            handle = open_namespace(
                root / NAMESPACE_DIR, root / CASTLE_DIR, _session(root, create=False)
            )
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
