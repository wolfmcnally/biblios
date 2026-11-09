"""The index as visibly derived state: bytes first, per-entry progress, yield, watermark,
and a search that says it is behind."""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from palace import search as palace_search_module
from palace.index import _errors as palace_errors

import biblios.reconcile as reconcile_module
from biblios import IndexLagError
from biblios.progress import EntryProgress, read_watermark
from biblios.reconcile import indexed_files
from biblios.testing import HashEmbedder
from tests.support import LONG, NOTES, RECIPE, TIDE, make_shelf


class _FailsOn(HashEmbedder):
    """Raises Palace's error the first time it is asked to embed a text containing ``marker``;
    disarmed, it embeds like the stand-in."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker
        self.armed = True

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.armed and any(self.marker in text for text in texts):
            raise palace_errors.IndexError("embedder down")
        return super().embed(texts)


def test_bytes_and_listing_are_available_before_the_index_catches_up(tmp_path: Path) -> None:
    """Failure mode: a curation call converts or embeds synchronously, a fresh shelf reads as
    lagging, or status reads index state from the entry metadata instead of the layers."""
    shelf, _ = make_shelf(tmp_path)
    with shelf:
        fresh = shelf.index_status()
        assert fresh.files == 0 and not fresh.lagging and not fresh.built
        assert fresh.searchable_revision == fresh.revision
        added = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        assert shelf.download(added.entry_id) == NOTES.encode()
        assert [entry.path for entry in shelf.ls()] == ["notes.txt"]
        assert shelf.stat("notes.txt").entry_id == added.entry_id
        assert not shelf.mirror_path(added.entry_id).exists() and not shelf.chunks_db.exists()
        status = shelf.index_status()
        assert (status.files, status.pending, status.searchable, status.unconverted) == (1, 1, 0, 0)
        assert status.lagging and not status.built
        assert status.entries == (
            EntryProgress(shelf.shelf_id, added.entry_id, "notes.txt", "stored"),
        )


def test_bulk_add_reports_progress_smallest_first_and_ends_current(tmp_path: Path) -> None:
    """Failure mode: progress never reaches the caller, order is arbitrary, the watermark
    does not advance past the step's own metadata mutations, or totals count chunks Palace
    did not embed."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        long = shelf.add("almanac.md", LONG.encode(), media_type="text/markdown")
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        tide = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        before = shelf.revision
        rows: list[EntryProgress] = []
        report = shelf.reconcile(settings, embedder=embedder, progress=rows.append)
        sizes = {long.entry_id: len(LONG), notes.entry_id: len(NOTES), tide.entry_id: len(TIDE)}
        order = [row.entry_id for row in rows if row.stage == "searchable"]
        assert order == sorted(order, key=sizes.__getitem__) and len(order) == 3
        assert all(row.shelf_id == shelf.shelf_id for row in rows)
        indexing = [r for r in rows if r.entry_id == long.entry_id and r.stage == "indexing"]
        assert (indexing[0].done, indexing[0].total) == (0, None)
        counted = [r for r in indexing if r.total is not None]
        total = counted[-1].total
        assert counted[-1].done == total and total is not None and total > 16
        assert [r.done for r in counted] == sorted(r.done for r in counted)
        assert all(r.eta_seconds is not None for r in counted if r.done < r.total)
        embedded = sum(1 for call in embedder.calls for text in call if long.entry_id in text)
        assert embedded == total
        status = shelf.index_status()
        assert not status.lagging and status.pending == 0 and status.searchable == 3
        assert status.searchable_revision == status.revision == report.searchable_revision
        assert report.revision > before and not report.yielded

        changed = LONG.replace("## Chapter 3\n", "## Chapter 3, revised\n", 1)
        shelf.update("almanac.md", changed.encode())
        rows.clear()
        embedder.calls.clear()
        shelf.reconcile(settings, embedder=embedder, progress=rows.append)
        counted = [
            r for r in rows if r.entry_id == long.entry_id and r.stage == "indexing" and r.total
        ]
        embedded = sum(len(call) for call in embedder.calls)
        assert counted[-1].total == embedded == counted[-1].done and embedded < total
        assert not shelf.index_status().lagging


