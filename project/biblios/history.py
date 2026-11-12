"""Finite retained-content access and coordinated forward-only excision.

Castle owns durable history, closure and retirement. This module disposes only
library-derived disclosures and never asserts consumer publication or erasure.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import sqlite_vec
from castle import cas
from castle.namespace import (
    AuthorizedSession,
    ExcisionInventory,
    MutationRequest,
    MutationResult,
    NamespaceError,
    ReadRequest,
    RecoveryResult,
    SnapshotView,
    canonical_bytes,
    execute_namespace,
    read_namespace,
    recover_namespace,
)
from palace.index.core import delete_path
from palace.writer_lock import writer_lock

from biblios.binding import ShelfBinding
from biblios.control import CallControl, ConsumerError, save_checkpoint

if TYPE_CHECKING:
    from biblios.consumer import BoundShelf


@dataclass(frozen=True, slots=True)
class ContentReference:
    namespace_id: str
    store_id: str
    epoch_id: str
    owner_kind: str
    owner_id: str
    role: str
    cas_id: str


@dataclass(frozen=True, slots=True)
class ExcisionSelection:
    content_ids: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()
    operation_ids: tuple[str, ...] = ()
    metadata_entry_ids: tuple[str, ...] = ()

    def mapping(self) -> dict[str, list[str]]:
        return {key: sorted(value) for key, value in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class SnapshotAction:
    snapshot_id: str
    action: Literal["drop", "replace"]
    remove_entry_ids: tuple[str, ...] = ()
    clear_metadata_entry_ids: tuple[str, ...] = ()

    def mapping(self) -> dict:
        if (
            self.action == "drop"
            and not self.remove_entry_ids
            and not self.clear_metadata_entry_ids
        ):
            return {"snapshot_id": self.snapshot_id, "action": self.action}
        if self.action == "replace":
            return {
                "snapshot_id": self.snapshot_id,
                "action": self.action,
                "remove_entry_ids": sorted(self.remove_entry_ids),
                "clear_metadata_entry_ids": sorted(self.clear_metadata_entry_ids),
            }
        raise ConsumerError("snapshot-action-invalid", stage="history")


@dataclass(frozen=True, slots=True)
class ExcisionRequest:
    operation_id: str
    expected_revision: int
    selection: ExcisionSelection
    snapshot_actions: tuple[SnapshotAction, ...]
    successor_root: Path


@dataclass(frozen=True, slots=True)
class ExcisionProposal:
    request: ExcisionRequest | None
    entry_ids: tuple[str, ...]
    metadata_entry_ids: tuple[str, ...]
    conflicting_content_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExcisionResult:
    mutation: MutationResult | RecoveryResult
    successor_binding: ShelfBinding
    # Explicit opening remains the consumer's publication/resumption instruction.
    service_resumed: bool = False
    residue: tuple[tuple[str, int], ...] = ()
    """After disposal, how many records of each class Biblios's own stores and files
    still hold for the selection, read back by keyed lookups (every count is expected to
    be zero). An inventory for the caller's own checks: never authority, and no claim
    about bytes outside Biblios's stores or about encoded copies elsewhere.
    ``biblios.identifier-files`` counts the files under the palace and mirror roots that
    still contain a selected entry or content id in any form, expected zero. Recorded
    call results are redacted (operation id and request digest kept, so a changed request
    under the id still refuses); curation and cancellation records that name the
    selection are removed, so re-sending such a request is answered from Castle's own
    record of the operation, never re-executed."""
    kept_retained: tuple[str, ...] = ()
    """Retained mirror files a content selection did not dispose of: edited after the
    selected content was rendered, or with no provenance record. They stay the user's;
    their names are reported, never inferred to derive from the selection."""


def _read(bound: BoundShelf, kind: str, args: dict, revision: int | None = None):
    try:
        return read_namespace(
            bound._shelf._handle,
            ReadRequest(
                bound.binding.namespace_id,
                bound.binding.expected_cas,
                revision,
                kind,
                args,
            ),
        ).value
    except NamespaceError as error:
        raise ConsumerError(error.code, stage="history", effect=error.effect) from error


def inventory(bound: BoundShelf, *, control: CallControl = CallControl()) -> ExcisionInventory:
    return bound._read(lambda: _read(bound, "inventory-excision", {}), control)


def retained(
    bound: BoundShelf, *, control: CallControl = CallControl()
) -> tuple[ContentReference, ...]:
    def collect():
        found = _read(bound, "inventory-excision", {})
        view = bound._admit()
        owners = {(d.owner_kind, d.owner_id, d.role, d.cas_id) for d in found.dependencies}
        owners.update(
            ("entry", row.entry.entry_id, "content", row.entry.cas_id)
            for row in found.entries
            if row.entry.cas_id is not None
        )
        owners.update(
            ("snapshot", row.view.snapshot.snapshot_id, "manifest", row.view.snapshot.cas_id)
            for row in found.snapshots
        )
        return tuple(
            ContentReference(view.namespace_id, view.store_id, view.epoch, *owner)
            for owner in sorted(owners)
        )

    return bound._read(collect, control)


def read_retained(
    bound: BoundShelf,
    reference: ContentReference,
    *,
    control: CallControl = CallControl(),
) -> bytes:
    def read():
        # Membership is re-read, so deletion/retirement never grants arbitrary CAS access.
        current = retained(bound, control=control)
        # Owner fields describe the captured historical occurrence. A replace can
        # move its retention to a snapshot/operation without changing those bytes.
        if not any(
            (item.namespace_id, item.store_id, item.epoch_id, item.cas_id)
            == (
                reference.namespace_id,
                reference.store_id,
                reference.epoch_id,
                reference.cas_id,
            )
            for item in current
        ):
            raise ConsumerError("retained-reference-invalid", stage="history")
        payload = cas.require_object(bound.binding.castle_root, reference.cas_id).read_bytes()
        if "sha256:" + hashlib.sha256(payload).hexdigest() != reference.cas_id:
            raise ConsumerError("content-mismatch", stage="history")
        return payload

    return bound._read(read, control)


def snapshot(
    bound: BoundShelf,
    snapshot_id: str,
    *,
    control: CallControl = CallControl(),
) -> SnapshotView:
    return bound._read(
        lambda: _read(bound, "inspect-snapshot", {"snapshot_id": snapshot_id}), control
    )


def _compile(binding: ShelfBinding, request: ExcisionRequest) -> MutationRequest:
    return MutationRequest(
        binding.namespace_id,
        binding.expected_cas,
        request.expected_revision,
        request.operation_id,
        "excise",
        {
            "selection": request.selection.mapping(),
            "snapshot_actions": sorted(
                (item.mapping() for item in request.snapshot_actions),
                key=lambda item: item["snapshot_id"],
            ),
        },
    )


def _successor(binding: ShelfBinding, request: ExcisionRequest) -> ShelfBinding:
    successor = replace(binding, castle_root=request.successor_root)
    successor.validate_paths()
    if not request.successor_root.is_absolute() or request.successor_root.exists():
        raise ConsumerError("successor-not-absent", stage="history")
    return successor


def preview(bound: BoundShelf, request: ExcisionRequest) -> ExcisionProposal:
    def propose():
        _successor(bound.binding, request)
        compiled = _compile(bound.binding, request)
        value = _read(
            bound,
            "preview-excision",
            {
                "operation_id": request.operation_id,
                **compiled.args,
            },
            request.expected_revision,
        )
        proposed = None
        if value.request is not None:
            proposed = replace(
                request,
                selection=ExcisionSelection(
                    **{key: tuple(value) for key, value in value.request.args["selection"].items()}
                ),
            )
        return ExcisionProposal(
            proposed,
            tuple(value.entry_ids),
            tuple(value.metadata_entry_ids),
            tuple(value.conflicting_content_ids),
        )

    return bound._read(propose, CallControl())


def _marker(binding: ShelfBinding) -> Path:
    return binding.palace_root / "consumer-excision.json"


def _refuse_selected_stages(binding: ShelfBinding, selected, contents, *, effect: str) -> None:
    """Refuse ``stage-pending`` while a persisted stage belongs to a selected entry or
    stages a selected source content: disposing one needs the caller's authenticated
    cancellation, which no artefact here supplies."""
    from biblios.derived import VOLATILE_NAME

    volatile = binding.palace_root / VOLATILE_NAME
    if not (selected or contents) or not volatile.is_file():
        return
    entries, sources = set(selected), set(contents)
    with closing(sqlite3.connect(volatile)) as conn:
        for entry_id, canonical in conn.execute("SELECT entry_id, canonical FROM stages"):
            source = json.loads(bytes(canonical))["entry"]["source_cas_id"]
            if entry_id in entries or source in sources:
                raise ConsumerError("stage-pending", stage="history", effect=effect)


def _dispose_stores(binding: ShelfBinding, selected, contents) -> None:
    """The selected entries' rows in Biblios's own stores (conversion bodies, rendered
    mirrors, accepted stages and chunk digests, grants and marks), then each store is
    compacted. The ledgers are redacted afterwards (:func:`_redact_ledgers`,
    :func:`_scrub_identifiers`); persisted stages were refused before any disposal."""
    from biblios.derived import DERIVED_NAME, VOLATILE_NAME, DerivedStore, account, transaction

    marks = ",".join("?" * len(selected))
    derived_file = binding.palace_root / DERIVED_NAME
    if contents and derived_file.is_file():
        # A selected source's recorded conversion (its body and rendered mirror), under
        # whichever entry recorded it, live or not.
        store = DerivedStore(binding.palace_root, read_only=False)
        with closing(store.conn):
            sources = ",".join("?" * len(contents))
            with transaction(store.conn):
                store.conn.execute(
                    f"DELETE FROM checkpoints WHERE source_cas_id IN ({sources})", tuple(contents)
                )
            _compact(store.conn)
    if selected and derived_file.is_file():
        store = DerivedStore(binding.palace_root, read_only=False)
        with closing(store.conn):
            with transaction(store.conn):
                for record in store.records(list(selected)).values():
                    account(store, record, None)
                for table in (
                    "checkpoints",
                    "accepted_chunks",
                    "accepted_nodes",
                    "accepted_stages",
                ):
                    store.conn.execute(
                        f"DELETE FROM {table} WHERE entry_id IN ({marks})", tuple(selected)
                    )
                store.conn.execute(
                    f"DELETE FROM entries WHERE entry_id IN ({marks})", tuple(selected)
                )
            _compact(store.conn)
    volatile_file = binding.palace_root / VOLATILE_NAME
    if (selected or contents) and volatile_file.is_file():
        with closing(sqlite3.connect(volatile_file, isolation_level=None)) as conn:
            if selected:
                with transaction(conn):
                    for table in (
                        "grants",
                        "dirty",
                        "refusals",
                        "read_observations",
                        "creation_intents",
                    ):
                        conn.execute(
                            f"DELETE FROM {table} WHERE entry_id IN ({marks})", tuple(selected)
                        )
            # Dropped stage rows and resume documents can hold selected bytes in free
            # pages even when no row names the selection now.
            _compact(conn)


def _residue(binding: ShelfBinding, selected, contents) -> tuple[tuple[str, int], ...]:
    """Read back, by keyed lookups over exactly the selection, what each store still
    holds for it. O(selection), no scan."""
    from biblios.derived import DERIVED_NAME, VOLATILE_NAME
    from biblios.mirrors import retained_files

    selected, contents = list(selected), list(contents)
    found: dict[str, int] = {}

    def count(conn, table: str, column: str, values: list[str]) -> int:
        if not values:
            return 0
        marks = ",".join("?" * len(values))
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({marks})", values
        ).fetchone()[0]

    derived = binding.palace_root / DERIVED_NAME
    if derived.is_file():
        with closing(sqlite3.connect(f"file:{derived}?mode=ro", uri=True)) as conn:
            for table in (
                "entries",
                "accepted_stages",
                "accepted_chunks",
                "accepted_nodes",
                "checkpoints",
            ):
                found["derived." + table] = count(conn, table, "entry_id", selected)
            found["derived.checkpoints.source"] = count(
                conn, "checkpoints", "source_cas_id", contents
            )
            # Unselected entries whose record still names a selected content (a source
            # replaced but not yet converted again): keyed by the source index.
            marks = ",".join("?" * len(contents))
            by_source = (
                conn.execute(
                    "SELECT entry_id, mirror_sha256, converter, accepted_stage_sha256 "
                    f"FROM entries WHERE source_cas_id IN ({marks})",
                    contents,
                ).fetchall()
                if contents
                else []
            )
    else:
        by_source = []
    if contents:
        from biblios.mirrors import EDIT_CONVERTER, digest

        found["derived.entries.source-accepted"] = sum(1 for row in by_source if row[3])
        renderings = 0
        for entry_id, mirror_sha256, converter, _accepted in by_source:
            file = binding.mirror_root / (entry_id + ".md")
            if (
                converter != EDIT_CONVERTER
                and file.is_file()
                and not file.is_symlink()
                and digest(file.read_bytes()) == mirror_sha256
            ):
                renderings += 1
        found["mirror.source-renderings"] = renderings
    volatile = binding.palace_root / VOLATILE_NAME
    if volatile.is_file():
        with closing(sqlite3.connect(f"file:{volatile}?mode=ro", uri=True)) as conn:
            for table in (
                "grants",
                "dirty",
                "refusals",
                "read_observations",
                "creation_intents",
                "stages",
            ):
                found["volatile." + table] = count(conn, table, "entry_id", selected)
    database = binding.palace_root / "index" / "chunks.sqlite"
    if database.is_file() and selected:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
            found["palace.files"] = count(conn, "files", "path", [e + ".md" for e in selected])
    if database.is_file() and by_source:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
            found["palace.files.source"] = count(
                conn, "files", "path", [row[0] + ".md" for row in by_source]
            )
    found["mirror.files"] = sum(
        1
        for entry_id in selected
        for name in (entry_id + ".md", entry_id + ".partial")
        if (binding.mirror_root / name).exists()
    ) + sum(len(retained_files(binding.mirror_root, entry_id)) for entry_id in selected)
    found["mirror.retention-records"] = sum(
        1 for entry_id in selected if _records_directory(binding, entry_id).exists()
    )
    return tuple(sorted(found.items()))


def _redact_ledgers(binding: ShelfBinding, selected) -> None:
    """Remove the selected entries from every recorded call result. Each operation keeps
    its id and request digest, so a replay of an old id still refuses a changed request;
    a replayed result is marked ``redacted`` and names no selected entry."""
    from biblios.derived import DERIVED_NAME, DerivedStore, transaction

    names = set(selected)
    if not names or not (binding.palace_root / DERIVED_NAME).is_file():
        return
    store = DerivedStore(binding.palace_root, read_only=False)
    with closing(store.conn):
        rows = store.conn.execute(
            "SELECT operation_id, result FROM operations WHERE result IS NOT NULL"
        ).fetchall()
        with transaction(store.conn):
            for operation_id, document in rows:
                value = json.loads(document)
                entries = [item for item in value["entries"] if item["entry_id"] not in names]
                accepted = [item for item in value["accepted"] if item["entry_id"] not in names]
                if len(entries) == len(value["entries"]) and len(accepted) == len(
                    value["accepted"]
                ):
                    continue
                value.update(entries=entries, accepted=accepted, redacted=True)
                store.conn.execute(
                    "UPDATE operations SET result=? WHERE operation_id=?",
                    (json.dumps(value, sort_keys=True), operation_id),
                )
        _compact(store.conn)


def _needles(selected, contents) -> list[bytes]:
    """Every form a selected identifier is written in: entry ids, and content ids both
    prefixed (``sha256:<hex>``) and bare."""
    found = {str(item).encode() for item in selected}
    for content in contents:
        found.add(str(content).encode())
        found.add(str(content).split(":", 1)[-1].encode())
    return sorted(value for value in found if value)


ID_COLUMNS = {
    # Every column of either store that can hold a selected entry id or content id, and
    # what the excision does with it (``key``: rows are deleted by the selected entry in
    # ``_dispose_stores``; ``scrub``: handled by ``_scrub_identifiers``; ``ledger``:
    # redacted by ``_redact_ledgers``).
    ("derived", "entries", "entry_id"): "key",
    ("derived", "entries", "source_cas_id"): "scrub",
    ("derived", "checkpoints", "entry_id"): "key",
    ("derived", "checkpoints", "source_cas_id"): "key",
    ("derived", "checkpoints", "document"): "key",
    ("derived", "accepted_stages", "entry_id"): "key",
    ("derived", "accepted_stages", "canonical"): "scrub",
    ("derived", "accepted_chunks", "entry_id"): "key",
    ("derived", "accepted_nodes", "entry_id"): "key",
    ("derived", "cancellations", "request_canonical"): "scrub",
    ("derived", "cancellations", "stage_canonical"): "scrub",
    ("derived", "cancellation_chain", "request_canonical"): "scrub",
    ("derived", "operations", "result"): "ledger",
    ("volatile", "grants", "entry_id"): "key",
    ("volatile", "grants", "cas_id"): "scrub",
    ("volatile", "creation_intents", "entry_id"): "key",
    ("volatile", "creation_intents", "cas_id"): "scrub",
    ("volatile", "creation_intents", "source_id"): "scrub",
    ("volatile", "curation_requests", "request"): "scrub",
    ("volatile", "dirty", "entry_id"): "key",
    ("volatile", "read_observations", "entry_id"): "key",
    ("volatile", "refusals", "entry_id"): "key",
    ("volatile", "stages", "entry_id"): "refused",
    ("volatile", "stages", "canonical"): "refused",
    ("volatile", "stage_resume", "entry_id"): "refused",
    ("volatile", "stage_resume", "document"): "refused",
    ("volatile", "stage_rows", "entry_id"): "refused",
}
"""``refused``: excision refuses before any effect while such a stage persists. Every
other column holds no entry or content id; a schema proof checks this inventory is
complete."""

NO_ID_COLUMNS = frozenset(
    {
        ("derived", "accepted_chunks", "body_sha256"),
        ("derived", "accepted_chunks", "chunk_id"),
        ("derived", "accepted_chunks", "input_sha256"),
        ("derived", "accepted_chunks", "position"),
        ("derived", "accepted_chunks", "vector_sha256"),
        ("derived", "accepted_nodes", "hash"),
        ("derived", "accepted_nodes", "hi"),
        ("derived", "accepted_nodes", "lo"),
        ("derived", "accepted_stages", "stage_sha256"),
        ("derived", "cancellation_chain", "evidence"),
        ("derived", "cancellation_chain", "operation_id"),
        ("derived", "cancellation_chain", "position"),
        ("derived", "cancellation_chain", "request_sha256"),
        ("derived", "cancellation_chain", "stage_sha256"),
        ("derived", "cancellations", "evidence"),
        ("derived", "cancellations", "operation_id"),
        ("derived", "cancellations", "request_sha256"),
        ("derived", "cancellations", "retained"),
        ("derived", "cancellations", "stage_sha256"),
        ("derived", "checkpoints", "key"),
        ("derived", "counters", "key"),
        ("derived", "counters", "value"),
        ("derived", "entries", "accepted_stage_sha256"),
        ("derived", "entries", "conversion"),
        ("derived", "entries", "converter"),
        ("derived", "entries", "converter_version"),
        ("derived", "entries", "indexed_name"),
        ("derived", "entries", "indexed_sha256"),
        ("derived", "entries", "mirror_sha256"),
        ("derived", "entries", "recorded_ns"),
        ("derived", "entries", "refusal"),
        ("derived", "entries", "served_seq"),
        ("derived", "entries", "stat_json"),
        ("derived", "entries", "state"),
        ("derived", "meta", "key"),
        ("derived", "meta", "value"),
        ("derived", "operations", "operation_id"),
        ("derived", "operations", "request_sha256"),
        ("derived", "view", "key"),
        ("derived", "view", "value"),
        ("volatile", "coverage", "key"),
        ("volatile", "coverage", "value"),
        ("volatile", "creation_intents", "observed_revision"),
        ("volatile", "creation_intents", "operation_id"),
        ("volatile", "creation_intents", "role"),
        ("volatile", "curation_requests", "request_id"),
        ("volatile", "dirty", "sequence"),
        ("volatile", "dirty", "source"),
        ("volatile", "grants", "intended_mirror_sha256"),
        ("volatile", "grants", "kind"),
        ("volatile", "grants", "operation_id"),
        ("volatile", "grants", "request_fingerprint"),
        ("volatile", "grants", "revision_after"),
        ("volatile", "grants", "role"),
        ("volatile", "grants", "state"),
        ("volatile", "meta", "key"),
        ("volatile", "meta", "value"),
        ("volatile", "read_observations", "category"),
        ("volatile", "read_observations", "observed"),
        ("volatile", "refusals", "code"),
        ("volatile", "refusals", "derivation_sha256"),
        ("volatile", "refusals", "mirror_sha256"),
        ("volatile", "stage_resume", "data"),
        ("volatile", "stage_rows", "body"),
        ("volatile", "stage_rows", "chunk_id"),
        ("volatile", "stage_rows", "input_sha256"),
        ("volatile", "stage_rows", "position"),
        ("volatile", "stage_rows", "vector"),
        ("volatile", "stages", "evidence"),
        ("volatile", "stages", "promotion"),
        ("volatile", "stages", "stage_sha256"),
        ("volatile", "stages", "state"),
    }
)
"""Every other column of either store: digests of bytes, counters, states, positions
and bodies of unselected rows only, never an entry id or content id."""


def _staged_names(canonical: bytes, entries: set[str], sources: set[str]) -> bool:
    """Whether a stage document names a selected entry or source. A document that does
    not parse as a stage is judged by its own bytes, so one damaged row can neither hide
    an identifier nor make the excision unrecoverable."""
    data = bytes(canonical)
    try:
        entry = json.loads(data)["entry"]
        return entry.get("entry_id") in entries or entry.get("source_cas_id") in sources
    except (ValueError, KeyError, TypeError, AttributeError):
        return any(needle in data for needle in _needles(entries, sources))


def _scrub_identifiers(binding: ShelfBinding, selected, contents) -> None:
    """Remove the selected identifiers that the keyed disposal leaves, column by column:
    an unselected entry's record forgets a selected source; an accepted stage, a stage
    cancellation (its receipt and its chain), a grant, a creation intent or a curation
    request that names the selection is removed; a kept retention record forgets a
    selected source. Each action matches only its own column, never a digest elsewhere
    that merely equals a selected one."""
    from biblios.derived import DERIVED_NAME, VOLATILE_NAME, transaction

    entries, sources = set(selected), set(contents)
    if not entries and not sources:
        return
    derived = binding.palace_root / DERIVED_NAME
    if derived.is_file():
        with closing(sqlite3.connect(derived, isolation_level=None)) as conn:
            marks = ",".join("?" * len(sources))
            with transaction(conn):
                if sources:
                    conn.execute(
                        f"UPDATE entries SET source_cas_id=NULL WHERE source_cas_id IN ({marks})",
                        tuple(sources),
                    )
                for entry_id, canonical in conn.execute(
                    "SELECT entry_id, canonical FROM accepted_stages"
                ).fetchall():
                    if _staged_names(canonical, entries, sources):
                        conn.execute("DELETE FROM accepted_stages WHERE entry_id=?", (entry_id,))
                for operation_id, stage_sha256, request, stage in conn.execute(
                    "SELECT operation_id, stage_sha256, request_canonical, stage_canonical "
                    "FROM cancellations"
                ).fetchall():
                    named = _staged_names(stage, entries, sources) or any(
                        entry.encode() in bytes(request) for entry in entries
                    )
                    if named:
                        conn.execute(
                            "DELETE FROM cancellations WHERE operation_id=?", (operation_id,)
                        )
                        conn.execute(
                            "DELETE FROM cancellation_chain WHERE stage_sha256=?", (stage_sha256,)
                        )
                for stage_sha256, position, request in conn.execute(
                    "SELECT stage_sha256, position, request_canonical FROM cancellation_chain"
                ).fetchall():
                    if any(entry.encode() in bytes(request) for entry in entries):
                        conn.execute(
                            "DELETE FROM cancellation_chain WHERE stage_sha256=? AND position=?",
                            (stage_sha256, position),
                        )
            _compact(conn)
    volatile = binding.palace_root / VOLATILE_NAME
    if volatile.is_file():
        with closing(sqlite3.connect(volatile, isolation_level=None)) as conn:
            with transaction(conn):
                if sources:
                    marks = ",".join("?" * len(sources))
                    conn.execute(f"DELETE FROM grants WHERE cas_id IN ({marks})", tuple(sources))
                    conn.execute(
                        f"DELETE FROM creation_intents WHERE cas_id IN ({marks})", tuple(sources)
                    )
                if entries:
                    marks = ",".join("?" * len(entries))
                    conn.execute(
                        f"DELETE FROM creation_intents WHERE source_id IN ({marks})",
                        tuple(entries),
                    )
                needles = _needles(entries, sources)
                for request_id, request in conn.execute(
                    "SELECT request_id, request FROM curation_requests"
                ).fetchall():
                    text = request.encode() if isinstance(request, str) else bytes(request)
                    if any(needle in text for needle in needles):
                        conn.execute(
                            "DELETE FROM curation_requests WHERE request_id=?", (request_id,)
                        )
            _compact(conn)
    from biblios.mirrors import _RETENTIONS

    records = binding.palace_root / _RETENTIONS
    if sources and records.is_dir() and not records.is_symlink():
        for record_file in sorted(records.rglob("*.json")):
            if record_file.is_symlink():
                continue
            record = json.loads(record_file.read_text())
            if isinstance(record, dict) and record.get("source_cas_id") in sources:
                record["source_cas_id"] = None
                save_checkpoint(record_file, record, stage="history")


SCAN_CHUNK = 1 << 20


def _identifier_residue(binding: ShelfBinding, selected, contents) -> int:
    """How many Biblios-managed files under the palace and mirror roots still contain a
    selected identifier in any form, read in bounded chunks (one pass over the bytes;
    the marker is excluded until its redacted write)."""
    needles = _needles(selected, contents)
    if not needles:
        return 0
    overlap = max(len(needle) for needle in needles) - 1
    count = 0
    for root in (binding.palace_root, binding.mirror_root):
        for file in sorted(root.rglob("*")):
            if not file.is_file() or file.is_symlink() or file.name == "consumer-excision.json":
                continue
            tail = b""
            with file.open("rb") as stream:
                while chunk := stream.read(SCAN_CHUNK):
                    window = tail + chunk
                    if any(needle in window for needle in needles):
                        count += 1
                        break
                    tail = window[-overlap:] if overlap else b""
    return count


def _completed_marker(document: dict, epoch_id: str, residue, kept) -> dict:
    """The marker a completed excision keeps: no selected entry or content identifier,
    only what binds a later recovery call to this operation and what it reported."""
    return {
        "state": "complete",
        "operation_id": document["operation_id"],
        "request_sha256": document["request_sha256"],
        "successor_root": document["successor_root"],
        "epoch_id": epoch_id,
        "selected_entries": len(document["entry_ids"]),
        "selected_contents": len(document.get("content_ids", [])),
        "residue": [list(item) for item in residue],
        "kept_retained": list(kept),
    }


def _finish(binding: ShelfBinding, document: dict, epoch_id: str):
    """Dispose, record the kept files, redact the ledgers, read back the keyed residue
    (before the scrub forgets the sources it is keyed on), scrub the remaining
    identifiers, count any identifier still present, then replace the marker with its
    redacted completed form. Until that last write the marker names the selection, so
    an interrupted finish is recovered by repeating these steps."""
    selected = document["entry_ids"]
    contents = document.get("content_ids", [])
    kept = tuple(sorted(set(_disposition(binding, document)) | set(document.get("kept", []))))
    if kept and list(kept) != document.get("kept"):
        # Kept files name only unselected entries; recorded now so a recovery after the
        # scrub (which forgets their selected source) still reports them.
        save_checkpoint(_marker(binding), dict(document, kept=list(kept)), stage="history")
    _redact_ledgers(binding, selected)
    residue = _residue(binding, selected, contents)
    _scrub_identifiers(binding, selected, contents)
    residue += (("biblios.identifier-files", _identifier_residue(binding, selected, contents)),)
    save_checkpoint(
        _marker(binding), _completed_marker(document, epoch_id, residue, kept), stage="history"
    )
    return kept, residue


def _compact(conn: sqlite3.Connection) -> None:
    """No selected bytes may survive in a write-ahead log or in free pages."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
        raise ConsumerError("checkpoint-failed", stage="history", effect="recovery-required")
    conn.execute("VACUUM")


