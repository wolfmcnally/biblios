"""verify reports every way the derived layers can disagree with the authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from biblios.testing import HashEmbedder
from tests.support import NOTES, TIDE, make_shelf


def test_verify_detects_tampering_stale_binding_missing_rows_and_a_wrong_stamp(
    tmp_path: Path,
) -> None:
    """Failure mode: verify can only say good; a mirror edited on disk, a replacement
    not yet reconciled, a Palace row that vanished, or a stamp that disagrees with the
    declared selector all pass."""
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        tide = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        assert shelf.verify().ok

        mirror = shelf.mirror_path(notes.entry_id)
        mirror.write_bytes(mirror.read_bytes() + b"\ntampered\n")
        report = shelf.verify()
        assert not report.ok
        assert any("mirror digest differs" in p and notes.entry_id in p for p in report.problems)
        shelf.reconcile(settings, embedder=HashEmbedder())
        assert shelf.verify().ok

        shelf.update("tide.txt", NOTES.encode() + b"\nchanged\n")
        report = shelf.verify()
        assert any("replaced bytes" in p and tide.entry_id in p for p in report.problems)
        shelf.reconcile(settings, embedder=HashEmbedder())
        assert shelf.verify().ok

        with sqlite3.connect(shelf.chunks_db) as conn:
            conn.execute("DELETE FROM files WHERE path = ?", (f"{tide.entry_id}.md",))
        report = shelf.verify()
        assert any("no file hash" in p and tide.entry_id in p for p in report.problems)

        with sqlite3.connect(shelf.chunks_db) as conn:
            conn.execute("UPDATE index_meta SET value = 'other-model' WHERE key = 'embed_model'")
        report = shelf.verify()
        assert any("embedding-identity mismatch" in p for p in report.problems)
