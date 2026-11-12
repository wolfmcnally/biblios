"""The index as visibly derived state: bytes first, per-entry progress, yield, honest
status, recovery of a lost store, and a search that says it is behind."""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import palace.index.core as palace_core
import pytest
from palace.index import _errors as palace_errors

from biblios.control import ConsumerError
from biblios.progress import EntryProgress, diagnose, indexed_files
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


def _outcomes(result) -> dict[str, str]:
    return {item.entry_id: item.outcome for item in result.entries}


def _accepted(result) -> list[str]:
    return [stage.entry_id for stage in result.accepted]


def _counts(shelf) -> tuple[int | None, int | None, int, int]:
    status = diagnose(shelf)[0]
    return status.files, status.pending, status.searchable, status.unconverted


def test_bytes_and_listing_are_available_before_the_index_catches_up(tmp_path: Path) -> None:
    """Failure mode: a curation call converts or embeds synchronously, a fresh shelf reads as
    lagging, or ordinary status guesses a count it cannot know in constant cost."""
    shelf, _ = make_shelf(tmp_path)
    with shelf:
        fresh = shelf.index_status()
        assert fresh.files is None and not fresh.lagging and not fresh.built
        assert fresh.searchable_revision == fresh.revision
        added = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        assert shelf.download(added.entry_id) == NOTES.encode()
        assert [entry.path for entry in shelf.ls()] == ["notes.txt"]
        assert shelf.stat("notes.txt").entry_id == added.entry_id
        assert not shelf.mirror_path(added.entry_id).exists() and not shelf.chunks_db.exists()
        status = shelf.index_status()
        assert status.lagging and not status.built and status.pending is None
        # The explicit diagnosis is the exact recount.
        exact = diagnose(shelf)[0]
        assert (exact.files, exact.pending, exact.searchable, exact.unconverted) == (1, 1, 0, 0)
        assert exact.entries == (
            EntryProgress(shelf.shelf_id, added.entry_id, "notes.txt", "stored"),
        )


def test_bulk_add_reports_progress_smallest_first_and_ends_current(tmp_path: Path) -> None:
    """Failure mode: progress never reaches the caller, order is arbitrary, indexing writes
    Castle, or totals count chunks Palace did not embed."""
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
        assert {row.path for row in rows} == {"almanac.md", "notes.txt", "tide.txt"}
        counted = [
            r for r in rows if r.entry_id == long.entry_id and r.stage == "indexing" and r.total
        ]
        total = counted[-1].total
        assert counted[-1].done == total and total is not None and total > 16
        assert [r.done for r in counted] == sorted(r.done for r in counted)
        embedded = sum(1 for call in embedder.calls for text in call if long.entry_id in text)
        assert embedded == total
        status = shelf.index_status()
        assert not status.lagging and status.searchable == 3
        assert _counts(shelf) == (3, 0, 3, 0)
        assert report.revision == before == shelf.revision and not report.yielded

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