_CLAIM = ".excising"


def _dispose_retained(binding: ShelfBinding, entries, contents) -> tuple[str, ...]:
    """Dispose of the retained mirror files a content selection provably produced, and
    return the names of those kept.

    Only the retention records of ``entries`` (the entries whose recorded conversions
    named a selected content, fixed in the excision marker before any effect) are read:
    no mirror-root scan. A file is disposed of only when its record names a selected
    content and the bytes are exactly both the digest its retained name records and the
    mirror that content's conversion recorded (the core's own unedited rendering).

    The file is first claimed by a no-replace rename to a private name, and only the
    claimed bytes are checked and removed, so bytes written at the retained name after
    the check stay there. An edited file is put back (or, when its name was taken again
    meanwhile, kept under the claim name) and reported. Bytes rewritten in place
    through an already-open descriptor between the check and the removal are outside
    what a rename can fence: arbitrary concurrent edits are not claimed safe.

    Coverage is bounded by the current checkpoint provenance: a retained file whose
    entry no longer has a checkpoint naming a selected content is not found, and is
    neither disposed of nor reported. Nothing scans for it."""
    from biblios.mirrors import _rename, digest

    if not contents:
        return ()
    wanted, kept = set(contents), []
    root = binding.mirror_root.resolve()
    for entry_id in entries:
        records = _records_directory(binding, entry_id)
        if not records.is_dir():
            continue
        for record_file in sorted(records.glob("*.json")):
            record = json.loads(record_file.read_text())
            if record.get("source_cas_id") not in wanted:
                continue
            name = str(record.get("retained", ""))
            file = binding.mirror_root / name
            claim = binding.mirror_root / (name + _CLAIM)
            if "/" in name or not name.startswith(entry_id + ".md"):
                raise ConsumerError(
                    "artifact-outside-domain", stage="history", effect="recovery-required"
                )
            if not claim.exists() and not claim.is_symlink():
                if not file.exists() and not file.is_symlink():
                    record_file.unlink()  # disposed of by an interrupted earlier pass
                    continue
                if file.is_symlink():
                    kept.append(name)
                    continue
                try:
                    _rename(file, claim, "noreplace")
                except FileNotFoundError:
                    continue
            if claim.is_symlink() or not claim.resolve().is_relative_to(root):
                raise ConsumerError(
                    "artifact-outside-domain", stage="history", effect="recovery-required"
                )
            present = digest(claim.read_bytes())
            if present == record.get("mirror_sha256") == record.get("recorded_mirror_sha256"):
                claim.unlink()
                record_file.unlink()
                continue
            try:
                _rename(claim, file, "noreplace")
                kept.append(name)
            except FileExistsError:
                kept.append(claim.name)
    return tuple(sorted(kept))


