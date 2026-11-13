"""Real held-provider and contested-writer windows at the public consumer boundary: a
writer inside a provider call never blocks public reads, and a change that lands during
that window is never published over."""

from __future__ import annotations

import importlib
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from biblios.control import CallControl, ConsumerError
from biblios.curation import CurationRequest, DeleteEntry, RelocateEntry, ReplaceContent
from biblios.history import ExcisionRequest, ExcisionSelection
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline


class HeldEmbedder(HashEmbedder):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def embed(self, texts):
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("test provider release missing")
        return super().embed(texts)


class Call:
    def __init__(self, call, name):
        self.result, self.errors = [], []
        self.done = threading.Event()

        def run():
            try:
                self.result.append(call())
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, name=name)
        self.thread.start()

    def join(self):
        self.thread.join(15)
        assert not self.thread.is_alive(), "owned test thread did not terminate"


@contextmanager
def working_case(tmp_path):
    fixture = consumer_fixture(tmp_path)
    held, jobs = HeldEmbedder(), []
    calls = []
    try:
        pipeline = lane_pipeline(fixture, calls)
        seed = fixture.bound.curate(fixture.request(data=b"lighthouse stable passage"))
        seed = seed.mutation.affected_entry_ids[0]
        seed_operation = identifier()
        assert (
            fixture.bound.derive(pipeline, seed, operation_id=seed_operation).outcome(seed).outcome
            == "searchable"
        )
        yield fixture, pipeline, held, jobs, seed, seed_operation
    finally:
        held.release.set()
        for job in jobs:
            job.join()
        fixture.close()


def _outcome(result, entry_id: str) -> tuple[str, str | None]:
    item = result.outcome(entry_id)
    return item.outcome, item.code


@pytest.mark.parametrize("mode", ("add", "replace"))
def test_public_reads_finish_inside_document_provider_window(tmp_path: Path, mode: str) -> None:
    """Failure mode: a writer holding a provider call blocks public reads, or a lagging
    read or search exposes replaced content or the not-yet-accepted entry."""
    with working_case(tmp_path) as (fixture, pipeline, held, jobs, seed, _):
        old_mirror = fixture.bound._shelf.mirror_path(seed).read_bytes()
        if mode == "add":
            entry = fixture.bound.curate(
                fixture.request(name="pending.txt", data=b"new document")
            ).mutation.affected_entry_ids[0]
        else:
            fixture.bound.curate(
                CurationRequest(
                    identifier(), fixture.bound.view.revision, ReplaceContent(seed, b"new document")
                )
            )
            entry = seed
        pipeline.embedder = held
        writer = Call(
            lambda: fixture.bound.derive(pipeline, entry, operation_id=identifier()), "derive"
        )
        jobs.append(writer)
        assert held.entered.wait(5), "document provider window was not reached"
        # A replaced source's old acceptance is withdrawn before any provider call (it is
        # never served for the new bytes); the view is stable for the whole window.
        before = fixture.bound.view
        started = time.monotonic()

        def read_all():
            view = fixture.bound.view
            files = fixture.bound.files()
            status = fixture.bound.status()
            source = fixture.bound.download(entry)
            passage = fixture.bound.read(seed).text if mode == "add" else None
            hits = fixture.bound.search(
                "lighthouse", embedder=HashEmbedder(), reranker=OverlapReranker()
            )
            return view, files, status, source, passage, hits

        reader = Call(read_all, "reader")
        jobs.append(reader)
        assert reader.done.wait(2), "public reads blocked behind held document embedding"
        assert time.monotonic() - started < 2
        assert not reader.errors, reader.errors
        view, files, status, source, passage, hits = reader.result[0]
        assert view == before and len(files) == (2 if mode == "add" else 1) and status.lagging
        assert source == b"new document"
        if mode == "add":
            assert passage is not None
            found = {hit.entry_id for hit in hits.hits}
            assert seed in found and entry not in found
        else:
            assert not hits.hits  # the replaced bytes' rows are never served
        assert fixture.bound._shelf.mirror_path(seed).read_bytes() == old_mirror
        held.release.set()
        writer.join()
        assert not writer.errors and _outcome(writer.result[0], entry) == ("searchable", None)


