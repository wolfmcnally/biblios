"""User-owned mirrors: per-entry state, one validator and one non-destructive writer.

A mirror is the user's representation of an entry's bytes. Biblios indexes it, reports
its state per entry and never re-converts or discards it implicitly. Every change to a
mirror path goes through :func:`write_mirror` or :func:`retire_mirror`, which use an
atomic exchange or a no-replace rename, so bytes that Biblios has not verified as the
version it expected are never overwritten or deleted. Displaced and retired bytes are
kept under retained names; nothing here deletes them.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from biblios.control import ConsumerError

if TYPE_CHECKING:
    from biblios.entries import Entry, EntryMetadata
    from biblios.shelf import Shelf

EDIT_CONVERTER = "biblios.mirror-edit"
"""The converter label recorded when the mirror's bytes came from a user, not a converter."""

SEARCHABLE = "searchable"
PENDING_REINDEX = "pending-reindex"
PENDING_CONVERSION = "pending-conversion"
ISOLATED = (
    "mirror-missing",
    "mirror-unicode-invalid",
    "mirror-header-invalid",
    "mirror-too-large",
    "mirror-name-section-invalid",
    "mirror-path-unsafe",
)
POLICY_CLASSES = frozenset(
    {"mirror-too-large", "mirror-name-section-invalid", "mirror-header-invalid"}
)
"""Classes found only against the pipeline's policy (byte bound, name section, declared
required header fields) when a mirror is planned for indexing; persisted per entry and
digest so status reports them without the policy."""

Lane = Literal["envelope", "consumer"]

# Drawbridge's exact front-matter recognition, applied after CR/CRLF normalisation.
_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_RETAINED = ".retained-"
_REFUSALS = "mirror-refusals"
_RETENTIONS = "mirror-retained"


@dataclass(frozen=True, slots=True)
class Provenance:
    """What Biblios knows, from its own records, about bytes it is about to retain.

    Every field that Biblios did not record is None: an unknown is never inferred.
    """

    store: Path
    """The Palace store whose ``mirror-retained`` directory holds the records."""
    reason: str
    """source-replaced, update, rollback, unconverted, removed, stray or staging."""
    source_cas_id: str | None
    """The source the entry's recorded conversion was made from, as Castle holds it."""
    recorded_mirror_sha256: str | None
    """The mirror digest that conversion recorded; differs from the retained digest when
    the user edited the mirror after it was last indexed."""
    operation_id: str | None
    revision: int | None
    """The namespace revision the retaining operation was admitted at."""


@dataclass(frozen=True, slots=True)
class MirrorState:
    """One converted entry's mirror as Biblios finds it now."""

    entry_id: str
    state: str
    digest: str | None
    """The sha256 of the bytes classified, or None when no regular file was read."""
    retained: int = 0
    """Retained displaced or retired files kept for this entry."""
    unattributed: int = 0
    """Retained files with no provenance record (an interruption between the two)."""

    @property
    def searchable(self) -> bool:
        return self.state == SEARCHABLE

    @property
    def isolated(self) -> bool:
        return self.state in ISOLATED


