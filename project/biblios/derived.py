"""Biblios's own indexing state, kept apart from the namespace.

Two SQLite databases live in the palace root. ``biblios.sqlite`` holds accepted
state only: per-entry records, the accepted stages with their exact canonical
bytes, the accepted chunk manifests, preparation checkpoints and the counters and
view value a reader binds. It is the file the readable checkpoint and the seal
digest. ``biblios-volatile.sqlite`` holds everything that changes while work is in
flight: dirty marks, read observations, creation intents and grants, recorded
curation requests, refusal markers and unresolved stages with their rows. It is
never digested or sealed, and a read-only binding never opens it.

Nothing stored here authenticates anything. A caller that authenticates
publication checks a stage's canonical bytes against its own signed record; the
library only checks that what it reports is internally consistent.

Both databases use write-ahead logging with ``synchronous=NORMAL``: durable across
a process crash, which is the authorized threat model. Power loss is a stated
limitation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat as stat_module
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from castle.namespace import canonical_bytes

from biblios.control import ConsumerError

SCHEMA_VERSION = 1
DERIVED_NAME = "biblios.sqlite"
VOLATILE_NAME = "biblios-volatile.sqlite"
ACCEPTED_MARKER = "accepted-ever"


def mark_accepted_ever(palace_root: Path) -> None:
    """Durably record, before the first Palace write of an accepted stage, that this shelf
    may hold accepted state. Monotonic: it is never removed, so a crash after it reads as
    a shelf whose missing index needs recovery, never as one that was never built."""
    marker = palace_root / ACCEPTED_MARKER
    if marker.is_file():
        return
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(palace_root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def accepted_ever(palace_root: Path) -> bool:
    """Whether anything may ever have been accepted here: one stat, no database open, so
    a read-only caller creates no file."""
    return (palace_root / ACCEPTED_MARKER).is_file()


_DERIVED_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS entries (
        entry_id TEXT PRIMARY KEY,
        source_cas_id TEXT,
        conversion TEXT NOT NULL,
        converter TEXT NOT NULL,
        converter_version TEXT NOT NULL,
        mirror_sha256 TEXT NOT NULL,
        stat_json TEXT,
        recorded_ns INTEGER NOT NULL,
        state TEXT NOT NULL,
        refusal TEXT,
        accepted_stage_sha256 TEXT,
        served_seq INTEGER NOT NULL DEFAULT 0,
        indexed_sha256 TEXT,
        indexed_name TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS accepted_stages (
        entry_id TEXT PRIMARY KEY,
        stage_sha256 TEXT NOT NULL,
        canonical BLOB NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS accepted_chunks (
        entry_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        body_sha256 TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        vector_sha256 TEXT NOT NULL,
        PRIMARY KEY (entry_id, chunk_id)
    )""",
    # The accepted stage's membership tree: one row per node, named by the half-open
    # leaf range it covers (2n - 1 rows for n chunks), so a hit's audit path is a keyed
    # read of at most ceil(log2 n) rows. Derived data: the signed root decides.
    """CREATE TABLE IF NOT EXISTS accepted_nodes (
        entry_id TEXT NOT NULL,
        lo INTEGER NOT NULL,
        hi INTEGER NOT NULL,
        hash BLOB NOT NULL,
        PRIMARY KEY (entry_id, lo, hi)
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoints (
        key TEXT PRIMARY KEY,
        entry_id TEXT NOT NULL,
        source_cas_id TEXT NOT NULL,
        document TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS checkpoints_by_source ON checkpoints (source_cas_id)""",
    """CREATE INDEX IF NOT EXISTS entries_by_source ON entries (source_cas_id)""",
    """CREATE INDEX IF NOT EXISTS accepted_by_stage ON accepted_stages (stage_sha256)""",
    """CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        request_sha256 TEXT NOT NULL,
        result TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS cancellations (
        operation_id TEXT PRIMARY KEY,
        request_sha256 TEXT NOT NULL,
        request_canonical BLOB NOT NULL,
        stage_sha256 TEXT NOT NULL UNIQUE,
        stage_canonical BLOB NOT NULL,
        evidence BLOB NOT NULL,
        retained TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cancellation_chain (
        stage_sha256 TEXT NOT NULL,
        position INTEGER NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL UNIQUE,
        request_canonical BLOB NOT NULL,
        evidence BLOB NOT NULL,
        PRIMARY KEY (stage_sha256, position)
    )""",
    """CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS view (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
)

_VOLATILE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS dirty (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id TEXT NOT NULL,
        source TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS dirty_entry ON dirty (entry_id, sequence)""",
    """CREATE TABLE IF NOT EXISTS read_observations (
        entry_id TEXT NOT NULL,
        category TEXT NOT NULL,
        observed TEXT NOT NULL,
        PRIMARY KEY (entry_id, category)
    )""",
    """CREATE TABLE IF NOT EXISTS curation_requests (
        request_id TEXT PRIMARY KEY,
        request TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS creation_intents (
        operation_id TEXT NOT NULL,
        entry_id TEXT NOT NULL,
        role TEXT NOT NULL,
        cas_id TEXT NOT NULL,
        source_id TEXT,
        observed_revision INTEGER NOT NULL,
        PRIMARY KEY (operation_id, entry_id)
    )""",
    """CREATE TABLE IF NOT EXISTS grants (
        entry_id TEXT NOT NULL,
        cas_id TEXT NOT NULL,
        role TEXT NOT NULL,
        operation_id TEXT,
        kind TEXT,
        request_fingerprint TEXT,
        revision_after INTEGER,
        state TEXT NOT NULL,
        intended_mirror_sha256 TEXT,
        PRIMARY KEY (entry_id, cas_id)
    )""",
    """CREATE TABLE IF NOT EXISTS refusals (
        entry_id TEXT NOT NULL,
        mirror_sha256 TEXT NOT NULL,
        derivation_sha256 TEXT NOT NULL,
        code TEXT NOT NULL,
        PRIMARY KEY (entry_id, mirror_sha256, derivation_sha256)
    )""",
    """CREATE TABLE IF NOT EXISTS stages (
        entry_id TEXT PRIMARY KEY,
        stage_sha256 TEXT NOT NULL,
        canonical BLOB NOT NULL,
        state TEXT NOT NULL,
        evidence BLOB,
        promotion BLOB
    )""",
    """CREATE INDEX IF NOT EXISTS stages_by_sha ON stages (stage_sha256)""",
    """CREATE TABLE IF NOT EXISTS stage_resume (
        entry_id TEXT PRIMARY KEY,
        document TEXT NOT NULL,
        data BLOB
    )""",
    """CREATE TABLE IF NOT EXISTS stage_rows (
        entry_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        chunk_id TEXT NOT NULL,
        body TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        vector BLOB NOT NULL,
        PRIMARY KEY (entry_id, position)
    )""",
    """CREATE TABLE IF NOT EXISTS coverage (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
)


GRANULARITY_NS = 2_000_000_000
"""How close a recorded modification time may be to its recording before it is racy."""


@dataclass(frozen=True, slots=True)
class StatIdentity:
    """The stat fields that decide whether a mirror's bytes must be re-read."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def of(cls, path: Path) -> StatIdentity | None:
        """The identity of a regular, non-link file at ``path``, or None when absent."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        return cls.from_result(info)

    @classmethod
    def from_result(cls, info: os.stat_result) -> StatIdentity | None:
        """The identity in one stat result, or None when it is not a regular file."""
        if not stat_module.S_ISREG(info.st_mode):
            return None
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def __iter__(self) -> Iterator[int]:
        return iter((self.device, self.inode, self.size, self.mtime_ns, self.ctime_ns))

    def unchanged(
        self, current: StatIdentity | None, *, recorded_ns: int, granularity_ns: int
    ) -> bool:
        """True only when no stat field changed and the recorded time is not racy.

        A modification time within one granularity of when the record was written
        could hide a same-size rewrite inside one clock tick, so it forces a reread.
        """
        return (
            current == self
            and self.mtime_ns < recorded_ns - granularity_ns
            and self.ctime_ns < recorded_ns - granularity_ns
        )


@dataclass(frozen=True, slots=True)
class DerivedRecord:
    """Biblios's accepted record of one entry: what was indexed, from what, and when.

    ``conversion`` is ``converted`` or ``unconverted:<code>``; ``state`` is ``accepted``,
    ``legacy-verified``, ``legacy-unmatched`` or ``served-rows-damaged``; ``refusal`` is a
    policy class bound to the mirror digest it was found for, as ``<class>:<digest>``.
    """

    entry_id: str
    source_cas_id: str | None
    conversion: str
    converter: str
    converter_version: str
    mirror_sha256: str
    stat: StatIdentity | None
    recorded_ns: int
    state: str
    refusal: str | None = None
    accepted_stage_sha256: str | None = None
    served_seq: int = 0
    indexed_sha256: str | None = None
    """The digest of the bytes Palace indexed for the accepted stage: the mirror's own
    digest, or its in-memory current-filename projection's (the stage's signed rule)."""
    indexed_name: str | None = None
    """The Castle filename the accepted stage's appended filename section carries."""

    @property
    def index_digest(self) -> str:
        """What Palace's file hash must equal for this record."""
        return self.mirror_sha256 if self.indexed_sha256 is None else self.indexed_sha256

    @property
    def converted(self) -> bool:
        return self.conversion == "converted"

    def refused_for(self, mirror_digest: str) -> str | None:
        """The policy class recorded for exactly these mirror bytes, if any."""
        if self.refusal is None:
            return None
        code, _, found = self.refusal.rpartition(":")
        return code if found == mirror_digest else None

    def row(self) -> tuple:
        stat = None if self.stat is None else json.dumps(list(self.stat.__iter__()))
        return (
            self.entry_id,
            self.source_cas_id,
            self.conversion,
            self.converter,
            self.converter_version,
            self.mirror_sha256,
            stat,
            self.recorded_ns,
            self.state,
            self.refusal,
            self.accepted_stage_sha256,
            self.served_seq,
            self.indexed_sha256,
            self.indexed_name,
        )

    @classmethod
    def from_row(cls, row: tuple) -> DerivedRecord:
        stat = None if row[6] is None else StatIdentity(*json.loads(row[6]))
        return cls(row[0], row[1], row[2], row[3], row[4], row[5], stat, *row[7:])


