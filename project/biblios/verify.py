"""Verify a shelf: Castle's journal, the mirrors, Palace's per-file hashes and its stamp."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

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
    mirrors: dict[str, str] = field(default_factory=dict)
    """Each converted entry whose mirror is not searchable, with its state."""
    retained: int = 0
    """Retained mirror files kept across the shelf, live entries or not."""
    unattributed: int = 0
    """Retained files with no provenance record."""


def verify_shelf(shelf: Shelf) -> VerifyReport:
    problems: list[str] = []
    castle = cas.verify(shelf.castle_root)
    if not castle.ok:
        problems.extend(f"castle: {error}" for error in castle.errors)
    from biblios.mirrors import (
        PENDING_CONVERSION,
        PENDING_REINDEX,
        classify_shelf,
        retained_summary,
    )

    retained, unattributed = retained_summary(shelf.palace_store, shelf.mirror_root)

    states = classify_shelf(shelf)
    expected: dict[str, str] = {}
    mirrors: dict[str, str] = {}
    for entry_id, state in sorted(states.items()):
        if state.searchable:
            expected[entry_id] = state.digest
            continue
        mirrors[entry_id] = state.state
        if state.state == PENDING_CONVERSION:
            problems.append(f"replaced bytes not yet converted: {entry_id}")
        elif state.state == PENDING_REINDEX:
            problems.append(
                f"mirror digest differs from the namespace record (not yet re-indexed): {entry_id}"
            )
        else:
            problems.append(f"mirror isolated ({state.state}): {entry_id}")
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
    # A non-searchable entry's rows are reported through its state above.
    for entry_id in sorted(set(indexed) - set(expected) - set(mirrors)):
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
    return VerifyReport(
        ok=not problems,
        problems=tuple(problems),
        mirrors=mirrors,
        retained=retained,
        unattributed=unattributed,
    )