def _content_entries(binding: ShelfBinding, contents, selected) -> list[str]:
    """The unselected entries whose recorded conversions name a selected content: one
    keyed read of the derived store's checkpoint provenance."""
    from biblios.derived import DERIVED_NAME

    if not contents or not (binding.palace_root / DERIVED_NAME).is_file():
        return []
    marks = ",".join("?" * len(contents))
    with closing(sqlite3.connect(binding.palace_root / DERIVED_NAME)) as conn:
        found = {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT entry_id FROM checkpoints WHERE source_cas_id IN ({marks})",
                list(contents),
            )
        }
    return sorted(found - set(selected))


def _records_directory(binding: ShelfBinding, entry_id: str) -> Path:
    from biblios.mirrors import retention_directory

    directory = retention_directory(binding.palace_root, entry_id)
    if directory.is_symlink():
        raise ConsumerError("artifact-outside-domain", stage="history", effect="recovery-required")
    return directory


def _disposition(binding: ShelfBinding, document: dict) -> tuple[str, ...]:
    """Idempotent derived cleanup, already fenced by the single Palace writer. Returns
    the retained files a content selection kept (see :func:`_dispose_retained`)."""
    selected = document["entry_ids"]
    contents = document.get("content_ids", [])
    _refuse_selected_stages(binding, selected, contents, effect="recovery-required")
    from biblios.mirrors import retained_files, retention_directory

    for entry_id in selected:
        # Explicit excision is the one operation that removes an entry's mirror bodies,
        # including retained displaced versions.
        names = [entry_id + ".md", entry_id + ".partial"]
        names += [file.name for file in retained_files(binding.mirror_root, entry_id)]
        for name in names:
            file = binding.mirror_root / name
            if file.is_symlink() or not file.resolve().is_relative_to(
                binding.mirror_root.resolve()
            ):
                raise ConsumerError(
                    "artifact-outside-domain", stage="history", effect="recovery-required"
                )
            file.unlink(missing_ok=True)
        # Their provenance records name excised content addresses.
        records = retention_directory(binding.palace_root, entry_id)
        if records.is_symlink():
            raise ConsumerError(
                "artifact-outside-domain", stage="history", effect="recovery-required"
            )
        if records.exists():
            shutil.rmtree(records)
    recorded = _recorded_by_content(binding, document.get("retained_entries", []), contents)
    kept_live = _dispose_recorded_mirrors(binding, recorded)
    database = binding.palace_root / "index" / "chunks.sqlite"
    if database.is_file():
        connection = sqlite3.connect(database)
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            for entry_id in [*selected, *recorded]:
                delete_path(
                    conn=connection,
                    watch_root=str(binding.mirror_root.resolve()),
                    path_str=entry_id + ".md",
                )
            # An FTS5 delete only appends a tombstone: the selected words stay in the
            # keyword index's segments, which VACUUM copies as live pages. Merging every
            # segment drops them before the free pages are reclaimed.
            with connection:
                connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ConsumerError(
                    "checkpoint-failed", stage="history", effect="recovery-required"
                )
            # A live database must not retain selected names/body in SQLite free pages.
            connection.execute("VACUUM")
        finally:
            connection.close()
    kept = _dispose_retained(binding, document.get("retained_entries", []), contents)
    _supersede_recorded(binding, recorded)
    _dispose_stores(binding, selected, contents)
    # Checkpoints can contain source-derived material; the new epoch invalidates them.
    for directory in (binding.palace_root / "consumer-records", binding.scratch_root / "consumer"):
        if directory.is_symlink():
            raise ConsumerError(
                "artifact-outside-domain", stage="history", effect="recovery-required"
            )
        if directory.exists():
            shutil.rmtree(directory)
    for name in ("watermark.json", "consumer-generation.json", "consumer-readable.json"):
        (binding.palace_root / name).unlink(missing_ok=True)
    return tuple(sorted({*kept, *kept_live}))