class MirrorInvalid(Exception):
    """Bytes refused by :func:`validate_mirror`; ``code`` is the class."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def lane_for(store: Path) -> Lane:
    """The consumer lane when the store declares a supplied representation."""
    from palace.index.embedder_config import embedder_config_path, load_embedder_config

    if not embedder_config_path(store).exists():
        return "envelope"
    return "consumer" if load_embedder_config(store).representation is not None else "envelope"


def _normalised(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_mirror(
    data: bytes,
    *,
    lane: Lane,
    max_bytes: int | None = None,
    required_header_fields: tuple[str, ...] = (),
) -> str:
    """Return the text of valid mirror bytes, or raise :class:`MirrorInvalid`.

    Checks strict UTF-8, the byte bound and the lane's header. The envelope lane uses
    Drawbridge's own parser unchanged. The consumer lane imposes no schema: a recognised
    front-matter block must load as a mapping and carry the consumer's declared fields.
    """
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise MirrorInvalid("mirror-unicode-invalid") from None
    if max_bytes is not None and len(data) > max_bytes:
        raise MirrorInvalid("mirror-too-large")
    if lane == "envelope":
        from drawbridge.mirror import parse_mirror_header

        try:
            parse_mirror_header(text)
        except Exception:
            raise MirrorInvalid("mirror-header-invalid") from None
        return text
    match = _FRONT_MATTER.match(_normalised(text))
    if match is None:
        if required_header_fields:
            raise MirrorInvalid("mirror-header-invalid")
        return text
    import yaml

    try:
        header = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        raise MirrorInvalid("mirror-header-invalid") from None
    if header is None:
        header = {}
    if not isinstance(header, dict) or any(key not in header for key in required_header_fields):
        raise MirrorInvalid("mirror-header-invalid")
    return text


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_mirror(path: Path, root: Path) -> bytes | None:
    """The regular file's bytes at ``path``, or None when absent.

    Raises :class:`MirrorInvalid` with ``mirror-path-unsafe`` for a symlink or a path
    resolving outside ``root``; such a file is never followed.
    """
    if path.is_symlink():
        raise MirrorInvalid("mirror-path-unsafe")
    if not path.exists():
        return None
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise MirrorInvalid("mirror-path-unsafe")
    return path.read_bytes()


def retained_files(root: Path, entry_id: str) -> list[Path]:
    return sorted(root.glob(f"{entry_id}.md{_RETAINED}*"))


def retained_index(root: Path) -> dict[str, list[Path]]:
    """Every retained file under ``root`` by entry id, from one directory listing."""
    found: dict[str, list[Path]] = {}
    if not root.is_dir():
        return found
    for file in sorted(root.iterdir()):
        entry_id, separator, _ = file.name.partition(".md" + _RETAINED)
        if separator:
            found.setdefault(entry_id, []).append(file)
    return found


def retained_summary(store: Path, root: Path) -> tuple[int, int]:
    """Shelf-wide ``(retained, unattributed)`` counts, whether or not the entry is live."""
    files = [file for group in retained_index(root).values() for file in group]
    return len(files), _unattributed(store, files)


def retention_directory(store: Path, entry_id: str) -> Path:
    return store / _RETENTIONS / entry_id


def retention_record(store: Path, retained: Path) -> dict | None:
    """The provenance record of one retained file, or None when none was written."""
    entry_id = retained.name.split(".md" + _RETAINED, 1)[0]
    path = retention_directory(store, entry_id) / (retained.name + ".json")
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _unattributed(store: Path, files: list[Path]) -> int:
    return sum(1 for file in files if retention_record(store, file) is None)


def _record_retention(target: Path, entry_id: str, provenance: Provenance) -> None:
    """Write the record after the no-replace retention it describes.

    The bytes are kept first; an interruption before this write leaves a retained file
    with no record, which status reports as unattributed and nothing infers.
    """
    directory = retention_directory(provenance.store, entry_id)
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": "biblios.mirror-retained/1",
        "entry_id": entry_id,
        "retained": target.name,
        "mirror_sha256": target.name.rsplit("-", 1)[1],
        "source_cas_id": provenance.source_cas_id,
        "recorded_mirror_sha256": provenance.recorded_mirror_sha256,
        "reason": provenance.reason,
        "operation_id": provenance.operation_id,
        "revision": provenance.revision,
    }
    path = directory / (target.name + ".json")
    partial = path.with_suffix(".tmp")
    with partial.open("w") as stream:
        json.dump(document, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)


def _refusal_path(store: Path, entry_id: str) -> Path:
    return store / _REFUSALS / f"{entry_id}.json"


def record_refusal(store: Path, entry_id: str, file_digest: str, code: str) -> None:
    """Persist a policy-dependent refusal for exactly these bytes (writer only)."""
    path = _refusal_path(store, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps({"digest": file_digest, "class": code}, sort_keys=True))
    os.replace(partial, path)


def clear_refusal(store: Path, entry_id: str) -> None:
    _refusal_path(store, entry_id).unlink(missing_ok=True)


def refusal_for(store: Path, entry_id: str, file_digest: str) -> str | None:
    path = _refusal_path(store, entry_id)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        isinstance(document, dict)
        and document.get("digest") == file_digest
        and document.get("class") in POLICY_CLASSES
    ):
        return document["class"]
    return None


_INTENTS = "copy-intents"


def record_copy_intent(store: Path, operation_id: str) -> None:
    """Record, before a copy commits, that this code created the copy operation.

    Materializing a copy's first mirror needs this record: a copy made by earlier code,
    which could materialize without recording it, never qualifies (fail closed).
    """
    path = store / _INTENTS / f"{operation_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps({"operation_id": operation_id}))
    with partial.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(partial, path)


def has_copy_intent(store: Path, operation_id: str) -> bool:
    path = store / _INTENTS / f"{operation_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) == {"operation_id": operation_id}
    except (OSError, ValueError):
        return False


def copy_evidence(shelf: Shelf, copied_by: str) -> frozenset[str]:
    """Entries a copy operation authorizes for one first materialization.

    The copy must have been created by this code (its intent was recorded before it
    committed), Castle must still hold its committed duplicate result, and each entry
    named there must be unchanged since that commit. Materialization always records the
    conversion, which advances the entry, so one copy authorizes one materialization.
    Older or retired evidence authorizes nothing.
    """
    from castle.namespace import namespace_operation

    if not has_copy_intent(shelf.palace_store, copied_by):
        raise ConsumerError("materialize-evidence-unavailable", stage="index")
    try:
        status = namespace_operation(shelf._handle, copied_by)
    except Exception:
        raise ConsumerError("materialize-evidence-unavailable", stage="index") from None
    result = status.result
    if status.state != "committed" or result is None or result.kind != "duplicate":
        raise ConsumerError("materialize-evidence-unavailable", stage="index")
    live = shelf._live()
    return frozenset(
        entry_id
        for entry_id in result.affected_entry_ids
        if entry_id in live and live[entry_id].changed_revision == result.revision_after
    )


def classify(
    shelf: Shelf, entry: Entry, *, lane: Lane, retained: dict[str, list[Path]] | None = None
) -> MirrorState | None:
    """The state of one entry's mirror; None for an entry with no recorded conversion.

    ``retained`` is a :func:`retained_index` the caller already listed, if any.
    """
    metadata = entry.metadata
    if metadata is None or not metadata.converted:
        return None
    root = shelf.mirror_root
    files = (
        retained.get(entry.entry_id, [])
        if retained is not None
        else retained_files(root, entry.entry_id)
    )
    retained = len(files)
    unattributed = _unattributed(shelf.palace_store, files)
    state = _classify(shelf, entry, lane, metadata, retained)
    return MirrorState(state.entry_id, state.state, state.digest, retained, unattributed)


def _classify(
    shelf: Shelf, entry: Entry, lane: Lane, metadata: EntryMetadata, retained: int
) -> MirrorState:
    root = shelf.mirror_root
    try:
        data = read_mirror(shelf.mirror_path(entry.entry_id), root)
    except MirrorInvalid as error:
        return MirrorState(entry.entry_id, error.code, None, retained)
    current = metadata.current_for(entry.cas_id)
    if data is None:
        # No mirror for replaced source data is ordinary conversion work. A current
        # conversion with no mirror is missing.
        state = "mirror-missing" if current else PENDING_CONVERSION
        return MirrorState(entry.entry_id, state, None, retained)
    found = digest(data)
    if not current:
        # Replacing a file is a new ingestion: the previous mirror is kept under a
        # retained name when the new data is converted (``digest`` names those bytes).
        return MirrorState(entry.entry_id, PENDING_CONVERSION, found, retained)
    if found == metadata.mirror_sha256:
        return MirrorState(entry.entry_id, SEARCHABLE, found, retained)
    try:
        validate_mirror(data, lane=lane)
    except MirrorInvalid as error:
        return MirrorState(entry.entry_id, error.code, found, retained)
    refused = refusal_for(shelf.palace_store, entry.entry_id, found)
    return MirrorState(entry.entry_id, refused or PENDING_REINDEX, found, retained)


def classify_shelf(shelf: Shelf, entries: list[Entry] | None = None) -> dict[str, MirrorState]:
    """Every converted entry's state, each mirror read at most once."""
    lane = lane_for(shelf.palace_store)
    retained = retained_index(shelf.mirror_root)
    states: dict[str, MirrorState] = {}
    for entry in shelf.files() if entries is None else entries:
        state = classify(shelf, entry, lane=lane, retained=retained)
        if state is not None:
            states[entry.entry_id] = state
    return states


