"""The index as visibly derived state: watermark, per-shelf status and per-entry progress.

The namespace revision is the shelf's authority. Biblios's accepted state lives in its
derived store (:mod:`biblios.derived`), whose counters are maintained in the same
transaction that changes an entry's served identity, so ordinary status costs O(1). The
whole-shelf recount is :func:`diagnose`, the explicit diagnosis that seal, the readable
checkpoint and verification use. The watermark is the revision at which a diagnosis last
found every live entry accepted and current; ordinary status never evaluates it anew.
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

from biblios.errors import BibliosError

if TYPE_CHECKING:
    from biblios.shelf import Shelf

WATERMARK_SCHEMA = "biblios.watermark/1"
WATERMARK_NAME = "watermark.json"
EMBED_SLICE = 16
STAGES = ("stored", "converting", "indexing", "searchable", "unconverted", "withheld")


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
    mirror: str | None = None
    """The mirror's state (see :mod:`biblios.mirrors`) when a conversion is recorded."""
    retained: int = 0
    """Displaced or retired mirror files Biblios keeps for this entry."""


@dataclass(frozen=True, slots=True)
class IndexStatus:
    shelf_id: str
    revision: int
    searchable_revision: int
    lagging: bool
    built: bool
    files: int | None
    searchable: int
    pending: int | None
    unconverted: int
    entries: tuple[EntryProgress, ...]
    """Every live file entry that is not yet searchable (diagnosis only; empty in the
    maintained status)."""
    isolated: int = 0
    """Entries withheld for their own mirror state; they never block the others."""
    # ``files`` and ``pending`` are None when unknown. ``lagging`` is true while the
    # watermark is behind the namespace revision, or a diagnosis found an entry pending.


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


def _accepted(shelf: Shelf) -> dict[str, tuple[str, str]]:
    """Every accepted record's ``(mirror digest, stage digest)``; diagnosis only."""
    from biblios.derived import DERIVED_NAME, DerivedStore

    if not (shelf.palace_store / DERIVED_NAME).is_file():
        return {}
    store = DerivedStore(shelf.palace_store, read_only=True)
    try:
        return {
            entry_id: (mirror, stage or "")
            for entry_id, mirror, stage in store.conn.execute(
                "SELECT entry_id, mirror_sha256, accepted_stage_sha256 FROM entries "
                "WHERE state='accepted' AND conversion='converted'"
            )
        }
    finally:
        store.close()


def _imported(shelf: Shelf) -> bool:
    """Whether the preservation import has run on this shelf (its derived counter)."""
    from biblios.derived import DERIVED_NAME, DerivedStore

    if not (shelf.palace_store / DERIVED_NAME).is_file():
        return False
    store = DerivedStore(shelf.palace_store, read_only=True)
    try:
        return store.counter("legacy-import") > 0
    finally:
        store.close()