def _recorded_by_content(binding: ShelfBinding, entries, contents) -> dict[str, object]:
    """The unselected entries whose accepted record was produced from a selected content
    (a source replaced but not yet converted again), by keyed reads of ``entries``."""
    from biblios.derived import read_derived

    store = read_derived(binding.palace_root) if contents and entries else None
    if store is None:
        return {}
    with closing(store.conn):
        records = store.records(list(entries))
    wanted = set(contents)
    return {
        entry_id: record for entry_id, record in records.items() if record.source_cas_id in wanted
    }


def _dispose_recorded_mirrors(binding: ShelfBinding, recorded) -> tuple[str, ...]:
    """Dispose of each such entry's live mirror only when it is exactly the core's own
    rendering of the selected content (a converted record, unedited bytes), by the same
    no-replace claim as retained files; an edited or user-written mirror is kept in place
    and reported. Bytes rewritten through an already-open descriptor between the check
    and the removal are outside what a rename can fence."""
    from biblios.mirrors import EDIT_CONVERTER, _rename, digest

    kept = []
    root = binding.mirror_root.resolve()
    for entry_id, record in sorted(recorded.items()):
        name = entry_id + ".md"
        file = binding.mirror_root / name
        claim = binding.mirror_root / (name + _CLAIM)
        if not claim.exists() and not claim.is_symlink():
            if file.is_symlink():
                kept.append(name)
                continue
            if not file.exists():
                continue
            try:
                _rename(file, claim, "noreplace")
            except FileNotFoundError:
                continue
        if claim.is_symlink() or not claim.resolve().is_relative_to(root):
            raise ConsumerError(
                "artifact-outside-domain", stage="history", effect="recovery-required"
            )
        if (
            record.converted
            and record.converter != EDIT_CONVERTER
            and digest(claim.read_bytes()) == record.mirror_sha256
        ):
            claim.unlink()
            continue
        try:
            _rename(claim, file, "noreplace")
            kept.append(name)
        except FileExistsError:
            kept.append(claim.name)
    return tuple(kept)


