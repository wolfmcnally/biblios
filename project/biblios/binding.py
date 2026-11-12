"""Explicit existing-shelf storage and handle ownership admission.

Callers keep mount and ancestor custody stable for a binding's entire lifetime.
This interface does not sandbox supplied Python providers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from castle.namespace import (
    AuthorizedSession,
    NamespaceError,
    open_namespace,
    open_namespace_readonly,
)
from castle.namespace_store import NamespaceHandle
from castle.store_state import StoreIdentity
from palace.index.s3vectors import BACKEND_SQLITE_VEC, load_vector_backend_config

from biblios.control import ConsumerError, ShelfView
from biblios.shelf import MANIFEST_NAME, Shelf, ShelfManifest

if TYPE_CHECKING:
    from biblios.consumer import BoundShelf


@dataclass(frozen=True, slots=True)
class ShelfBinding:
    """Declared storage domain, actual active epoch and explicit lifetime policy."""

    root: Path
    namespace_root: Path
    castle_root: Path
    mirror_root: Path
    palace_root: Path
    scratch_root: Path
    shelf_id: str
    namespace_id: str
    expected_cas: StoreIdentity
    access: Literal["read-only", "read-write"] = "read-write"
    ownership: Literal["borrowed", "owned"] = "borrowed"

    def validate_paths(self) -> None:
        """Reject overlapping stores and symlinks escaping the admitted domain."""
        if self.access not in {"read-only", "read-write"} or self.ownership not in {
            "borrowed",
            "owned",
        }:
            raise ConsumerError("invalid-binding", stage="binding")
        if not self.root.is_absolute():
            raise ConsumerError("invalid-binding", stage="binding")
        root = self.root.resolve(strict=True)
        admitted = []
        for item in (
            self.namespace_root,
            self.castle_root,
            self.mirror_root,
            self.palace_root,
            self.scratch_root,
        ):
            if not item.is_absolute():
                raise ConsumerError("invalid-binding", stage="binding")
            resolved = item.resolve()
            if not resolved.is_relative_to(root) or resolved == root:
                raise ConsumerError("root-outside-domain", stage="binding")
            if any(
                resolved == other or resolved in other.parents or other in resolved.parents
                for other in admitted
            ):
                raise ConsumerError("overlapping-roots", stage="binding")
            admitted.append(resolved)
        for relative in (
            "meta",
            "index",
            "meta/embedder.toml",
            "meta/vector-backend.toml",
            "meta/index-writer.lock",
            "index/chunks.sqlite",
            "index/chunks.sqlite-wal",
            "index/chunks.sqlite-shm",
            "biblios.sqlite",
            "biblios.sqlite-wal",
            "biblios.sqlite-shm",
            "biblios-volatile.sqlite",
            "biblios-volatile.sqlite-wal",
            "biblios-volatile.sqlite-shm",
            "accepted-ever",
            "consumer-records",
            "consumer-excision.json",
            "watermark.json",
            "consumer-generation.json",
            "consumer-readable.json",
        ):
            if (
                not (self.palace_root / relative)
                .resolve()
                .is_relative_to(self.palace_root.resolve())
            ):
                raise ConsumerError("artifact-outside-domain", stage="binding")
        if (
            not (self.scratch_root / "consumer")
            .resolve()
            .is_relative_to(self.scratch_root.resolve())
        ):
            raise ConsumerError("artifact-outside-domain", stage="binding")


def local_backend(binding: ShelfBinding) -> None:
    """Model injection alone does not constrain Palace's separate vector backend."""
    try:
        selected = load_vector_backend_config(binding.palace_root)
    except Exception as error:
        raise ConsumerError("backend-invalid", stage="binding") from error
    if selected.backend != BACKEND_SQLITE_VEC:
        raise ConsumerError("backend-not-admitted", stage="binding")


