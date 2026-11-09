"""The index as visibly derived state: watermark, per-shelf status and per-entry progress.

The namespace revision is the shelf's authority. The derived side is the mirror set,
Palace's per-file hashes and one shelf-wide watermark, the revision the index last matched
in full. A reader compares the two and says how far the index is behind; a running
reconcile step reports the entry it is converting or indexing through the same record.
Nothing here is cached and nothing about index state is written into entry metadata.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from palace.index.schema import read_identity

from biblios.entries import UNCONVERTED_PREFIX
from biblios.errors import BibliosError

if TYPE_CHECKING:
    from biblios.shelf import Shelf

WATERMARK_SCHEMA = "biblios.watermark/1"
WATERMARK_NAME = "watermark.json"
EMBED_SLICE = 16
STAGES = ("stored", "converting", "indexing", "searchable", "unconverted")


@dataclass(frozen=True, slots=True)
class EntryProgress:
    """One entry's place on the way from stored bytes to a searchable index.

    ``stored`` and ``unconverted`` come from :func:`index_status`; ``converting`` and
    ``indexing`` (with ``done`` and ``total`` counting the chunks Palace embeds for that
    one entry) come only from a running reconcile step's progress callback.
    """

    shelf_id: str
    entry_id: str
    path: str
    stage: str
    done: int | None = None
    total: int | None = None
    eta_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class IndexStatus:
    shelf_id: str
    revision: int
    searchable_revision: int
    lagging: bool
    built: bool
    files: int
    searchable: int
    pending: int
    unconverted: int
    entries: tuple[EntryProgress, ...]
    """Every live file entry that is not yet searchable."""
    # ``lagging`` is true while the watermark is behind the namespace revision or any entry
    # is pending: a store rebuilt from scratch with an old watermark is behind too.


def watermark_path(shelf: Shelf) -> Path:
    return shelf.palace_store / WATERMARK_NAME


def read_watermark(shelf: Shelf) -> int:
    """The revision the index last matched in full; ``-1`` when no watermark exists."""
    path = watermark_path(shelf)
    if not path.is_file():
        return -1
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != WATERMARK_SCHEMA or not isinstance(document.get("revision"), int):
        raise BibliosError(f"unsupported watermark at {path}")
    return document["revision"]


def write_watermark(shelf: Shelf, revision: int) -> None:
    path = watermark_path(shelf)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps({"schema": WATERMARK_SCHEMA, "revision": revision}))
    os.replace(partial, path)


@contextmanager
def read_only_index(db: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to Palace's index, closed on exit whatever happened.

    ``with sqlite3.connect(...)`` only commits or rolls back; it never closes. CPython's
    connection carries a reference cycle through its statement cache, so an unreferenced
    connection stays open until the cyclic collector runs, and a reader that finished long
    ago can still block the seal's switch out of WAL (observed 2026-11-08 as "database is
    locked" at that switch). Every read of the index closes its connection here, explicitly.
    """
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def recorded_identity_incomplete(db: Path) -> bool:
    """A database whose four identity axes are not all recorded was left by an interrupted
    bootstrap: Palace stamps them row by row outside a transaction, so a reader or a crash
    can meet one, two or three of them. Only a complete stamp is an identity."""
    with read_only_index(db) as conn:
        found = read_identity(conn)
    return any(v is None for v in (found.convention, found.model, found.provider, found.dim))


def store_built(shelf: Shelf) -> bool:
    """Palace creates the database before it writes the schema and stamp; only a database
    with a complete stamp is a queryable store."""
    db = shelf.chunks_db
    return db.is_file() and not recorded_identity_incomplete(db)


