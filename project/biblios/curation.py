"""Closed consumer requests mapped to Castle's existing atomic operations.

The caller retains one original operation UUID and frozen arguments. Castle's
canonical request/result is the sole durable namespace retry authority.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from castle.namespace import (
    MAX_ENTRIES,
    MutationRequest,
    MutationResult,
    NamespaceError,
    canonical_bytes,
    execute_namespace,
    namespace_operation,
)

from biblios.config import DEFAULT_MAX_FILE_BYTES, Settings
from biblios.control import CallControl, ConsumerError, Observation, observe
from biblios.entries import DIRECTORY_METADATA, EntryMetadata

if TYPE_CHECKING:
    from collections.abc import Callable

    from biblios.consumer import BoundShelf


@dataclass(frozen=True, slots=True)
class CurationLimits:
    files: int = 100
    folders: int = 100
    payload_bytes: int = DEFAULT_MAX_FILE_BYTES
    file_bytes: int = DEFAULT_MAX_FILE_BYTES
    name_bytes: int = 255


LIMITS = CurationLimits()
_MIN_PAYLOAD_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CreateFile:
    parent_id: str
    name: str
    data: bytes
    media_type: str
    added_at: str
    entry_id: str | None = None
    tags: tuple[str, ...] = ()
    dates: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CreateDirectory:
    parent_id: str
    name: str
    entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReplaceContent:
    entry_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class SetEntryMetadata:
    """Explicit descriptive metadata, including after a metadata-only excision."""

    entry_id: str
    media_type: str
    display_name: str
    added_at: str
    tags: tuple[str, ...] = ()
    dates: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RelocateEntry:
    entry_id: str
    parent_id: str
    name: str


@dataclass(frozen=True, slots=True)
class DeleteEntry:
    entry_id: str
    recursive: bool = False


@dataclass(frozen=True, slots=True)
class CopyIdentity:
    source_id: str
    new_id: str


@dataclass(frozen=True, slots=True)
class DuplicateEntry:
    entry_id: str
    parent_id: str
    name: str
    identities: tuple[CopyIdentity, ...]


@dataclass(frozen=True, slots=True)
class ImportItem:
    """A frozen creation record; parents name reserved IDs, not mutable paths."""

    entry_id: str
    parent_id: str
    name: str
    data: bytes | None = None
    media_type: str = ""
    added_at: str = ""
    tags: tuple[str, ...] = ()
    dates: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ImportFolder:
    parent_id: str
    entries: tuple[ImportItem, ...]


@dataclass(frozen=True, slots=True)
class TakeSnapshot:
    snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteSnapshot:
    snapshot_id: str


type CurationAction = (
    CreateFile
    | CreateDirectory
    | ReplaceContent
    | SetEntryMetadata
    | RelocateEntry
    | DeleteEntry
    | DuplicateEntry
    | ImportFolder
    | TakeSnapshot
    | DeleteSnapshot
)


@dataclass(frozen=True, slots=True)
class CurationRequest:
    operation_id: str
    expected_revision: int
    action: CurationAction


@dataclass(frozen=True, slots=True)
class CurationResult:
    operation_id: str
    mutation: MutationResult
    observation: Observation
    replayed: bool


def stable_id(namespace_id: str, operation_id: str, position: str) -> str:
    """Stable v4-shaped reserved identity, as required by Castle's UUID grammar."""
    seed = canonical_bytes([namespace_id, operation_id, position])
    return str(uuid.UUID(bytes=hashlib.sha256(seed).digest()[:16], version=4))


def name(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
        or len(normalized.encode("utf-8")) > LIMITS.name_bytes
    ):
        raise ConsumerError("invalid-name", stage="curation")
    return normalized


def content(data: bytes, limits: CurationLimits) -> dict[str, str | int]:
    if type(data) is not bytes or len(data) > limits.file_bytes:
        raise ConsumerError("payload-limit", stage="curation")
    return {"cas_id": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)}


def metadata(
    media_type: str,
    display_name: str,
    added_at: str,
    tags: tuple[str, ...],
    dates: tuple[tuple[str, str], ...],
) -> dict:
    if not media_type or not added_at or len(dict(dates)) != len(dates):
        raise ConsumerError("metadata-invalid", stage="curation")
    value = EntryMetadata(
        media_type, display_name, added_at, tags=tuple(sorted(set(tags))), dates=dict(dates)
    ).to_mapping()
    EntryMetadata.from_mapping(value)
    return value