_ENTRY_COLUMNS = (
    "entry_id, source_cas_id, conversion, converter, converter_version, mirror_sha256, "
    "stat_json, recorded_ns, state, refusal, accepted_stage_sha256, served_seq, "
    "indexed_sha256, indexed_name"
)


def _connect(path: Path, schema: tuple[str, ...], *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        if not path.is_file():
            raise ConsumerError("derived-store-missing", stage="read")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        for statement in schema:
            conn.execute(statement)
        version = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if version is None:
            conn.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        elif version[0] != str(SCHEMA_VERSION):
            conn.execute("ROLLBACK")
            raise ConsumerError("derived-store-unsupported", stage="index")
        conn.execute("COMMIT")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One short immediate write transaction; never held across a callback."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class DerivedStore:
    """The digest-bound accepted state: ``palace_root/biblios.sqlite``."""

    def __init__(self, palace_root: Path, *, read_only: bool = False) -> None:
        self.path = palace_root / DERIVED_NAME
        self.read_only = read_only
        self.conn = _connect(self.path, _DERIVED_SCHEMA, read_only=read_only)

    def close(self) -> None:
        self.conn.close()

    def exists(self) -> bool:
        return self.path.is_file()

    def counter(self, key: str) -> int:
        row = self.conn.execute("SELECT value FROM counters WHERE key=?", (key,)).fetchone()
        return 0 if row is None else int(row[0])

    def set_counter(self, key: str, value: int) -> None:
        self.conn.execute(
            "INSERT INTO counters VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def record(self, entry_id: str) -> DerivedRecord | None:
        row = self.conn.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        return None if row is None else DerivedRecord.from_row(row)

    def records(self, entry_ids: list[str]) -> dict[str, DerivedRecord]:
        """The records of exactly the named entries, one keyed lookup each."""
        found: dict[str, DerivedRecord] = {}
        for entry_id in entry_ids:
            record = self.record(entry_id)
            if record is not None:
                found[entry_id] = record
        return found

    def put(self, record: DerivedRecord) -> None:
        """Insert or replace one record; the caller holds the write transaction."""
        self.conn.execute(
            f"INSERT OR REPLACE INTO entries ({_ENTRY_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            record.row(),
        )


def read_derived(palace_root: Path) -> DerivedStore | None:
    """The accepted store opened read-only, or None when there is none yet.

    The first writer creates the file before its schema transaction commits, so the
    file's existence is not the store's: a reader that finds the file but no committed
    schema sees no store, exactly as when the file is absent."""
    if not (palace_root / DERIVED_NAME).is_file():
        return None
    store = DerivedStore(palace_root, read_only=True)
    try:
        ready = (
            store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            is not None
            and store.meta("schema") is not None
        )
    except BaseException:
        store.close()
        raise
    if not ready:
        store.close()
        return None
    return store


class VolatileStore:
    """In-flight state that is never sealed: ``palace_root/biblios-volatile.sqlite``.

    Only a write-grade binding opens it; a read-only binding has no use for marks,
    intents, grants or unresolved stages.
    """

    def __init__(self, palace_root: Path) -> None:
        self.path = palace_root / VOLATILE_NAME
        self.conn = _connect(self.path, _VOLATILE_SCHEMA, read_only=False)

    def close(self) -> None:
        self.conn.close()

    def mark_dirty(self, entry_ids: list[str], *, source: str) -> int:
        """Record a mark per entry; return the highest sequence written."""
        with transaction(self.conn):
            for entry_id in entry_ids:
                self.conn.execute(
                    "INSERT INTO dirty (entry_id, source) VALUES (?, ?)", (entry_id, source)
                )
            row = self.conn.execute("SELECT MAX(sequence) FROM dirty").fetchone()
        return int(row[0] or 0)

    def marked(self) -> tuple[int, list[str]]:
        """The highest mark sequence and the distinct entries marked up to it."""
        row = self.conn.execute("SELECT MAX(sequence) FROM dirty").fetchone()
        ceiling = int(row[0] or 0)
        entries = [
            value
            for (value,) in self.conn.execute(
                "SELECT DISTINCT entry_id FROM dirty WHERE sequence <= ? ORDER BY entry_id",
                (ceiling,),
            )
        ]
        return ceiling, entries

    def record_intents(
        self, operation_id: str, revision: int, intents: list[tuple[str, str, str, str | None]]
    ) -> None:
        """Before the Castle call: ``(entry_id, role, cas_id, source_id)`` per entry."""
        with transaction(self.conn):
            self.conn.executemany(
                "INSERT OR REPLACE INTO creation_intents VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (operation_id, entry_id, role, cas_id, source_id, revision)
                    for entry_id, role, cas_id, source_id in intents
                ],
            )

    def grant(self, operation_id: str, result) -> list[str]:
        """After Castle committed ``result`` to this call: one grant per intent entry the
        result names; an affected id not in the intent receives none."""
        affected = set(result.affected_entry_ids)
        granted: list[str] = []
        with transaction(self.conn):
            rows = self.conn.execute(
                "SELECT entry_id, role, cas_id FROM creation_intents WHERE operation_id=?",
                (operation_id,),
            ).fetchall()
            for entry_id, role, cas_id in rows:
                if entry_id not in affected:
                    continue
                self.conn.execute(
                    # A later grant for the same source replaces the role, but keeps the
                    # digest of a first mirror the core already wrote: those bytes stay
                    # recognizably its own (retained on removal, never the user's).
                    "INSERT INTO grants VALUES (?, ?, ?, ?, ?, ?, ?, 'granted', NULL) "
                    "ON CONFLICT (entry_id, cas_id) DO UPDATE SET role=excluded.role, "
                    "operation_id=excluded.operation_id, kind=excluded.kind, "
                    "request_fingerprint=excluded.request_fingerprint, "
                    "revision_after=excluded.revision_after, state='granted'",
                    (
                        entry_id,
                        cas_id,
                        role,
                        operation_id,
                        result.kind,
                        result.request_fingerprint,
                        result.revision_after,
                    ),
                )
                granted.append(entry_id)
            self.conn.execute("DELETE FROM creation_intents WHERE operation_id=?", (operation_id,))
        return granted

    def record_request(self, operation_id: str, document: str) -> None:
        """The exact canonical request a curation call sends, before it sends it."""
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO curation_requests VALUES (?, ?)", (operation_id, document)
            )

    def stored_request(self, operation_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT request FROM curation_requests WHERE request_id=?", (operation_id,)
        ).fetchone()
        return None if row is None else row[0]

    def clear_marks(self, entry_id: str, *, through: int) -> None:
        """Delete only marks at or below the sequence read before the entry was statted."""
        with transaction(self.conn):
            self.conn.execute(
                "DELETE FROM dirty WHERE entry_id=? AND sequence <= ?", (entry_id, through)
            )