@pytest.mark.parametrize("change", ("rename", "replace", "delete"))
def test_a_source_change_during_the_provider_window_is_never_published_over(
    tmp_path: Path, change: str
) -> None:
    """Failure mode: vectors embedded for bytes that were replaced or removed while the
    provider ran are published, or the entry is not found again by the next call."""
    with working_case(tmp_path) as (fixture, pipeline, held, jobs, _, _):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        converted, convert = [], pipeline.converter
        pipeline.converter = lambda request: converted.append(1) or convert(request)
        pipeline.embedder = held
        writer = Call(
            lambda: fixture.bound.derive(pipeline, entry, operation_id=identifier()), "derive"
        )
        jobs.append(writer)
        assert held.entered.wait(5), "stale-return provider window was not reached"
        mutation = {
            "rename": RelocateEntry(entry, fixture.root_id, "renamed.txt"),
            "replace": ReplaceContent(entry, b"changed bytes"),
            "delete": DeleteEntry(entry),
        }[change]
        fixture.bound.curate(CurationRequest(identifier(), fixture.bound.view.revision, mutation))
        held.release.set()
        writer.join()
        assert not writer.errors and _outcome(writer.result[0], entry) == ("pending", "stale-view")
        assert writer.result[0].accepted == ()
        found = fixture.bound.search("fixture", embedder=HashEmbedder(), reranker=OverlapReranker())
        assert entry not in {hit.entry_id for hit in found.hits}
        pipeline.embedder = HashEmbedder()
        mirror = fixture.bound._shelf.mirror_path(entry)
        staged = mirror.read_bytes()
        settled = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        retained = [path for path in mirror.parent.iterdir() if ".retained-" in path.name]
        if change == "rename":
            # The first mirror carries no name, so it stays the entry's mirror, neither
            # retained nor rewritten; only the index's filename row carries the new name.
            assert settled.outcome(entry).outcome == "searchable"
            assert retained == [] and mirror.read_bytes() == staged
            assert b"renamed" not in staged
            assert len(converted) == 1  # a rename never re-runs the converter
            found = fixture.bound.search(
                "renamed.txt", embedder=HashEmbedder(), reranker=OverlapReranker()
            )
            assert entry in {hit.entry_id for hit in found.hits}
        elif change == "replace":
            # The first mirror written for the stale bytes is the core's own: retained,
            # never deleted, and the new source's mirror takes the live path.
            assert settled.outcome(entry).outcome == "searchable"
            assert len(retained) == 1 and retained[0].read_bytes() == staged
        else:
            # Removed before any acceptance: the core's own exact first mirror is retired
            # (never deleted) by an ordinary removal stage with no predecessor.
            assert settled.outcome(entry).outcome == "removed"
            assert len(retained) == 1 and retained[0].read_bytes() == staged
            assert not mirror.exists()


@pytest.mark.parametrize("operation", ("derive", "donor", "bring", "checkpoint", "excise"))
def test_contested_writer_does_not_capture_reader_mutex(
    tmp_path: Path, monkeypatch, operation: str
) -> None:
    """Failure mode: a second writer waiting for the store lock holds the reader mutex,
    deadlocks the provider return, or leaves the active lifetime."""
    from biblios.core import BringCurrentRequest

    with working_case(tmp_path) as (fixture, pipeline, held, jobs, seed, seed_operation):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        request = ExcisionRequest(
            identifier(),
            fixture.bound.view.revision,
            ExcisionSelection(metadata_entry_ids=(seed,)),
            (),
            fixture.binding.root / "successor",
        )
        pipeline.embedder = held
        writer = Call(
            lambda: fixture.bound.derive(pipeline, entry, operation_id=identifier()), "derive"
        )
        jobs.append(writer)
        assert held.entered.wait(5), "active writer provider window was not reached"
        import biblios.consumer as consumer

        original_lock = consumer.writer_lock

        def bounded_lock(*args, **kwargs):
            if threading.current_thread().name == "competitor":
                kwargs["timeout"] = 3
            return original_lock(*args, **kwargs)

        monkeypatch.setattr(consumer, "writer_lock", bounded_lock)
        lock_module = importlib.import_module("palace.writer_lock")
        original, waiting = lock_module.fcntl.flock, threading.Event()

        def observed_flock(*args):
            try:
                return original(*args)
            except BlockingIOError:
                if threading.current_thread().name == "competitor":
                    waiting.set()
                raise

        monkeypatch.setattr(lock_module.fcntl, "flock", observed_flock)
        actions = {
            "derive": lambda: fixture.bound.derive(pipeline, seed, operation_id=seed_operation),
            "donor": lambda: fixture.bound.donor(seed),
            "bring": lambda: fixture.bound.bring_current(
                pipeline, BringCurrentRequest(identifier(), (seed,))
            ),
            "checkpoint": fixture.bound.checkpoint,
            "excise": lambda: fixture.bound.excise(
                request, control=CallControl(cancelled=held.release.is_set)
            ),
        }
        competitor = Call(actions[operation], "competitor")
        jobs.append(competitor)
        assert waiting.wait(5), "competitor never reached actual contested writer admission"
        reader = Call(fixture.bound.status, "reader")
        jobs.append(reader)
        assert reader.done.wait(2) and not reader.errors, "contested writer held the reader mutex"
        with pytest.raises(ConsumerError) as closing:
            fixture.bound.close()
        assert closing.value.code == "work-active", "waiting writer was absent from active lifetime"
        held.release.set()
        writer.join()
        competitor.join()
        assert not writer.errors and _outcome(writer.result[0], entry) == ("searchable", None)
        if operation == "excise":
            assert len(competitor.errors) == 1 and competitor.errors[0].code == "cancelled"
        else:
            assert not competitor.errors, competitor.errors
        assert len(held.calls) == 1, "contention repeated external embedding dispatch"