def compile_request(
    bound: BoundShelf,
    request: CurationRequest,
    limits: CurationLimits,
) -> tuple[MutationRequest, dict[str, bytes], int]:
    """Normalize the entire immutable request without consulting mutable tree state."""
    action = request.action
    sources: dict[str, bytes] = {}
    additions = 0
    generated = stable_id(bound.binding.namespace_id, request.operation_id, "entry")
    if type(action) is CreateFile:
        spec = content(action.data, limits)
        kind, args = (
            "create-file",
            {
                "entry_id": action.entry_id or generated,
                "parent_id": action.parent_id,
                "name": name(action.name),
                "metadata": metadata(
                    action.media_type, name(action.name), action.added_at, action.tags, action.dates
                ),
                "content": spec,
            },
        )
        sources[str(spec["cas_id"])] = action.data
        additions = 1
    elif type(action) is CreateDirectory:
        kind, args = (
            "create-directory",
            {
                "entry_id": action.entry_id or generated,
                "parent_id": action.parent_id,
                "name": name(action.name),
                "metadata": dict(DIRECTORY_METADATA),
            },
        )
        additions = 1
    elif type(action) is ReplaceContent:
        spec = content(action.data, limits)
        kind, args = "replace-content", {"entry_id": action.entry_id, "content": spec}
        sources[str(spec["cas_id"])] = action.data
    elif type(action) is SetEntryMetadata:
        kind, args = (
            "set-metadata",
            {
                "entry_id": action.entry_id,
                "metadata": metadata(
                    action.media_type,
                    name(action.display_name),
                    action.added_at,
                    action.tags,
                    action.dates,
                ),
            },
        )
    elif type(action) is RelocateEntry:
        kind, args = (
            "relocate",
            {
                "entry_id": action.entry_id,
                "parent_id": action.parent_id,
                "name": name(action.name),
            },
        )
    elif type(action) is DeleteEntry:
        if type(action.recursive) is not bool:
            raise ConsumerError("request-invalid", stage="curation")
        kind = "delete-subtree" if action.recursive else "delete-entry"
        args = {"entry_id": action.entry_id}
    elif type(action) is DuplicateEntry:
        if not action.identities or len(action.identities) > LIMITS.files + LIMITS.folders:
            raise ConsumerError("inventory-limit", stage="curation")
        kind, args = (
            "duplicate",
            {
                "entry_id": action.entry_id,
                "parent_id": action.parent_id,
                "name": name(action.name),
                "identities": sorted(
                    (
                        {"source_id": pair.source_id, "new_id": pair.new_id}
                        for pair in action.identities
                    ),
                    key=lambda pair: pair["source_id"],
                ),
            },
        )
        additions = len(action.identities)
    elif type(action) is ImportFolder:
        if not action.entries or len(action.entries) > LIMITS.files + LIMITS.folders:
            raise ConsumerError("inventory-limit", stage="curation")
        incoming = []
        files = folders = payload = 0
        for item in action.entries:
            row = {"entry_id": item.entry_id, "parent_id": item.parent_id, "name": name(item.name)}
            if item.data is None:
                if item.media_type or item.added_at or item.tags or item.dates:
                    raise ConsumerError("request-invalid", stage="curation")
                folders += 1
                row.update(
                    kind="directory", metadata=dict(DIRECTORY_METADATA), cas_id=None, size=None
                )
            else:
                spec = content(item.data, limits)
                files += 1
                payload += len(item.data)
                row.update(
                    kind="file",
                    metadata=metadata(
                        item.media_type, name(item.name), item.added_at, item.tags, item.dates
                    ),
                    **spec,
                )
                sources[str(spec["cas_id"])] = item.data
            incoming.append(row)
        if files > limits.files or folders > limits.folders or payload > limits.payload_bytes:
            raise ConsumerError("inventory-limit", stage="curation")
        kind, args = (
            "import-folder",
            {
                "parent_id": action.parent_id,
                "entries": sorted(incoming, key=lambda row: row["entry_id"]),
            },
        )
        additions = len(incoming)
    elif type(action) is TakeSnapshot:
        kind, args = "snapshot", {"snapshot_id": action.snapshot_id or generated}
    elif type(action) is DeleteSnapshot:
        kind, args = "delete-snapshot", {"snapshot_id": action.snapshot_id}
    else:
        raise ConsumerError("request-invalid", stage="curation")
    mutation = MutationRequest(
        bound.binding.namespace_id,
        bound.binding.expected_cas,
        request.expected_revision,
        request.operation_id,
        kind,
        args,
    )
    # Public canonical encoding enforces the separate metadata-request limit.
    from dataclasses import asdict

    canonical_bytes(asdict(mutation))
    return mutation, sources, additions


