"""Seal a shelf into a static artifact, verify one, and unseal it again.

A seal is one critical section under the shelf's handle lock and its step lock, which a
running reconcile step holds for its whole run: the shelf must be dynamic,
identity-checked, built and current (no entry pending, the watermark at the namespace
revision); then a Castle namespace snapshot is taken, the watermark moves to the revision
that snapshot produced, the Palace store is checkpointed and switched to the rollback
journal so no ``-wal`` or ``-shm`` file exists or will be created by a reader, the manifest
is published as sealed with the next generation, and ``seal.json`` is published last with
the snapshot record, the sealed revision and the digests of the files a receiver must prove.
A failure before the manifest is published restores the journal and the watermark; a failure
between the two publications leaves a sealed manifest without a record, which ``open_shelf``
refuses and ``unseal`` repairs; the open shelf is sealed from the manifest's publication
either way, and drops its writable Castle handle for a read-only one, so it refuses every
write and admits other readers.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from castle.namespace import NamespaceError, SnapshotRecord, open_namespace

from biblios.errors import BibliosError, IndexLagError
from biblios.progress import (
    index_status,
    read_only_index,
    read_watermark,
    store_built,
    write_watermark,
)
from biblios.reconcile import require_identity
from biblios.shelf import MANIFEST_NAME, SEAL_NAME, Shelf, ShelfManifest, _session
from biblios.verify import VerifyReport

SEAL_SCHEMA = "biblios.seal/1"
DIGESTED = (
    "shelf.json",
    "palace/index/chunks.sqlite",
    "palace/meta/embedder.toml",
    "palace/watermark.json",
)
_FIELDS = {"schema", "generation", "sealed_at", "snapshot", "sealed_revision", "digests"}
_SNAPSHOT_FIELDS = {"snapshot_id", "cas_id", "size", "captured_revision"}


@dataclass(frozen=True, slots=True)
class SealRecord:
    """What ``seal.json`` says: which snapshot and which bytes a receiver must find."""

    generation: int
    sealed_at: str
    snapshot: SnapshotRecord
    sealed_revision: int
    digests: dict[str, str]
    schema: str = SEAL_SCHEMA

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generation": self.generation,
            "sealed_at": self.sealed_at,
            "snapshot": asdict(self.snapshot),
            "sealed_revision": self.sealed_revision,
            "digests": dict(self.digests),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> SealRecord:
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise BibliosError(f"seal record does not carry the {SEAL_SCHEMA} fields")
        if value["schema"] != SEAL_SCHEMA:
            raise BibliosError(f"unsupported seal schema: {value['schema']!r}")
        snapshot = value["snapshot"]
        if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
            raise BibliosError("seal record carries a malformed snapshot")
        if not isinstance(value["digests"], dict) or set(value["digests"]) != set(DIGESTED):
            raise BibliosError("seal record does not digest the sealed files")
        for name in ("generation", "sealed_revision"):
            if type(value[name]) is not int:
                raise BibliosError(f"seal record {name} is not an integer")
        for name in ("size", "captured_revision"):
            if type(snapshot[name]) is not int:
                raise BibliosError(f"seal record snapshot {name} is not an integer")
        for name in ("snapshot_id", "cas_id"):
            if not isinstance(snapshot[name], str):
                raise BibliosError(f"seal record snapshot {name} is not a string")
        if not isinstance(value["sealed_at"], str):
            raise BibliosError("seal record sealed_at is not a string")
        digests = value["digests"]
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in digests.items()):
            raise BibliosError("seal record digests are not strings")
        return cls(
            generation=value["generation"],
            sealed_at=value["sealed_at"],
            snapshot=SnapshotRecord(**snapshot),
            sealed_revision=value["sealed_revision"],
            digests=dict(digests),
        )

    @classmethod
    def read(cls, path: Path) -> SealRecord:
        try:
            import json

            return cls.from_mapping(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError) as error:
            raise BibliosError(f"seal record unreadable: {path}: {error}") from error


def seal(shelf: Shelf) -> SealRecord:
    """Make the shelf a static artifact; refuses before any write unless it is ready."""
    with shelf._handle.mutex:
        if shelf.sealed:
            raise BibliosError(f"shelf {shelf.shelf_id} is already sealed")
        require_identity(shelf)
        if not store_built(shelf):
            raise BibliosError("the index has never been built; reconcile before sealing")
        status = index_status(shelf)
        if status.lagging:
            raise IndexLagError(
                f"the index lags: {status.pending} pending, revision {status.revision}, "
                f"index at {status.searchable_revision}; reconcile before sealing"
            )
        if status.isolated:
            raise BibliosError(
                f"mirror-states-unsettled: {status.isolated} isolated mirror(s); repair or "
                "update them before sealing"
            )
        staging = shelf.chunks_db.parent / "build-staging"
        if staging.exists() and any(staging.iterdir()):
            raise BibliosError("an interrupted Palace build is staged; reconcile before sealing")
        # A step that already passed its own admission must finish before the transition;
        # one admitted later reads the sealed mode under this lock and refuses.
        if not shelf._step_lock.acquire(blocking=False):
            raise BibliosError("a reconcile step is running; seal after it returns")
        try:
            return _transition(shelf)
        finally:
            shelf._step_lock.release()


def _transition(shelf: Shelf) -> SealRecord:
    """The write half of a seal; the caller holds the handle mutex and the step lock."""
    import json

    previous_watermark = read_watermark(shelf)
    snapshot, sealed_revision = shelf._snapshot()
    manifest_path = shelf.root / MANIFEST_NAME
    record_path = shelf.root / SEAL_NAME
    staged_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
    staged_record = record_path.with_name(record_path.name + ".tmp")
    try:
        write_watermark(shelf, sealed_revision)
        _set_journal(shelf.chunks_db, "delete")
        manifest = replace(shelf.manifest, mode="sealed", generation=shelf.manifest.generation + 1)
        staged_manifest.write_text(manifest.dumps())
        digests = {
            name: _digest(staged_manifest if name == MANIFEST_NAME else shelf.root / name)
            for name in DIGESTED
        }
        record = SealRecord(
            generation=manifest.generation,
            sealed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            snapshot=snapshot,
            sealed_revision=sealed_revision,
            digests=digests,
        )
        staged_record.write_text(json.dumps(record.to_mapping(), indent=2, sort_keys=True) + "\n")
        os.replace(staged_manifest, manifest_path)
    except BaseException:
        # Nothing is published yet: restore the journal and the watermark and leave a
        # dynamic shelf whose only trace of the attempt is one more Castle snapshot.
        staged_manifest.unlink(missing_ok=True)
        staged_record.unlink(missing_ok=True)
        _set_journal(shelf.chunks_db, "wal")
        write_watermark(shelf, previous_watermark)
        raise
    # The manifest is published: from here the open object is sealed too, whatever
    # happens to the record. A failure publishing it leaves an incomplete seal that
    # open_shelf refuses and unseal repairs once this object is closed.
    shelf.manifest = manifest
    try:
        os.replace(staged_record, record_path)
    finally:
        shelf._reopen_readonly()
    return record


def verify_seal(shelf: Shelf) -> VerifyReport:
    """Prove a directory is the artifact that was sealed, wherever it was copied."""
    problems: list[str] = []
    if not shelf.sealed:
        return VerifyReport(ok=False, problems=("shelf is not sealed",))
    record_path = shelf.root / SEAL_NAME
    try:
        record = SealRecord.read(record_path)
    except BibliosError as error:
        return VerifyReport(ok=False, problems=(str(error),))
    for name, expected in record.digests.items():
        path = shelf.root / name
        if not path.is_file():
            problems.append(f"sealed file missing: {name}")
        elif _digest(path) != expected:
            problems.append(f"digest differs from the seal record: {name}")
    revision = shelf.revision
    if revision != record.sealed_revision:
        problems.append(
            f"namespace revision {revision} differs from the sealed revision "
            f"{record.sealed_revision}"
        )
    if record.sealed_revision != record.snapshot.captured_revision + 1:
        problems.append("sealed revision is not one above the snapshot's captured revision")
    try:
        view = shelf._read("inspect-snapshot", {"snapshot_id": record.snapshot.snapshot_id})
    except NamespaceError as error:
        problems.append(f"snapshot absent from the namespace: {error.code}")
    else:
        if view.snapshot != record.snapshot:
            problems.append("namespace snapshot differs from the seal record")
    db = shelf.chunks_db
    if db.is_file():
        try:
            with read_only_index(db) as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error as error:
            problems.append(f"index database unreadable: {error}")
        else:
            if mode != "delete":
                problems.append(f"index journal is {mode!r}, not the rollback journal")
    for sidecar in _sidecars(db):
        problems.append(f"index sidecar present: {sidecar.name}")
    return VerifyReport(ok=not problems, problems=tuple(problems))


def unseal(root: Path) -> Shelf:
    """Return a sealed (or incompletely sealed) shelf to dynamic and open it read-write."""
    root = Path(root).resolve()
    manifest = ShelfManifest.read(root / MANIFEST_NAME)
    if manifest.mode != "sealed":
        raise BibliosError(f"shelf at {root} is not sealed")
    handle = open_namespace(root / "namespace", root / "castle", _session(root, create=False))
    try:
        db = root / "palace" / "index" / "chunks.sqlite"
        if db.is_file():
            _set_journal(db, "wal")
        replace(manifest, mode="dynamic").write(root / MANIFEST_NAME)
        (root / SEAL_NAME).unlink(missing_ok=True)
    except BaseException:
        handle.close()
        raise
    return Shelf(root, ShelfManifest.read(root / MANIFEST_NAME), handle)


def _set_journal(db: Path, mode: str) -> None:
    """Switch the Palace database between WAL and the rollback journal.

    Into ``delete``: a truncating checkpoint must find no other connection busy, and the
    switch must leave no ``-wal`` or ``-shm`` file. Into ``wal``: the mode Palace's writers
    expect.
    """
    connection = sqlite3.connect(db, timeout=5.0, isolation_level=None)
    try:
        if mode == "delete":
            busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise BibliosError("the index is in use; a seal needs it idle")
        (result,) = connection.execute(f"PRAGMA journal_mode={mode.upper()}").fetchone()
    finally:
        connection.close()
    if result != mode:
        raise BibliosError(f"could not switch the index journal to {mode}: {result!r}")
    if mode == "delete" and (left := _sidecars(db)):
        raise BibliosError(f"index sidecars remain after the switch: {[p.name for p in left]}")


def _sidecars(db: Path) -> list[Path]:
    return [
        p for p in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")) if p.exists()
    ]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