def test_real_writer_timeout_is_effect_free_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    """Failure mode: a writer admission that times out claims an effect or leaks active
    work, or disturbs the writer that holds the lock."""
    import biblios.consumer as consumer

    with working_case(tmp_path) as (fixture, pipeline, held, jobs, seed, _):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        pipeline.embedder = held
        writer = Call(
            lambda: fixture.bound.derive(pipeline, entry, operation_id=identifier()), "derive"
        )
        jobs.append(writer)
        assert held.entered.wait(5), "timeout provider window was not reached"
        active_before = fixture.bound.active_work
        original = consumer.writer_lock
        monkeypatch.setattr(
            consumer, "writer_lock", lambda *args, **kwargs: original(*args, **kwargs, timeout=0)
        )
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.donor(seed)
        assert (refused.value.code, refused.value.effect) == ("writer-busy", "none")
        assert type(refused.value.__cause__).__name__ == "WriterLockTimeout"
        assert fixture.bound.active_work == active_before, "timed-out admission leaked active work"
        held.release.set()
        writer.join()
        assert not writer.errors, "timeout damaged the original writer"
        assert len(held.calls) == 1, "timeout repeated external embedding dispatch"


def test_cancellation_before_dispatch_has_no_external_uncertainty(tmp_path: Path) -> None:
    """Failure mode: a call cancelled before any provider dispatch reports an uncertain
    external effect, or dispatches anyway."""
    with working_case(tmp_path) as (fixture, pipeline, held, _, _, _):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        cancelled = threading.Event()
        original = pipeline.input_view

        def cancel_then_view(plan):
            cancelled.set()  # the last step before the embedding dispatch
            return original(plan)

        pipeline.input_view = cancel_then_view
        pipeline.embedder = held
        result = fixture.bound.derive(
            pipeline,
            entry,
            operation_id=identifier(),
            control=CallControl(cancelled=cancelled.is_set),
        )
        outcome = result.outcome(entry)
        assert (outcome.outcome, outcome.code) == ("pending", "cancelled")
        assert not held.entered.is_set() and result.accepted == ()


def test_a_first_mirror_left_by_a_failed_preparation_is_adopted_not_reconverted(
    tmp_path: Path,
) -> None:
    """Failure mode: the no-replace first mirror written before a preparation failure is
    later mistaken for a user's file and refused, or converted and written again."""
    with working_case(tmp_path) as (fixture, pipeline, _, _, _, _):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        calls, convert = [], pipeline.converter
        pipeline.converter = lambda request: calls.append(1) or convert(request)
        mirror = fixture.bound._shelf.mirror_path(entry)
        original = pipeline.input_view
        pipeline.input_view = lambda plan: ()
        failed = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        assert failed.outcome(entry).outcome != "searchable" and failed.accepted == ()
        written = mirror.read_bytes()
        converted = len(calls)
        pipeline.input_view = original
        adopted = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        assert _outcome(adopted, entry) == ("searchable", None)
        assert mirror.read_bytes() == written and len(calls) == converted