def test_yield_and_provider_failure_leave_work_pending_and_resume_without_re_embedding(
    tmp_path: Path,
) -> None:
    """Failure mode: a yield or a provider failure re-embeds finished entries, loses the
    unfinished ones, aborts the whole call, or reports the shelf current."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
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
        assert first.yielded and _accepted(first) == [a.entry_id]
        assert shelf.index_status().lagging and _counts(shelf)[1:3] == (2, 1)

        embedder.calls.clear()
        second = shelf.reconcile(settings, embedder=embedder)
        assert not second.yielded and _accepted(second) == [b.entry_id, c.entry_id]
        assert not any("fresnel" in text for call in embedder.calls for text in call)
        assert not shelf.index_status().lagging

        d = shelf.add("d.txt", (NOTES + " again").encode(), media_type="text/plain")
        e = shelf.add(
            "e.md", LONG.replace("Harbour", "Lighthouse").encode(), media_type="text/markdown"
        )
        flaky = _FailsOn("Lighthouse almanac")
        failed = shelf.reconcile(settings, embedder=flaky)
        # A provider failure is scoped to its entry; the rest of the call completes.
        assert _outcomes(failed) == {d.entry_id: "searchable", e.entry_id: "uncertain"}
        assert failed.outcome(e.entry_id).cause is not None
        index = indexed_files(shelf)
        assert d.entry_id in index and e.entry_id not in index
        assert shelf.index_status().lagging

        flaky.armed = False
        flaky.calls.clear()
        third = shelf.reconcile(settings, embedder=flaky)
        assert _accepted(third) == [e.entry_id]
        assert not any("again" in text for call in flaky.calls for text in call)
        assert not shelf.index_status().lagging


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
        assert not status.built and status.lagging
        booting = second.search("spring tides", embedder=embedder, rerank=False)
        assert booting.hits == () and booting.indexing
        second.reconcile(settings2, embedder=embedder)
        found = second.search("spring tides new moon", embedder=embedder, rerank=False)
        assert found.hits and not found.indexing


def test_replaced_entry_is_refused_until_its_new_chunks_are_indexed(tmp_path: Path) -> None:
    """Failure mode: stale chunks pass as current for a replaced source. (A served body is
    read in the retrieval's own snapshot, so no separate hydration read can vanish.)"""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        shelf.update("notes.txt", TIDE.encode())
        failed = shelf.reconcile(settings, embedder=_FailsOn("Spring tides"))
        assert _outcomes(failed) == {notes.entry_id: "uncertain"}
        stale = shelf.search("fresnel lens kerosene", embedder=embedder, rerank=False)
        assert (
            stale.hits == ()
            and notes.entry_id in {w.entry_id for w in stale.withheld}
            and stale.indexing
        )

        shelf.reconcile(settings, embedder=embedder)
        fresh = shelf.search("spring tides new moon", embedder=embedder, rerank=False)
        assert fresh.hits[0].entry_id == notes.entry_id and not fresh.indexing
        assert "fresnel" not in fresh.hits[0].chunk.lower()


def test_status_reads_a_committed_view_while_a_worker_reconciles(tmp_path: Path) -> None:
    """Failure mode: a reader in another thread observes the writer's vacated rows, so an
    entry disappears from listings, or a curation call during a step is lost or reported
    as already indexed."""
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
            # conflict with the step nor be overwritten by it.
            shelf.set_tags("almanac.md", ("keep",))
            while worker.is_alive():
                assert time.monotonic() < deadline, "the worker outlived the proof's bound"
                shelf.index_status()
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
        # A tag-only write changes nothing the index derives from: the step's fence saw
        # the same source and stayed current. (A content change during a step is fenced
        # stale and requeued; see test_core.)
        again = shelf.reconcile(settings, embedder=HashEmbedder())
        assert again.accepted == () and not shelf.index_status().lagging

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


def test_a_tag_set_during_a_step_survives_it(tmp_path: Path) -> None:
    """Failure mode: indexing writes the entry's metadata back from its snapshot, erasing
    tags or dates committed meanwhile."""
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
        assert entry.metadata is not None and entry.metadata.tags == ("ocean",)
        assert entry.metadata.dates == {"observed": "2026-11-07"}
        assert not first.yielded and not shelf.index_status().lagging
        second = shelf.reconcile(settings, embedder=embedder)
        assert second.accepted == () and not shelf.index_status().lagging
        assert shelf.tags("tide.txt") == ("ocean",)


def test_a_lost_store_reads_as_lagging_and_a_sweep_rebuilds_it_from_the_mirrors(
    tmp_path: Path,
) -> None:
    """Failure mode: a removed index store reads as searchable, nothing ever rebuilds it,
    or recovery reconverts sources or rewrites the user's mirrors."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        tide = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        mirrors = {
            entry_id: shelf.mirror_path(entry_id).read_bytes()
            for entry_id in (notes.entry_id, tide.entry_id)
        }
        shutil.rmtree(shelf.chunks_db.parent)
        gone = shelf.index_status()
        assert not gone.built and gone.lagging and gone.searchable == 0
        # An ordinary call refuses the lost index; only the explicit sweep recovers it.
        with pytest.raises(ConsumerError, match="index-missing"):
            shelf.reconcile(settings, embedder=embedder)
        rows: list[EntryProgress] = []
        report = shelf.reconcile(settings, embedder=embedder, progress=rows.append, sweep=True)
        assert sorted(_accepted(report)) == sorted((notes.entry_id, tide.entry_id))
        assert {row.entry_id for row in rows if row.stage == "indexing"} >= {notes.entry_id}
        assert not any(row.stage == "converting" for row in rows)
        assert {k: shelf.mirror_path(k).read_bytes() for k in mirrors} == mirrors
        status = shelf.index_status()
        assert status.built and not status.lagging and status.searchable == 2
        assert (
            shelf.search("fresnel lens", embedder=embedder, rerank=False).hits[0].entry_id
            == notes.entry_id
        )
        assert shelf.verify().ok, shelf.verify().problems


def test_an_unconverted_replacement_whose_row_removal_failed_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: a replacement recorded as unconverted whose old index row was never
    removed is scheduled by no later step, so its stale rows stay forever."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        tide = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        shelf.update("tide.txt", b"\x00\x01 not text \x00")
        real_delete = palace_core.delete_path
        calls = {"n": 0}

        def failing_once(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise palace_errors.IndexError("palace unavailable")
            return real_delete(*args, **kwargs)

        monkeypatch.setattr(palace_core, "delete_path", failing_once)
        failed = shelf.reconcile(settings, embedder=embedder)
        outcome = failed.outcome(tide.entry_id)
        assert (outcome.outcome, outcome.code) == ("recovery-required", "publication-failed")
        assert "palace unavailable" in str(outcome.cause)
        assert tide.entry_id in indexed_files(shelf) and shelf.index_status().lagging

        report = shelf.reconcile(settings, embedder=embedder)
        assert _outcomes(report) == {tide.entry_id: "unconverted"}
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
    from biblios.shelf import require_identity

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