# ---- the non-destructive writer -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    path: Path
    digest: str
    retained: tuple[Path, ...] = ()
    """Files the write kept under retained names: displaced or conflicting bytes."""


_SWAP = 2  # RENAME_SWAP (macOS) / RENAME_EXCHANGE (Linux)
_NOREPLACE_DARWIN = 4  # RENAME_EXCL
_NOREPLACE_LINUX = 1  # RENAME_NOREPLACE
_AT_FDCWD = -100


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)


def _rename(source: Path, target: Path, flag: str) -> None:
    """Rename with an atomic flag: ``swap`` or ``noreplace``; never a plain replace."""
    try:
        libc = _libc()
        if sys.platform == "darwin":
            function = libc.renamex_np
            function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            value = _SWAP if flag == "swap" else _NOREPLACE_DARWIN
            result = function(os.fsencode(source), os.fsencode(target), value)
        elif sys.platform.startswith("linux"):
            function = libc.renameat2
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            value = _SWAP if flag == "swap" else _NOREPLACE_LINUX
            result = function(_AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target), value)
        else:
            raise AttributeError(sys.platform)
    except AttributeError:
        raise ConsumerError("mirror-write-unsupported", stage="index") from None
    if result != 0:
        code = ctypes.get_errno()
        if code in (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS, errno.EXDEV):
            raise ConsumerError("mirror-write-unsupported", stage="index")
        raise OSError(code, os.strerror(code), str(source))