def _supersede_recorded(binding: ShelfBinding, recorded) -> None:
    """Their Palace rows are gone: the accepted identity is nulled and the chunk digests
    dropped, as a recorded source change does, so nothing claims the deleted rows."""
    from biblios.derived import DerivedStore, account, transaction

    if not recorded:
        return
    store = DerivedStore(binding.palace_root, read_only=False)
    with closing(store.conn):
        with transaction(store.conn):
            for record in store.records(list(recorded)).values():
                new = replace(record, state="superseded", accepted_stage_sha256=None)
                account(store, record, new)
                store.put(new)
                for table in ("accepted_chunks", "accepted_nodes"):
                    store.conn.execute(f"DELETE FROM {table} WHERE entry_id=?", (record.entry_id,))
        _compact(store.conn)


def excise(
    bound: BoundShelf,
    request: ExcisionRequest,
    *,
    control: CallControl = CallControl(),
) -> ExcisionResult:
    with (
        bound._writer("consumer excision", stage="history", control=control),
        bound._shelf._handle.mutex,
    ):
        bound._admit(write=True)
        control.check("history")
        successor = _successor(bound.binding, request)
        proposed = preview(bound, request)
        if proposed.request != request:
            raise ConsumerError("closure-not-accepted", stage="history")
        compiled = _compile(bound.binding, request)
        document = {
            "state": "pending",
            "operation_id": request.operation_id,
            "request_sha256": hashlib.sha256(canonical_bytes(asdict(compiled))).hexdigest(),
            "successor_root": str(request.successor_root),
            "epoch_id": bound.binding.expected_cas.epoch_id,
            "entry_ids": sorted(set(proposed.entry_ids) | set(proposed.metadata_entry_ids)),
            # Exactly the caller's selected source contents: a content selection names no
            # entry when the content is only an earlier version of a live one.
            "content_ids": sorted(request.selection.content_ids),
        }
        # The entries whose recorded conversions name a selected content, fixed before
        # any effect so recovery reads exactly these retention records again.
        document["retained_entries"] = _content_entries(
            bound.binding, document["content_ids"], document["entry_ids"]
        )
        # Fenced before any effect: a persisted stage of a selected entry is never left
        # replayable beside disposed records, and is never cancelled implicitly.
        _refuse_selected_stages(
            bound.binding, document["entry_ids"], document["content_ids"], effect="none"
        )
        previous = _marker(bound.binding).read_bytes() if _marker(bound.binding).exists() else None
        save_checkpoint(_marker(bound.binding), document, stage="history")
        try:
            result = execute_namespace(
                bound._shelf._handle,
                compiled,
                {},
                {
                    "predecessor_root": bound.binding.castle_root,
                    "successor_root": request.successor_root,
                },
            )
        except NamespaceError as error:
            if error.effect == "none":
                if previous is None:
                    _marker(bound.binding).unlink()
                else:
                    save_checkpoint(_marker(bound.binding), json.loads(previous), stage="history")
            raise ConsumerError(error.code, stage="history", effect=error.effect) from error
        except Exception as error:
            raise ConsumerError(
                "excision-outcome-unknown", stage="history", effect="uncertain"
            ) from error
        bound._closed = True
        try:
            kept, residue = _finish(bound.binding, document, result.cas_binding.epoch_id)
        except Exception as error:
            raise ConsumerError(
                "derived-recovery-required", stage="history", effect="recovery-required"
            ) from error
        control.check("history", effect="namespace-committed")
        return ExcisionResult(
            result,
            replace(successor, expected_cas=result.cas_binding),
            residue=residue,
            kept_retained=kept,
        )