def test_yield_and_interruption_leave_the_watermark_behind_and_resume_without_re_embedding(
    tmp_path: Path,
) -> None:
    """Failure mode: a yield or a failure re-embeds finished entries, or advances the
    watermark over work that did not complete."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        start = read_watermark(shelf)
        a = shelf.add("a.txt", NOTES.encode(), media_type="text/plain")
        b = shelf.add("b.txt", (RECIPE + TIDE).encode(), media_type="text/plain")
        c = shelf.add("c.md", LONG.encode(), media_type="text/markdown")
        done = {"searchable": 0}

        def progress(row: EntryProgress) -> None:
            done["searchable"] += row.stage == "searchable"

        first = shelf.reconcile(
            settings,
            embedder=embedder,
            progress=progress,
            should_yield=lambda: done["searchable"] >= 1,
        )
        assert first.yielded and first.indexed == (a.entry_id,)
        assert read_watermark(shelf) == start == first.searchable_revision
        status = shelf.index_status()
        assert status.lagging and status.searchable == 1 and status.pending == 2

        embedder.calls.clear()
        second = shelf.reconcile(settings, embedder=embedder)
        assert not second.yielded and second.indexed == (b.entry_id, c.entry_id)
        assert not any("fresnel" in text for call in embedder.calls for text in call)
        assert read_watermark(shelf) == shelf.revision and not shelf.index_status().lagging

        watermark = read_watermark(shelf)
        d = shelf.add("d.txt", (NOTES + " again").encode(), media_type="text/plain")
        e = shelf.add(
            "e.md", LONG.replace("Harbour", "Lighthouse").encode(), media_type="text/markdown"
        )
        flaky = _FailsOn("Lighthouse almanac")
        with pytest.raises(IndexLagError, match="embedder down"):
            shelf.reconcile(settings, embedder=flaky)
        index = indexed_files(shelf)
        assert d.entry_id in index and e.entry_id not in index
        assert read_watermark(shelf) == watermark and shelf.index_status().lagging

        flaky.armed = False
        flaky.calls.clear()
        third = shelf.reconcile(settings, embedder=flaky)
        assert third.indexed == (e.entry_id,)
        assert not any("again" in text for call in flaky.calls for text in call)
        assert read_watermark(shelf) == shelf.revision and not shelf.index_status().lagging


def test_search_answers_while_the_index_lags(tmp_path: Path) -> None:
    """Failure mode: search raises on an unbuilt or bootstrapping store, or claims
    completeness while the index is behind."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        empty = shelf.search("anything at all", embedder=embedder, rerank=False)
        assert empty.hits == () and not empty.indexing
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.add("almanac.md", LONG.encode(), media_type="text/markdown")
        behind = shelf.search("fresnel lens", embedder=embedder, rerank=False)
        assert behind.hits == () and behind.indexing
        assert behind.searchable_revision < behind.revision
        seen = {"searchable": 0}

        def progress(row: EntryProgress) -> None:
            seen["searchable"] += row.stage == "searchable"

        partial = shelf.reconcile(
            settings,
            embedder=embedder,
            progress=progress,
            should_yield=lambda: seen["searchable"] >= 1,
        )
        assert partial.yielded
        lagging = shelf.search("fresnel lens kerosene", embedder=embedder, rerank=False)
        assert lagging.hits[0].entry_id == notes.entry_id and lagging.indexing
        shelf.reconcile(settings, embedder=embedder)
        current = shelf.search("fresnel lens kerosene", embedder=embedder, rerank=False)
        assert current.hits[0].entry_id == notes.entry_id and not current.indexing

    second, settings2 = make_shelf(tmp_path / "second")
    with second:
        second.chunks_db.parent.mkdir(parents=True)
        sqlite3.connect(second.chunks_db).close()
        second.add("tide.txt", TIDE.encode(), media_type="text/plain")
        status = second.index_status()
        assert not status.built and status.lagging and status.pending == 1
        booting = second.search("spring tides", embedder=embedder, rerank=False)
        assert booting.hits == () and booting.indexing
        second.reconcile(settings2, embedder=embedder)
        found = second.search("spring tides new moon", embedder=embedder, rerank=False)
        assert found.hits and not found.indexing

        # Palace stamps the four identity rows one by one outside a transaction: a stamp
        # with only some of them is bootstrap in progress, not a built store, and never
        # a foreign identity to refuse.
        with sqlite3.connect(second.chunks_db) as conn:
            conn.execute("DELETE FROM index_meta WHERE key != 'embed_model'")
        partial = second.index_status()
        assert not partial.built and partial.lagging
        halfway = second.search("spring tides", embedder=embedder, rerank=False)
        assert halfway.hits == () and halfway.indexing
        rebuilt = second.reconcile(settings2, embedder=embedder)
        assert rebuilt.built and not second.index_status().lagging
        assert second.search("spring tides new moon", embedder=embedder, rerank=False).hits