def prepare_readable(shelf: Shelf) -> IndexStatus:
    """Writer-only dynamic checkpoint of both accepted stores, without sealing.

    Readers never change journal modes or repair this checkpoint. The caller holds
    Palace writer admission, and no other connection to the accepted store is open.
    An unresolved stage never blocks it: each entry's accepted predecessor is served.
    """
    import hashlib

    from biblios.control import ConsumerError
    from biblios.derived import DERIVED_NAME

    shelf._refuse_if_sealed()
    status, expected, withheld = diagnose(shelf)
    if {k: v for k, v in indexed_files(shelf).items() if k not in withheld} != expected:
        raise ConsumerError("index-membership-mismatch", stage="index")
    derived = shelf.palace_store / DERIVED_NAME
    digests: dict[str, str | None] = {}
    for name, db in (("database_sha256", shelf.chunks_db), ("derived_sha256", derived)):
        if not db.is_file():
            digests[name] = None
            continue
        connection = sqlite3.connect(db)
        try:
            checked = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checked[0] != 0:
                raise ConsumerError("checkpoint-busy", stage="index")
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ConsumerError("checkpoint-failed", stage="index")
        finally:
            connection.close()
        digests[name] = hashlib.sha256(db.read_bytes()).hexdigest()
    marker = shelf.palace_store / "consumer-readable.json"
    document = {
        "revision": status.revision,
        "membership": expected,
        "withheld": withheld,
        "imported": _imported(shelf),
        **digests,
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
    """The maintained counters: O(1), no namespace or mirror enumeration.

    How many live entries were never recorded needs Castle's live-file count, which is
    not yet adopted, so ``files`` and ``pending`` are None (unknown), never zero.
    """
    from biblios.derived import DERIVED_NAME, DerivedStore

    revision = shelf.revision
    counts = {"accepted": 0, "unconverted": 0, "isolated": 0}
    # The revision through which the core last observed and settled every change; until
    # anything ran it is the watermark the shelf was created with.
    searchable_revision = read_watermark(shelf)
    unsettled = 0
    # A seal writes the watermark at the sealed revision after a full diagnosis; that
    # stays the floor whatever the core's own counter observed before the seal's snapshot.
    if (shelf.palace_store / DERIVED_NAME).is_file():
        store = DerivedStore(shelf.palace_store, read_only=True)
        try:
            counts = {key: store.counter("state:" + key) for key in counts}
            counts["isolated"] = store.counter("state:served-rows-damaged")
            observed = store.conn.execute(
                "SELECT value FROM counters WHERE key='observed_revision'"
            ).fetchone()
            if observed is not None:
                searchable_revision = max(searchable_revision, int(observed[0]))
            unsettled = store.counter("unsettled")
        finally:
            store.close()
    built = store_built(shelf)
    # Accepted state without a built index serves nothing: it lags until a sweep
    # re-presents the accepted mirrors.
    lost = not built and counts["accepted"] > 0
    return IndexStatus(
        shelf_id=shelf.shelf_id,
        revision=revision,
        searchable_revision=searchable_revision,
        lagging=searchable_revision < revision or unsettled > 0 or lost,
        built=built,
        files=None,
        searchable=0 if lost else counts["accepted"],
        pending=None,
        unconverted=counts["unconverted"],
        entries=(),
        isolated=counts["isolated"],
    )


def diagnose(shelf: Shelf) -> tuple[IndexStatus, dict[str, str], dict[str, str]]:
    """The explicit whole-shelf recount: O(N) stats, used by seal, checkpoint and verify.

    Returns the exact status, the expected index membership (each searchable entry at
    its accepted mirror digest) and the withheld entries with their states.
    """
    from biblios.derived import DERIVED_NAME, GRANULARITY_NS, DerivedStore
    from biblios.mirrors import classify, lane_for, retained_index

    revision = shelf.revision
    try:
        built = store_built(shelf)
        index = indexed_files(shelf) if built else {}
    except (sqlite3.Error, BibliosError):
        built = False
        # An unusable index serves nothing: every entry reads as pending here, and the
        # caller that needs the reason (verify) reports it from its own read.
        index = {}
    lane = lane_for(shelf.palace_store)
    retained = retained_index(shelf.mirror_root)
    expected: dict[str, str] = {}
    withheld: dict[str, str] = {}
    rows: list[EntryProgress] = []
    files = searchable = pending = unconverted = isolated = 0
    store = (
        DerivedStore(shelf.palace_store, read_only=True)
        if (shelf.palace_store / DERIVED_NAME).is_file()
        else None
    )
    try:
        for entry in shelf.files():
            files += 1
            record = store.record(entry.entry_id) if store is not None else None
            state = classify(
                entry,
                record,
                mirror_path=shelf.mirror_path(entry.entry_id),
                mirror_root=shelf.mirror_root,
                lane=lane,
                granularity_ns=GRANULARITY_NS,
                retained=tuple(retained.get(entry.entry_id, ())),
                store=shelf.palace_store,
            )
            mirror = state.state if state is not None else None
            kept = state.retained if state is not None else 0
            current = record is not None and record.source_cas_id == entry.cas_id
            if current and not record.converted:
                unconverted += 1
                stage = "unconverted"
            elif current and state is not None and state.searchable and record.state == "accepted":
                # Expected membership comes from the accepted record, never from the index
                # itself, so a vanished or altered row is reported rather than absorbed.
                expected[entry.entry_id] = record.index_digest
                if index.get(entry.entry_id) == record.index_digest:
                    searchable += 1
                    continue
                pending += 1
                stage = "stored"
            elif state is not None and state.isolated:
                isolated += 1
                withheld[entry.entry_id] = state.state
                stage = "withheld"
            else:
                pending += 1
                stage = "stored"
            rows.append(
                EntryProgress(
                    shelf.shelf_id, entry.entry_id, entry.path, stage, mirror=mirror, retained=kept
                )
            )
    finally:
        if store is not None:
            store.close()
    searchable_revision = read_watermark(shelf)
    status = IndexStatus(
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
        isolated=isolated,
    )
    return status, expected, withheld


@dataclass(frozen=True, slots=True)
class SettleResult:
    """The outcome of one settle call.

    ``settled``: every store is in rollback-journal mode with no sidecars, durable.
    ``busy``: a reader blocked one store; ``unavailable``: one store could not be opened or
    settled. Either may be partial: ``settled_stores`` names the stores already switched
    before ``store`` stopped the call, and those stay switched. Settling is idempotent, so
    the caller retries the whole call. ``cause`` is the SQLite error that stopped it, when
    one did. ``revision`` is informational and never admission authority.
    """

    state: str
    revision: int
    settled_stores: tuple[str, ...] = ()
    store: str | None = None
    cause: sqlite3.Error | None = None


def _settle_one(db: Path) -> tuple[str, sqlite3.Error | None]:
    try:
        connection = sqlite3.connect(db, timeout=0)
    except sqlite3.Error as error:
        return "unavailable", error
    try:
        checked = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checked[0] != 0:
            return "busy", None
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            return "busy", None
    except sqlite3.OperationalError as error:
        return "busy", error
    except sqlite3.Error as error:
        return "unavailable", error
    finally:
        connection.close()
    if any(db.with_name(db.name + suffix).exists() for suffix in ("-wal", "-shm")):
        return "busy", None
    descriptor = os.open(db.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "settled", None


def settle_for_readers(shelf: Shelf) -> SettleResult:
    """Prepare both accepted stores for no-sidecar read-only opening; the writer only.

    Cost is the write-ahead pages since the last settle, with no membership recount and
    no file hash. The next write-mode open returns each store to write-ahead logging.
    Nothing here is admission authority: a reader's caller-signed evidence decides that.
    """
    from biblios.derived import DERIVED_NAME

    revision = shelf.revision
    done: list[str] = []
    for db in (shelf.chunks_db, shelf.palace_store / DERIVED_NAME):
        if not db.is_file():
            continue
        state, cause = _settle_one(db)
        if state != "settled":
            return SettleResult(state, revision, tuple(done), db.name, cause)
        done.append(db.name)
    return SettleResult("settled", revision, tuple(done))