def _retain(file: Path, entry_root: Path, entry_id: str, provenance: Provenance) -> Path:
    """Move ``file`` to a new unique retained name with a no-replace rename, then record
    its provenance."""
    data_digest = digest(file.read_bytes())
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    for counter in range(1000):
        name = f"{entry_id}.md{_RETAINED}{stamp}-{counter}-{data_digest}"
        target = entry_root / name
        try:
            _rename(file, target, "noreplace")
        except FileExistsError:
            continue
        _record_retention(target, entry_id, provenance)
        return target
    raise ConsumerError("mirror-retention-failed", stage="index", effect="recovery-required")


def _stage(
    path: Path, data: bytes, entry_id: str, provenance: Provenance
) -> tuple[Path, tuple[Path, ...]]:
    """Write ``data`` to the entry's exclusive staging file (the caller retained any
    stale one first, see :func:`_retain_interrupted`)."""
    partial = path.with_suffix(".partial")
    kept: tuple[Path, ...] = ()
    if partial.is_symlink():
        raise ConsumerError("artifact-outside-domain", stage="index")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return partial, kept


def _staging(provenance: Provenance, reason: str = "staging-discarded") -> Provenance:
    """A staging file's bytes are not known to be any recorded conversion's mirror."""
    return Provenance(
        provenance.store, reason, None, None, provenance.operation_id, provenance.revision
    )


def _discard_own(
    partial: Path, data: bytes, entry_id: str, provenance: Provenance
) -> tuple[Path, ...]:
    """Move an unused staging file to a retained name; never delete it.

    Comparing bytes and then deleting is not atomic: a writer holding the file open, or
    one that renamed over it, would lose its bytes. Retention keeps whatever is there.
    """
    if not partial.exists() or partial.is_symlink():
        return ()
    return (_retain(partial, partial.parent, entry_id, _staging(provenance)),)