def test_replaced_entry_is_refused_until_its_new_chunks_are_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: stale chunks pass as current under new metadata, or a chunk that
    vanished between retrieval and hydration raises out of search."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        shelf.update("notes.txt", TIDE.encode())
        with pytest.raises(IndexLagError, match="embedder down"):
            shelf.reconcile(settings, embedder=_FailsOn("Spring tides"))
        metadata = shelf.stat("notes.txt").metadata
        assert metadata is not None and metadata.converted
        assert indexed_files(shelf)[notes.entry_id] != metadata.mirror_sha256
        stale = shelf.search("fresnel lens kerosene", embedder=embedder, rerank=False)
        assert stale.hits == () and notes.entry_id in stale.refused and stale.indexing

        shelf.reconcile(settings, embedder=embedder)
        fresh = shelf.search("spring tides new moon", embedder=embedder, rerank=False)
        assert fresh.hits[0].entry_id == notes.entry_id and not fresh.indexing
        assert "fresnel" not in fresh.hits[0].chunk.lower()

        def vanished(**kwargs: object) -> object:
            raise palace_errors.IndexError("unknown chunk_id: gone")

        monkeypatch.setattr(palace_search_module, "expand", vanished)
        refused = shelf.search("spring tides new moon", embedder=embedder, rerank=False)
        assert refused.hits == () and notes.entry_id in refused.refused


def test_status_reads_a_committed_view_while_a_worker_reconciles(tmp_path: Path) -> None:
    """Failure mode: a reader in another thread observes the writer's vacated rows, so an
    entry disappears from counts or listings while its metadata is being recorded."""
    shelf, settings = make_shelf(tmp_path)
    started = threading.Event()
    proceed = threading.Event()

    class _Paused(HashEmbedder):
        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            started.set()
            assert proceed.wait(30), "the reader never released the writer"
            return super().embed(texts)

    with shelf:
        for name, text in (("almanac.md", LONG), ("notes.txt", NOTES), ("tide.txt", TIDE)):
            shelf.add(name, text.encode())
        failures: list[BaseException] = []

        def work() -> None:
            try:
                shelf.reconcile(settings, embedder=_Paused())
            except BaseException as error:  # noqa: BLE001 - reported to the test thread
                failures.append(error)

        worker = threading.Thread(target=work, name="reconcile", daemon=True)
        worker.start()
        observations = 0
        reader = HashEmbedder()
        deadline = time.monotonic() + 60
        try:
            assert started.wait(30), "the worker never reached the embedder"
            # A curation call from this thread while the step runs: it must neither
            # conflict with the step's own mutations nor be overwritten by them.
            shelf.set_tags("almanac.md", ("keep",))
            while worker.is_alive():
                assert time.monotonic() < deadline, "the worker outlived the proof's bound"
                status = shelf.index_status()
                assert status.files == 3
                assert len(shelf.ls()) == 3
                shelf.search("tide", embedder=reader, rerank=False)
                observations += 1
                proceed.set()
        finally:
            proceed.set()
            worker.join(30)
        assert not worker.is_alive() and failures == []
        assert observations > 0
        assert shelf.tags("almanac.md") == ("keep",)
        # The external write makes the first step leave the watermark; the next step,
        # which embeds nothing, brings the shelf current.
        assert shelf.index_status().lagging
        again = shelf.reconcile(settings, embedder=HashEmbedder())
        assert again.indexed == () and not shelf.index_status().lagging

        # The reader waits on the lock Castle holds for a whole mutation, so it can never
        # see the rows a commit vacates and rewrites inside that lock.
        listed: list[int] = []
        with shelf._handle.mutex:
            reader_thread = threading.Thread(target=lambda: listed.append(len(list(shelf.files()))))
            reader_thread.start()
            reader_thread.join(0.5)
            assert reader_thread.is_alive()
        reader_thread.join(30)
        assert not reader_thread.is_alive() and listed == [3]