def _file_rows(shelf: Shelf) -> list[tuple[str, str, str]]:
    """Palace's ``files`` rows ``(path, watch_root, file_hash)``; a shelf store has one root.

    Palace keys files by the absolute mirror root at index time. A sealed shelf copied
    elsewhere still answers by entry id and digest, so the root is read but not used as a
    filter; two roots in one store is a defect, never a silent merge.
    """
    if not store_built(shelf):
        return []
    with read_only_index(shelf.chunks_db) as conn:
        rows = conn.execute("SELECT path, watch_root, file_hash FROM files").fetchall()
    roots = {root for _, root, _ in rows}
    if len(roots) > 1:
        raise BibliosError(f"palace store holds files under more than one root: {sorted(roots)}")
    return rows


def indexed_files(shelf: Shelf) -> dict[str, str]:
    """Palace's own per-file hashes for the shelf's one mirror root: ``{entry_id: sha256}``."""
    return {Path(path).stem: file_hash for path, _, file_hash in _file_rows(shelf)}


def prepare_readable(shelf: Shelf) -> IndexStatus:
    """Writer-only dynamic checkpoint, without sealing or admitting a stale mirror set.

    Readers never change journal modes or repair this checkpoint. The caller
    holds the namespace coordinator; Palace's writer lock covers connection
    creation through its close and checkpoint publication.
    """
    import hashlib

    from palace.writer_lock import writer_lock

    from biblios.control import ConsumerError

    shelf._refuse_if_sealed()
    with writer_lock(shelf.palace_store, operation="consumer readable checkpoint"):
        expected = {}
        for entry in shelf.files():
            if entry.searchable and entry.metadata is not None:
                file = shelf.mirror_path(entry.entry_id)
                if (
                    file.is_symlink()
                    or hashlib.sha256(file.read_bytes()).hexdigest() != entry.metadata.mirror_sha256
                ):
                    raise ConsumerError("mirror-unavailable", stage="index")
                expected[entry.entry_id] = entry.metadata.mirror_sha256
        if indexed_files(shelf) != expected:
            raise ConsumerError("index-membership-mismatch", stage="index")
        if shelf.chunks_db.is_file():
            connection = sqlite3.connect(shelf.chunks_db)
            try:
                checked = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checked[0] != 0:
                    raise ConsumerError("checkpoint-busy", stage="index")
                if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise ConsumerError("checkpoint-failed", stage="index")
            finally:
                connection.close()
        status = index_status(shelf)
        marker = shelf.palace_store / "consumer-readable.json"
        document = {
            "revision": status.revision,
            "membership": expected,
            "database_sha256": hashlib.sha256(shelf.chunks_db.read_bytes()).hexdigest()
            if shelf.chunks_db.is_file()
            else None,
        }
        partial = marker.with_suffix(".partial")
        with partial.open("w") as stream:
            json.dump(document, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, marker)
        return status


def recorded_root(shelf: Shelf) -> str | None:
    """The absolute mirror root Palace recorded, or None for an unbuilt or empty store."""
    rows = _file_rows(shelf)
    return rows[0][1] if rows else None


def index_status(shelf: Shelf) -> IndexStatus:
    """How far the index is behind the namespace, computed from the three layers now."""
    revision = shelf.revision
    searchable_revision = read_watermark(shelf)
    built = store_built(shelf)
    index = indexed_files(shelf) if built else {}
    files = searchable = pending = unconverted = 0
    rows: list[EntryProgress] = []
    for entry in shelf.files():
        files += 1
        metadata = entry.metadata
        current = metadata is not None and metadata.current_for(entry.cas_id)
        if current and metadata.conversion.startswith(UNCONVERTED_PREFIX):
            unconverted += 1
            stage = "unconverted"
        elif current and metadata.converted and index.get(entry.entry_id) == metadata.mirror_sha256:
            searchable += 1
            continue
        else:
            pending += 1
            stage = "stored"
        rows.append(EntryProgress(shelf.shelf_id, entry.entry_id, entry.path, stage))
    return IndexStatus(
        shelf_id=shelf.shelf_id,
        revision=revision,
        searchable_revision=searchable_revision,
        lagging=searchable_revision < revision or pending > 0,
        built=built,
        files=files,
        searchable=searchable,
        pending=pending,
        unconverted=unconverted,
        entries=tuple(rows),
    )
