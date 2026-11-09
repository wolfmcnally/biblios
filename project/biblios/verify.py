"""Verify a shelf: Castle's journal, the mirrors, Palace's per-file hashes and its stamp."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from castle import cas
from palace.index._errors import IdentityMismatchError
from palace.index.embedder_config import embedder_config_path, identity_for, load_embedder_config
from palace.index.schema import assert_identity

from biblios.errors import BibliosError
from biblios.progress import read_only_index
from biblios.reconcile import indexed_files
from biblios.shelf import Shelf


@dataclass(frozen=True, slots=True)
class VerifyReport:
    ok: bool
    problems: tuple[str, ...]


def verify_shelf(shelf: Shelf) -> VerifyReport:
    problems: list[str] = []
    castle = cas.verify(shelf.castle_root)
    if not castle.ok:
        problems.extend(f"castle: {error}" for error in castle.errors)
    expected: dict[str, str] = {}
    for entry in shelf.files():
        metadata = entry.metadata
        if metadata is None or not metadata.converted:
            continue
        if not metadata.current_for(entry.cas_id):
            problems.append(f"conversion recorded over replaced bytes: {entry.entry_id}")
            continue
        mirror = shelf.mirror_path(entry.entry_id)
        if not mirror.is_file():
            problems.append(f"mirror missing: {entry.entry_id}")
            continue
        digest = hashlib.sha256(mirror.read_bytes()).hexdigest()
        if digest != metadata.mirror_sha256:
            problems.append(f"mirror digest differs from the namespace record: {entry.entry_id}")
        expected[entry.entry_id] = digest
    try:
        indexed = indexed_files(shelf)
    except (sqlite3.Error, BibliosError) as error:
        problems.append(f"palace: index unusable: {error}")
        indexed = {}
    for entry_id, digest in expected.items():
        if entry_id not in indexed:
            problems.append(f"palace holds no file hash for entry {entry_id}")
        elif indexed[entry_id] != digest:
            problems.append(f"palace file hash differs from the live mirror: {entry_id}")
    for entry_id in sorted(set(indexed) - set(expected)):
        problems.append(f"palace indexes an entry that is not live and current: {entry_id}")
    store = shelf.palace_store
    if not embedder_config_path(store).exists():
        problems.append("palace: no embedder selector declared")
    elif shelf.chunks_db.is_file():
        try:
            with read_only_index(shelf.chunks_db) as conn:
                assert_identity(conn, identity_for(load_embedder_config(store)))
        except IdentityMismatchError as error:
            problems.append(f"palace: {error}")
        except sqlite3.Error as error:
            problems.append(f"palace: index database unreadable: {error}")
    if shelf.sealed:
        from biblios.seal import verify_seal

        problems.extend(verify_seal(shelf).problems)
    return VerifyReport(ok=not problems, problems=tuple(problems))
