"""Finite retained-content access and coordinated forward-only excision.

Castle owns durable history, closure and retirement. This module disposes only
library-derived disclosures and never asserts consumer publication or erasure.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
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
        raise ConsumerError(error.code, stage="history", effect=error.effect) from None


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


def _disposition(binding: ShelfBinding, document: dict) -> None:
    """Idempotent derived cleanup, already fenced by the single Palace writer."""
    selected = document["entry_ids"]
    for entry_id in selected:
        for suffix in (".md", ".partial"):
            file = binding.mirror_root / (entry_id + suffix)
            if file.is_symlink() or not file.resolve().is_relative_to(
                binding.mirror_root.resolve()
            ):
                raise ConsumerError(
                    "artifact-outside-domain", stage="history", effect="recovery-required"
                )
            file.unlink(missing_ok=True)
    database = binding.palace_root / "index" / "chunks.sqlite"
    if database.is_file():
        connection = sqlite3.connect(database)
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            for entry_id in selected:
                delete_path(
                    conn=connection,
                    watch_root=str(binding.mirror_root.resolve()),
                    path_str=entry_id + ".md",
                )
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ConsumerError(
                    "checkpoint-failed", stage="history", effect="recovery-required"
                )
            # A live database must not retain selected names/body in SQLite free pages.
            connection.execute("VACUUM")
        finally:
            connection.close()
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


def excise(
    bound: BoundShelf,
    request: ExcisionRequest,
    *,
    control: CallControl = CallControl(),
) -> ExcisionResult:
    with bound._shelf._handle.mutex:
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
        }
        with writer_lock(bound.binding.palace_root, operation="consumer excision"):
            previous = (
                _marker(bound.binding).read_bytes() if _marker(bound.binding).exists() else None
            )
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
                        save_checkpoint(
                            _marker(bound.binding), json.loads(previous), stage="history"
                        )
                raise ConsumerError(error.code, stage="history", effect=error.effect) from None
            except Exception:
                raise ConsumerError(
                    "excision-outcome-unknown", stage="history", effect="uncertain"
                ) from None
            bound._closed = True
            try:
                _disposition(bound.binding, document)
                document.update(state="complete", epoch_id=result.cas_binding.epoch_id)
                save_checkpoint(_marker(bound.binding), document, stage="history")
            except Exception:
                raise ConsumerError(
                    "derived-recovery-required", stage="history", effect="recovery-required"
                ) from None
            control.check("history", effect="namespace-committed")
            return ExcisionResult(result, replace(successor, expected_cas=result.cas_binding))


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
            _disposition(binding, document)
            document.update(state="complete", epoch_id=result.cas_binding.epoch_id)
            save_checkpoint(_marker(binding), document, stage="history")
            return ExcisionResult(result, replace(successor, expected_cas=result.cas_binding))
    except ConsumerError:
        raise
    except NamespaceError as error:
        raise ConsumerError(error.code, stage="history", effect=error.effect) from None
    except Exception:
        raise ConsumerError(
            "excision-recovery-failed", stage="history", effect="recovery-required"
        ) from None
