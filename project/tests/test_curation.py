"""Namespace-backed curation: every operation is one atomic Castle mutation."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from biblios import BibliosError, DuplicateContentError, Mutation
from biblios.progress import indexed_files
from biblios.testing import HashEmbedder
from tests.support import NOTES, RECIPE, TIDE, make_shelf


def test_curation_operations_keep_the_entry_id_and_resolve_paths_at_answer_time(
    tmp_path: Path,
) -> None:
    """Failure mode: a rename, move or copy mints a new entry id or loses metadata, so
    derived artifacts keyed by entry id go stale."""
    shelf, _ = make_shelf(tmp_path)
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain", tags=("sea",))
        assert isinstance(notes, Mutation) and notes.kind == "create-file"
        shelf.mkdir("archive")
        assert [e.path for e in shelf.ls()] == ["archive", "notes.txt"]
        original = shelf.stat("notes.txt")
        assert original.entry_id == notes.entry_id
        assert original.metadata is not None and original.metadata.media_type == "text/plain"
        assert shelf.tags("notes.txt") == ("sea",)

        renamed = shelf.rename("notes.txt", "log.txt")
        assert renamed.entry_id == notes.entry_id
        assert shelf.resolve(notes.entry_id).path == "log.txt"
        moved = shelf.move("log.txt", "archive/log.txt")
        assert moved.entry_id == notes.entry_id
        assert shelf.resolve(notes.entry_id).path == "archive/log.txt"

        copied = shelf.resolve(shelf.copy("archive/log.txt", "copy.txt").entry_id)
        assert copied.entry_id != notes.entry_id
        assert copied.cas_id == original.cas_id and copied.path == "copy.txt"
        assert copied.metadata is not None and copied.metadata.tags == ("sea",)

        updated = shelf.update("archive/log.txt", RECIPE.encode())
        assert updated.entry_id == notes.entry_id and updated.kind == "replace-content"
        replaced = shelf.resolve(notes.entry_id)
        assert replaced.cas_id != original.cas_id
        assert replaced.metadata is not None and replaced.metadata.tags == ("sea",)
        assert shelf.download(notes.entry_id) == RECIPE.encode()

        shelf.set_tags("copy.txt", ("sea", "copy"), {"written": "2026-11-06"})
        assert shelf.tags("copy.txt") == ("sea", "copy")

        removed = shelf.remove("archive")
        assert removed.kind == "delete-subtree" and notes.entry_id in removed.affected_entry_ids
        assert [e.path for e in shelf.ls()] == ["copy.txt"]
        with pytest.raises(BibliosError, match="no live entry"):
            shelf.resolve(notes.entry_id)
        with pytest.raises(BibliosError, match="no entry at"):
            shelf.stat("archive/log.txt")
        assert shelf.download(copied.entry_id) == NOTES.encode()


def test_add_reports_duplicate_content_instead_of_silently_creating_or_refusing(
    tmp_path: Path,
) -> None:
    """Failure mode: the same bytes silently become two entries, or a wanted second entry
    cannot be created at all."""
    shelf, _ = make_shelf(tmp_path)
    with shelf:
        first = shelf.add("a.txt", TIDE.encode(), media_type="text/plain")
        with pytest.raises(DuplicateContentError) as refused:
            shelf.add("b.txt", TIDE.encode(), media_type="text/plain")
        assert refused.value.entry_ids == (first.entry_id,)
        assert [e.path for e in shelf.ls()] == ["a.txt"]
        second = shelf.add("b.txt", TIDE.encode(), media_type="text/plain", allow_duplicate=True)
        assert second.entry_id != first.entry_id
        assert shelf.resolve(second.entry_id).cas_id == shelf.resolve(first.entry_id).cas_id


def test_retry_with_the_same_operation_id_returns_the_original_result(tmp_path: Path) -> None:
    """Failure mode: a retry re-executes the mutation (a second entry, a second rename),
    returns a different outcome after later curation, or fails because the original path
    no longer resolves."""
    shelf, _ = make_shelf(tmp_path)
    with shelf:
        add_id = str(uuid.uuid4())
        first = shelf.add("a.txt", NOTES.encode(), media_type="text/plain", operation_id=add_id)
        again = shelf.add("a.txt", NOTES.encode(), media_type="text/plain", operation_id=add_id)
        assert again.entry_id == first.entry_id and again.replayed and not first.replayed
        assert [e.path for e in shelf.ls()] == ["a.txt"]

        mkdir_id = str(uuid.uuid4())
        folder = shelf.mkdir("docs", operation_id=mkdir_id)
        assert shelf.mkdir("docs", operation_id=mkdir_id).entry_id == folder.entry_id

        rename_id = str(uuid.uuid4())
        renamed = shelf.rename("a.txt", "b.txt", operation_id=rename_id)
        shelf.move("b.txt", "docs/b.txt")
        # The original path is gone, and the entry has moved on: the retry still answers
        # with the original outcome.
        replay = shelf.rename("a.txt", "b.txt", operation_id=rename_id)
        assert (replay.entry_id, replay.revision_after) == (
            renamed.entry_id,
            renamed.revision_after,
        )
        assert replay.replayed and shelf.resolve(first.entry_id).path == "docs/b.txt"

        update_id = str(uuid.uuid4())
        updated = shelf.update("docs/b.txt", RECIPE.encode(), operation_id=update_id)
        shelf.update("docs/b.txt", TIDE.encode())
        replay = shelf.update("docs/b.txt", RECIPE.encode(), operation_id=update_id)
        assert replay.revision_after == updated.revision_after and replay.replayed
        assert shelf.download(first.entry_id) == TIDE.encode()

        copy_id = str(uuid.uuid4())
        copied = shelf.copy("docs/b.txt", "c.txt", operation_id=copy_id)
        assert shelf.copy("docs/b.txt", "c.txt", operation_id=copy_id).entry_id == copied.entry_id
        assert sorted(e.path for e in shelf.ls()) == ["c.txt", "docs"]

        remove_id = str(uuid.uuid4())
        removed = shelf.remove("c.txt", operation_id=remove_id)
        replay = shelf.remove("c.txt", operation_id=remove_id)
        assert replay.revision_after == removed.revision_after and replay.replayed
        assert sorted(e.path for e in shelf.ls()) == ["docs"]

        # A retry after the entry itself is gone still returns the stored outcome.
        shelf.remove("docs")
        late = shelf.add("a.txt", NOTES.encode(), media_type="text/plain", operation_id=add_id)
        assert late.entry_id == first.entry_id and late.replayed
        assert shelf.ls() == []


def test_an_operation_id_binds_the_whole_request_so_another_request_never_replays_it(
    tmp_path: Path,
) -> None:
    """Failure mode: an operation id reused for a semantically different request (another
    parent, other metadata, another target, another destination) is answered with the
    earlier operation's stored outcome, leaving the new request undone."""
    shelf, _ = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("docs")
        shelf.mkdir("other")
        shared = str(uuid.uuid4())
        first = shelf.add(
            "docs/a.txt", NOTES.encode(), media_type="text/plain", operation_id=shared
        )
        variants = [
            lambda: shelf.add(
                "other/a.txt",
                NOTES.encode(),
                media_type="text/plain",
                allow_duplicate=True,
                operation_id=shared,
            ),
            lambda: shelf.add(
                "docs/b.txt",
                RECIPE.encode(),
                media_type="text/plain",
                tags=("x",),
                operation_id=shared,
            ),
            lambda: shelf.mkdir("docs/sub", operation_id=shared),
            lambda: shelf.update("docs/b.txt", TIDE.encode(), operation_id=shared),
            lambda: shelf.rename("docs/b.txt", "c.txt", operation_id=shared),
            lambda: shelf.move("docs/c.txt", "other/c.txt", operation_id=shared),
            lambda: shelf.copy("other/c.txt", "docs/d.txt", operation_id=shared),
            lambda: shelf.remove("docs/d.txt", operation_id=shared),
        ]
        seen = {first.operation_id}
        for variant in variants:
            outcome = variant()
            # Every distinct request is its own operation: executed, never replayed.
            assert not outcome.replayed
            assert outcome.operation_id not in seen
            seen.add(outcome.operation_id)
        assert sorted(e.path for e in shelf.ls("docs")) == ["docs/a.txt", "docs/sub"]
        assert sorted(e.path for e in shelf.ls("other")) == ["other/a.txt", "other/c.txt"]
        # The exact original request still replays its own outcome.
        again = shelf.add(
            "docs/a.txt", NOTES.encode(), media_type="text/plain", operation_id=shared
        )
        assert again.replayed and again.operation_id == first.operation_id
        assert again.entry_id == first.entry_id