def _retain_interrupted(path: Path, entry_id: str, provenance: Provenance) -> tuple[Path, ...]:
    """Keep a staging file an interrupted write left behind, before anything else.

    After a crash between an exchange and its retention, the staging name holds the
    displaced bytes, the version ``provenance`` describes (or an edit that intervened).
    """
    partial = path.with_suffix(".partial")
    if partial.is_symlink():
        raise ConsumerError("artifact-outside-domain", stage="index")
    if not partial.exists():
        return ()
    # The bytes may also be this write's own staging or an edit; the recorded source is
    # claimed only when they are exactly the recorded conversion's mirror.
    recorded = digest(partial.read_bytes()) == provenance.recorded_mirror_sha256
    interrupted = Provenance(
        provenance.store,
        "interrupted-write",
        provenance.source_cas_id if recorded else None,
        provenance.recorded_mirror_sha256 if recorded else None,
        provenance.operation_id,
        provenance.revision,
    )
    return (_retain(partial, path.parent, entry_id, interrupted),)


def discard_staged(path: Path, data: bytes, provenance: Provenance) -> tuple[Path, ...]:
    """Move a mirror Biblios created and never published to a retained name.

    Used when an operation fails after creating a new mirror. Nothing is deleted: a user
    who edited or reopened the new file keeps those bytes in the retained version.
    """
    if path.is_symlink() or not path.is_file():
        return ()
    return (_retain(path, path.parent, path.stem, _staging(provenance)),)


def write_mirror(
    path: Path, data: bytes, *, expected: str | None, provenance: Provenance
) -> WriteOutcome:
    """Put ``data`` at ``path`` without losing bytes Biblios did not verify.

    ``expected=None`` creates and refuses if anything exists. ``expected`` set exchanges
    atomically and keeps the displaced bytes; if they differ from ``expected`` the
    exchange is undone and ``mirror-changed-concurrently`` is raised. A path that already
    holds exactly ``data`` is a completed write (resume). ``provenance`` describes the
    bytes expected at ``path``; it is recorded for each file this write retains.
    """
    entry_id = path.stem
    if path.is_symlink():
        raise ConsumerError("artifact-outside-domain", stage="index")
    path.parent.mkdir(parents=True, exist_ok=True)
    wanted = digest(data)
    interrupted = _retain_interrupted(path, entry_id, provenance)
    if path.is_file() and digest(path.read_bytes()) == wanted:
        return WriteOutcome(path, wanted, interrupted)
    partial, kept = _stage(path, data, entry_id, provenance)
    kept = interrupted + kept
    if expected is None:
        try:
            _rename(partial, path, "noreplace")
        except FileExistsError:
            kept += _discard_own(partial, data, entry_id, provenance)
            raise ConsumerError("mirror-changed-concurrently", stage="index") from None
        return WriteOutcome(path, wanted, kept)
    try:
        _rename(partial, path, "swap")
    except FileNotFoundError:
        kept += _discard_own(partial, data, entry_id, provenance)
        raise ConsumerError("mirror-changed-concurrently", stage="index") from None
    displaced = partial.read_bytes()
    if digest(displaced) == expected:
        moved = _retain(partial, path.parent, entry_id, provenance)
        return WriteOutcome(path, wanted, (*kept, moved))
    # An edit intervened: put it back; what returned to staging is kept, never deleted.
    _rename(partial, path, "swap")
    kept += _discard_own(partial, data, entry_id, provenance)
    raise ConsumerError("mirror-changed-concurrently", stage="index")


def retire_mirror(path: Path, *, provenance: Provenance) -> Path | None:
    """Move a mirror out of the indexed name with a no-replace rename; never delete it."""
    if path.is_symlink():
        raise ConsumerError("artifact-outside-domain", stage="index")
    if not path.exists():
        return None
    return _retain(path, path.parent, path.stem, provenance)