def validate_handle(binding: ShelfBinding, handle: NamespaceHandle) -> ShelfView:
    """Re-admit the actual live handle and paths without opening another writer."""
    try:
        binding.validate_paths()
        if handle.closed or handle.failed:
            raise ConsumerError("handle-unavailable", stage="binding", effect="recovery-required")
        if (
            handle.root.resolve() != binding.namespace_root.resolve()
            or handle.cas_root.resolve() != binding.castle_root.resolve()
            or handle.read_only != (binding.access == "read-only")
        ):
            raise ConsumerError("binding-mismatch", stage="binding")
        info = handle.info()
        manifest = ShelfManifest.read(binding.root / MANIFEST_NAME)
        if (
            info.namespace_id != binding.namespace_id
            or info.cas != binding.expected_cas
            or manifest.shelf_id != binding.shelf_id
            or (manifest.mode == "sealed" and binding.access != "read-only")
        ):
            raise ConsumerError("binding-mismatch", stage="binding")
        local_backend(binding)
        disposition = binding.palace_root / "consumer-excision.json"
        if disposition.exists():
            try:
                record = json.loads(disposition.read_text())
                complete = record["state"] == "complete" and record["epoch_id"] == info.cas.epoch_id
            except Exception:
                complete = False
            if not complete:
                raise ConsumerError(
                    "derived-recovery-required", stage="binding", effect="recovery-required"
                )
        checkpoint_file = binding.palace_root / "consumer-readable.json"
        database = binding.palace_root / "index" / "chunks.sqlite"
        if checkpoint_file.is_file():
            checkpoint = json.loads(checkpoint_file.read_text())
            if checkpoint["database_sha256"] is not None and not database.is_file():
                raise ConsumerError("index-missing", stage="binding", effect="recovery-required")
        if binding.access == "read-only":
            if database.is_file():
                # Inspect the header before any SQLite reader can create WAL sidecars.
                with database.open("rb") as stream:
                    header = stream.read(20)
                marker = json.loads((binding.palace_root / "consumer-readable.json").read_text())
                if (
                    header[18:20] != b"\x01\x01"
                    or marker["database_sha256"]
                    != hashlib.sha256(database.read_bytes()).hexdigest()
                ):
                    raise ConsumerError("readable-checkpoint-required", stage="binding")
        return ShelfView(
            binding.shelf_id,
            info.namespace_id,
            info.cas.store_id,
            info.cas.epoch_id,
            info.cas.lineage_id,
            info.cas.generation,
            info.revision,
            info.database_generation,
        )
    except ConsumerError:
        raise
    except NamespaceError as error:
        raise ConsumerError(error.code, stage="binding", effect=error.effect) from error
    except Exception as error:
        raise ConsumerError("binding-unreadable", stage="binding") from error


def bind_shelf(binding: ShelfBinding, handle: NamespaceHandle) -> BoundShelf:
    """Borrow or take ownership of precisely the supplied open namespace handle."""
    from biblios.consumer import BoundShelf

    try:
        validate_handle(binding, handle)
        shelf = Shelf(
            binding.root,
            ShelfManifest.read(binding.root / MANIFEST_NAME),
            handle,
            layout=binding,
            owns_handle=binding.ownership == "owned",
        )
        return BoundShelf(binding, shelf)
    except Exception:
        if binding.ownership == "owned":
            handle.close()
        raise


def open_bound_shelf(binding: ShelfBinding, session: AuthorizedSession) -> BoundShelf:
    """Open existing authority once; never initialize or repair on a reader path.

    After an excision, explicit opening of the declared successor is the consumer's
    instruction to resume. The consumer must finish its own publication first;
    Biblios separately requires completed local derived disposition.

    A namespace Castle refuses to open as an unsupported format is
    ``namespace-format-unsupported``: a shelf written with Castle 0.3 opens only after
    the operator migrates it explicitly with Castle's
    ``castle.namespace_migrate_v1.migrate_namespace_v1``; Biblios never migrates it.
    """
    if binding.ownership != "owned":
        raise ConsumerError("owned-binding-required", stage="binding")
    try:
        binding.validate_paths()
        local_backend(binding)
        opener = open_namespace_readonly if binding.access == "read-only" else open_namespace
        handle = opener(binding.namespace_root, binding.castle_root, session)
    except ConsumerError:
        raise
    except NamespaceError as error:
        raise ConsumerError(error.code, stage="binding", effect=error.effect) from error
    except Exception as error:
        raise ConsumerError("binding-unreadable", stage="binding") from error
    return bind_shelf(binding, handle)