def recover_excision(
    binding: ShelfBinding,
    session: AuthorizedSession,
    request: ExcisionRequest,
) -> ExcisionResult:
    """Explicit original roots/token only; no automatic open or predecessor fallback."""
    try:
        binding.validate_paths()
        successor = replace(binding, castle_root=request.successor_root)
        successor.validate_paths()
        document = json.loads(_marker(binding).read_text())
        compiled = _compile(binding, request)
        digest = hashlib.sha256(canonical_bytes(asdict(compiled))).hexdigest()
        if (
            document["operation_id"] != request.operation_id
            or document["request_sha256"] != digest
            or document["successor_root"] != str(request.successor_root)
        ):
            raise ConsumerError("recovery-binding-mismatch", stage="history")
        with writer_lock(binding.palace_root, operation="consumer excision recovery"):
            result = recover_namespace(
                binding.namespace_root,
                binding.castle_root,
                session,
                {
                    "predecessor_root": binding.castle_root,
                    "successor_root": request.successor_root,
                    "operation_id": request.operation_id,
                    "expected_predecessor": binding.expected_cas,
                    "action": "resume",
                },
            )
            if document["state"] == "complete":
                # Already finished and redacted: nothing selected is left to dispose,
                # and the residue it reported is the record's.
                residue = tuple((name, count) for name, count in document["residue"])
                kept: tuple[str, ...] = tuple(document["kept_retained"])
            else:
                kept, residue = _finish(binding, document, result.cas_binding.epoch_id)
            return ExcisionResult(
                result,
                replace(successor, expected_cas=result.cas_binding),
                residue=residue,
                kept_retained=kept,
            )
    except ConsumerError:
        raise
    except NamespaceError as error:
        raise ConsumerError(error.code, stage="history", effect=error.effect) from error
    except Exception as error:
        raise ConsumerError(
            "excision-recovery-failed", stage="history", effect="recovery-required"
        ) from error