def test_copy_of_a_directory_replays_its_root_and_remove_returns_at_the_commit(
    tmp_path: Path,
) -> None:
    """Failure mode: a copied directory's replay names a child instead of the copied root,
    or remove waits on the index (or touches it) after the namespace has committed."""
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("docs")
        for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"):
            shelf.add(f"docs/{name}", (NOTES + name).encode(), media_type="text/plain")
        copy_id = str(uuid.uuid4())
        copied = shelf.copy("docs", "docs-copy", operation_id=copy_id)
        root = shelf.resolve(copied.entry_id)
        assert root.kind == "directory" and root.path == "docs-copy"
        assert len(copied.affected_entry_ids) == 7
        replay = shelf.copy("docs", "docs-copy", operation_id=copy_id)
        assert replay.entry_id == copied.entry_id and replay.replayed

        embedder = HashEmbedder()
        shelf.reconcile(settings, embedder=embedder)
        victim = shelf.resolve(shelf.stat("docs/a.txt").entry_id)
        assert victim.entry_id in indexed_files(shelf)

        embedder.calls.clear()
        remove_id = str(uuid.uuid4())
        removed = shelf.remove("docs/a.txt", operation_id=remove_id)
        assert removed.entry_id == victim.entry_id and not removed.replayed
        with pytest.raises(BibliosError, match="no entry at"):
            shelf.stat("docs/a.txt")
        assert victim.entry_id in indexed_files(shelf) and embedder.calls == []
        assert shelf.mirror_path(victim.entry_id).exists()
        replay = shelf.remove("docs/a.txt", operation_id=remove_id)
        assert replay.replayed and replay.entry_id == victim.entry_id
        report = shelf.reconcile(settings, embedder=embedder)
        removed_now = [item.entry_id for item in report.entries if item.outcome == "removed"]
        assert removed_now == [victim.entry_id] and embedder.calls == []
        assert victim.entry_id not in indexed_files(shelf)
        assert not shelf.mirror_path(victim.entry_id).exists()


def test_a_shelf_opened_through_a_relative_root_removes_index_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: a relative shelf root makes Palace see mirror paths it never stored,
    so the reconcile after a remove leaves the rows behind."""
    from biblios import open_shelf

    shelf, settings = make_shelf(tmp_path)
    with shelf:
        entry = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
    monkeypatch.chdir(tmp_path)
    with open_shelf(Path("shelf")) as relative:
        assert relative.root.is_absolute()
        assert entry.entry_id in indexed_files(relative)
        relative.remove("tide.txt")
        relative.reconcile(settings, embedder=HashEmbedder())
        assert indexed_files(relative) == {}
        assert relative.verify().ok, relative.verify().problems