@contextmanager
def opened(
    palace_root: Path, *, read_only: bool
) -> Iterator[tuple[DerivedStore, VolatileStore | None]]:
    """Both stores for a write-grade binding; the accepted store alone for a reader."""
    derived = DerivedStore(palace_root, read_only=read_only)
    with closing(derived.conn):
        if read_only:
            yield derived, None
            return
        volatile = VolatileStore(palace_root)
        with closing(volatile.conn):
            yield derived, volatile


def account(derived: DerivedStore, old: DerivedRecord | None, new: DerivedRecord | None) -> None:
    """Move one entry's contribution to the per-state counters and the served-view
    accumulator from ``old`` to ``new``, inside the caller's transaction."""

    def key(record: DerivedRecord) -> str:
        return "state:" + (record.state if record.converted else "unconverted")

    if old is not None:
        derived.set_counter(key(old), derived.counter(key(old)) - 1)
    if new is not None:
        derived.set_counter(key(new), derived.counter(key(new)) + 1)
    row = derived.conn.execute("SELECT value FROM view WHERE key='accumulator'").fetchone()
    accumulator = int(row[0]) if row is not None else 0
    modulus = 1 << 256
    for record, sign in ((old, -1), (new, 1)):
        if record is not None and record.accepted_stage_sha256 is not None:
            term = int.from_bytes(
                hashlib.sha256(
                    canonical_bytes([record.entry_id, record.accepted_stage_sha256])
                ).digest()
            )
            accumulator = (accumulator + sign * term) % modulus
    derived.conn.execute(
        "INSERT OR REPLACE INTO view VALUES ('accumulator', ?)", (str(accumulator),)
    )