def test_a_tag_set_during_a_step_survives_the_conversion_record(tmp_path: Path) -> None:
    """Failure mode: the step records a conversion from its snapshot of the entry and
    writes the whole metadata map back, erasing tags or dates committed meanwhile."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")

        def progress(row: EntryProgress) -> None:
            if row.stage == "converting" and row.path == "tide.txt":
                shelf.set_tags("tide.txt", ("ocean",), {"observed": "2026-11-07"})

        first = shelf.reconcile(settings, embedder=embedder, progress=progress)
        entry = shelf.stat("tide.txt")
        assert entry.metadata is not None and entry.metadata.converted
        assert entry.metadata.tags == ("ocean",) and entry.metadata.dates == {
            "observed": "2026-11-07"
        }
        assert not first.yielded and shelf.index_status().lagging
        second = shelf.reconcile(settings, embedder=embedder)
        assert second.indexed == () and not shelf.index_status().lagging
        assert shelf.tags("tide.txt") == ("ocean",)


def test_a_missing_store_is_rebuilt_over_retained_mirrors_with_progress(tmp_path: Path) -> None:
    """Failure mode: recovery of a removed store over existing mirrors fails in the build
    progress adapter, or reads as current because the old watermark survived."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        shutil.rmtree(shelf.chunks_db.parent)
        gone = shelf.index_status()
        assert not gone.built and gone.lagging and gone.pending == 2
        assert gone.searchable_revision == gone.revision
        rows: list[EntryProgress] = []
        report = shelf.reconcile(settings, embedder=embedder, progress=rows.append)
        assert report.built
        assert {row.entry_id for row in rows if row.stage == "indexing"} >= {notes.entry_id}
        status = shelf.index_status()
        assert status.built and not status.lagging and status.searchable == 2
        assert (
            shelf.search("fresnel lens", embedder=embedder, rerank=False).hits[0].entry_id
            == notes.entry_id
        )


def test_an_unconverted_replacement_interrupted_before_row_removal_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: a replacement recorded as unconverted whose old index row was never
    removed is scheduled by no later step, so the shelf can never reconcile again."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        tide = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        shelf.update("tide.txt", b"\x00\x01 not text \x00")
        real_update = reconcile_module.update
        calls = {"n": 0}

        def failing_once(**kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise palace_errors.IndexError("palace unavailable")
            return real_update(**kwargs)

        monkeypatch.setattr(reconcile_module, "update", failing_once)
        with pytest.raises(IndexLagError, match="unavailable"):
            shelf.reconcile(settings, embedder=embedder)
        metadata = shelf.stat("tide.txt").metadata
        assert metadata is not None and metadata.conversion.startswith("unconverted:")
        assert metadata.current_for(shelf.stat("tide.txt").cas_id)
        assert tide.entry_id in indexed_files(shelf) and shelf.index_status().lagging

        report = shelf.reconcile(settings, embedder=embedder)
        assert report.removed == (tide.entry_id,)
        assert indexed_files(shelf) == {} and not shelf.index_status().lagging
        assert shelf.verify().ok, shelf.verify().problems


def test_index_reads_close_their_connections_before_returning(tmp_path: Path) -> None:
    """Failure mode: a read of Palace's index relies on garbage collection to close its
    connection. CPython's sqlite3 connection sits in a reference cycle through its statement
    cache, so an unreferenced one survives until the cyclic collector runs, and a reader
    that returned long ago still holds the database open; the seal's switch out of WAL then
    fails with "database is locked" (observed 2026-11-08 in the racing-seal proof under
    load, once the paused step's identity reads were still open)."""
    import gc

    from biblios.progress import recorded_root, store_built
    from biblios.reconcile import require_identity

    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        gc.collect()
        gc.disable()
        try:
            baseline = {id(o) for o in gc.get_objects() if isinstance(o, sqlite3.Connection)}
            for name, read in (
                ("store_built", lambda: store_built(shelf)),
                ("require_identity", lambda: require_identity(shelf)),
                ("recorded_root", lambda: recorded_root(shelf)),
                ("verify", lambda: shelf.verify()),
            ):
                read()
                survivors = [
                    o
                    for o in gc.get_objects()
                    if isinstance(o, sqlite3.Connection) and id(o) not in baseline
                ]
                open_survivors = []
                for connection in survivors:
                    try:
                        connection.execute("SELECT 1")
                    except sqlite3.ProgrammingError:
                        continue  # closed: harmless until collected
                    open_survivors.append(connection)
                assert not open_survivors, (
                    f"{name} left {len(open_survivors)} index connection(s) open for the "
                    "garbage collector to close"
                )
        finally:
            gc.enable()