def creation_intents(kind: str, args: dict, live: dict) -> list[tuple[str, str, str, str | None]]:
    """Per entry ``(entry_id, role, cas_id, source_id)`` this call creates, replaces,
    copies or removes: the positive authority indexing later requires."""
    if kind == "create-file":
        return [(args["entry_id"], "created", args["content"]["cas_id"], None)]
    if kind == "replace-content":
        return [(args["entry_id"], "replaced", args["content"]["cas_id"], None)]
    if kind == "import-folder":
        return [
            (row["entry_id"], "created", row["cas_id"], None)
            for row in args["entries"]
            if row["kind"] == "file"
        ]
    if kind == "duplicate":
        return [
            (pair["new_id"], "copied", live[pair["source_id"]].cas_id, pair["source_id"])
            for pair in args["identities"]
            if pair["source_id"] in live and live[pair["source_id"]].kind == "file"
        ]
    if kind in ("delete-entry", "delete-subtree"):
        root = args["entry_id"]
        found, frontier = [], [root]
        while frontier:
            current = frontier.pop()
            found.append(current)
            frontier.extend(e.entry_id for e in live.values() if e.parent_id == current)
        return [
            (entry_id, "removed", live[entry_id].cas_id, None)
            for entry_id in found
            if entry_id in live and live[entry_id].kind == "file"
        ]
    return []


def execute(
    bound: BoundShelf,
    request: CurationRequest,
    *,
    settings: Settings | None = None,
    control: CallControl = CallControl(),
    observer: Callable[[Observation], None] | None = None,
) -> CurationResult:
    """Serialize fresh revision admission and durable replay through one coordinator."""
    handle = bound._shelf._handle
    with handle.mutex:
        bound._admit(write=True)
        control.check("curation")
        dispatched = False
        try:
            file_bytes = settings.max_file_bytes if settings is not None else DEFAULT_MAX_FILE_BYTES
            limits = replace(
                LIMITS,
                file_bytes=file_bytes,
                payload_bytes=max(_MIN_PAYLOAD_BYTES, file_bytes),
            )
            compiled, sources, additions = compile_request(bound, request, limits)
            saved = namespace_operation(handle, request.operation_id)
            replayed = saved.state != "unknown"
            if not replayed:
                if handle.info().revision != request.expected_revision:
                    raise ConsumerError("namespace-revision-conflict", stage="curation")
                live = bound._shelf._live()
                if len(live) + additions > MAX_ENTRIES:
                    raise ConsumerError("inventory-limit", stage="curation")
                if type(request.action) is DuplicateEntry:
                    selected = [live.get(pair.source_id) for pair in request.action.identities]
                    if any(entry is None for entry in selected):
                        raise ConsumerError("namespace-entry-missing", stage="curation")
                    files = [
                        entry for entry in selected if entry is not None and entry.kind == "file"
                    ]
                    if (
                        len(files) > limits.files
                        or len(selected) - len(files) > limits.folders
                        or sum(entry.size or 0 for entry in files) > limits.payload_bytes
                        or any((entry.size or 0) > limits.file_bytes for entry in files)
                    ):
                        raise ConsumerError("inventory-limit", stage="curation")
            volatile = None
            if not replayed:
                intents = creation_intents(compiled.kind, compiled.args, bound._shelf._live())
                if intents:
                    from biblios.derived import VolatileStore

                    # Written inside this mutex hold, before Castle is called.
                    volatile = VolatileStore(bound.binding.palace_root)
                    volatile.record_intents(
                        compiled.operation_id, request.expected_revision, intents
                    )
            dispatched = True
            try:
                result = execute_namespace(handle, compiled, sources)
                if volatile is not None:
                    volatile.grant(compiled.operation_id, result)
            finally:
                if volatile is not None:
                    volatile.close()
        except ConsumerError:
            raise
        except NamespaceError as error:
            raise ConsumerError(error.code, stage="curation", effect=error.effect) from error
        except Exception as error:
            raise ConsumerError(
                "mutation-outcome-unknown" if dispatched else "request-invalid",
                stage="curation",
                effect="uncertain" if dispatched else "none",
            ) from error
        try:
            view = bound._admit()
        except Exception as error:
            # The durable result already returned. Failed completion admission must
            # never invite the caller to submit a replacement operation identity.
            raise ConsumerError(
                "completion-unavailable", stage="curation", effect="namespace-committed"
            ) from error
        observation = Observation(
            request.operation_id,
            "curation",
            "namespace-committed",
            view,
            result.revision_before,
            tuple(result.affected_entry_ids),
            tuple(sorted(set(sources) | ({result.snapshot.cas_id} if result.snapshot else set()))),
            replayed=replayed,
        )
        control.check("curation", effect="namespace-committed")
        observe(observer, observation, control)
        return CurationResult(request.operation_id, result, observation, replayed)
