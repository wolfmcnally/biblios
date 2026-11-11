"""Real held-provider and contested-writer windows at the public consumer boundary."""

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
from tests.consumer_fixtures import consumer_fixture, identifier
from tests.test_consumer_pipeline import _pipeline


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
        pipeline, _ = _pipeline(fixture, calls)
        seed = fixture.bound.curate(fixture.request(data=b"lighthouse stable passage"))
        seed = seed.mutation.affected_entry_ids[0]
        seed_operation = identifier()
        fixture.bound.derive(pipeline, seed, operation_id=seed_operation)
        yield fixture, pipeline, held, jobs, seed, seed_operation
    finally:
        held.release.set()
        for job in jobs:
            job.join()
        fixture.close()


@pytest.mark.parametrize("mode", ("add", "replace"))
def test_public_reads_finish_inside_document_provider_window(tmp_path: Path, mode: str) -> None:
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
        before = fixture.bound.view
        pipeline.embedder = held
        writer = Call(
            lambda: fixture.bound.derive(pipeline, entry, operation_id=identifier()), "derive"
        )
        jobs.append(writer)
        assert held.entered.wait(5), "document provider window was not reached"
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
        assert time.monotonic() - started < 2, "public reads exceeded unchanged two-second bound"
        assert not reader.errors, "public lagging reads failed during document embedding"
        view, files, status, source, passage, hits = reader.result[0]
        assert view == before and len(files) == (2 if mode == "add" else 1) and status.lagging, (
            "admitted lagging view changed during provider window"
        )
        assert source == b"new document" and (
            passage is not None if mode == "add" else not hits.hits
        ), "lagging read/search exposed obsolete replaced content"
        if mode == "add":
            assert seed in {hit.entry_id for hit in hits.hits} and entry not in {
                hit.entry_id for hit in hits.hits
            }, "strict lagging search lost admitted seed or exposed pending entry"
        assert fixture.bound._shelf.mirror_path(seed).read_bytes() == old_mirror, (
            "admitted mirror replaced before provider completion"
        )
        held.release.set()
        writer.join()
        assert not writer.errors and writer.result[0].state == "indexed", (
            "held derivation did not publish after reader completion"
        )


@pytest.mark.parametrize("change", ("rename", "replace", "delete", "derived"))
def test_document_return_refuses_changed_view(tmp_path: Path, change: str) -> None:
    with working_case(tmp_path) as (fixture, pipeline, held, jobs, _, _):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        operation = identifier()
        pipeline.embedder = held
        writer = Call(
            lambda: fixture.bound.derive(pipeline, entry, operation_id=operation), "derive"
        )
        jobs.append(writer)
        assert held.entered.wait(5), "stale-return provider window was not reached"
        if change == "derived":
            # Test-local tamper challenges the complete digest, not a supported writer.
            (fixture.binding.palace_root / "consumer-generation.json").write_text("tampered")
        else:
            mutation = {
                "rename": RelocateEntry(entry, fixture.root_id, "renamed.txt"),
                "replace": ReplaceContent(entry, b"changed bytes"),
                "delete": DeleteEntry(entry),
            }[change]
            fixture.bound.curate(
                CurationRequest(identifier(), fixture.bound.view.revision, mutation)
            )
        held.release.set()
        writer.join()
        assert len(writer.errors) == 1 and isinstance(writer.errors[0], ConsumerError), (
            "stale provider result was accepted"
        )
        error = writer.errors[0]
        assert (error.code, error.effect) == ("stale-view", "uncertain"), (
            "stale provider return lost precise uncertainty"
        )
        assert not fixture.bound._shelf.mirror_path(entry).exists(), (
            "stale operation retained its newly staged unadmitted mirror"
        )
        assert pipeline.status(fixture.bound, operation_id=operation).state == (
            "indexing" if change == "derived" else "uncertain"
        ), "stale external dispatch was silently accepted for replay"


@pytest.mark.parametrize("operation", ("derive", "donor", "finish", "checkpoint", "excise"))
def test_contested_writer_does_not_capture_reader_mutex(
    tmp_path: Path, monkeypatch, operation: str
) -> None:
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
            "donor": lambda: pipeline.donor(fixture.bound, seed, operation_id=seed_operation),
            "finish": lambda: pipeline.finish(fixture.bound, operation_id=identifier()),
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
        assert reader.done.wait(2) and not reader.errors, (
            "contested writer held the public reader mutex"
        )
        with pytest.raises(ConsumerError) as closing:
            fixture.bound.close()
        assert closing.value.code == "work-active", "waiting writer was absent from active lifetime"
        held.release.set()
        writer.join()
        competitor.join()
        assert not writer.errors and writer.result[0].state == "indexed", (
            "competing writer deadlocked active provider return"
        )
        assert (
            not competitor.errors
            if operation != "excise"
            else len(competitor.errors) == 1 and competitor.errors[0].code == "cancelled"
        ), "admitted competitor failed or performed cancelled excision"
        assert len(held.calls) == 1, "contention repeated external embedding dispatch"


def test_real_writer_timeout_is_effect_free_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    import biblios.consumer as consumer

    with working_case(tmp_path) as (fixture, pipeline, held, jobs, seed, seed_operation):
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
            pipeline.donor(fixture.bound, seed, operation_id=seed_operation)
        assert (refused.value.code, refused.value.effect) == ("writer-busy", "none"), (
            "writer admission timeout escaped or claimed an admitted-body effect"
        )
        assert fixture.bound.active_work == active_before, "timed-out admission leaked active work"
        held.release.set()
        writer.join()
        assert not writer.errors, "timeout damaged the original writer"
        assert len(held.calls) == 1, "timeout repeated external embedding dispatch"


@pytest.mark.parametrize("change", ("rename", "cancel"))
def test_pre_dispatch_refusal_has_no_external_uncertainty(
    tmp_path: Path, monkeypatch, change
) -> None:
    with working_case(tmp_path) as (fixture, pipeline, held, _, _, _):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        operation = identifier()
        original = pipeline._embed
        cancelled = threading.Event()

        def before_dispatch(bound, identity, work, control):
            if change == "rename":
                bound.curate(
                    CurationRequest(
                        identifier(),
                        bound.view.revision,
                        RelocateEntry(entry, fixture.root_id, "renamed.txt"),
                    )
                )
            else:
                cancelled.set()
            return original(bound, identity, work, control)

        monkeypatch.setattr(pipeline, "_embed", before_dispatch)
        pipeline.embedder = held
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.derive(
                pipeline,
                entry,
                operation_id=operation,
                control=CallControl(cancelled=cancelled.is_set),
            )
        assert (refused.value.code, refused.value.effect) == (
            "stale-view" if change == "rename" else "cancelled",
            "none",
        ), "pre-dispatch refusal invented external uncertainty"
        assert (
            not held.entered.is_set()
            and pipeline.status(fixture.bound, operation_id=operation).state == "indexing"
        ), "pre-dispatch refusal disagreed with durable state"
        assert not fixture.bound._shelf.mirror_path(entry).exists(), "pre-dispatch staging survived"


def test_prepare_failure_removes_owned_unadmitted_residue(tmp_path: Path, monkeypatch) -> None:
    with working_case(tmp_path) as (fixture, pipeline, _, _, _, _):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        operation = identifier()
        mirror = fixture.bound._shelf.mirror_path(entry)
        original = pipeline.input_view
        pipeline.input_view = lambda plan: ()
        with pytest.raises(ConsumerError):
            fixture.bound.derive(pipeline, entry, operation_id=operation)
        assert not mirror.exists(), "preparation failure retained unadmitted staging"
        # A prior interrupted attempt may already have left these same owned bytes.
        from biblios.pipeline import convert

        conversion = convert(
            fixture.bound,
            entry,
            operation_id=operation,
            converter=pipeline.converter,
            renderer=pipeline.renderer,
            policy=pipeline.policy,
        )
        mirror.write_text(conversion.mirror.markdown)
        pipeline.input_view = original
        embed = pipeline._embed

        def stale(bound, identity, work, control):
            bound.curate(
                CurationRequest(
                    identifier(),
                    bound.view.revision,
                    RelocateEntry(entry, fixture.root_id, "renamed.txt"),
                )
            )
            return embed(bound, identity, work, control)

        monkeypatch.setattr(pipeline, "_embed", stale)
        with pytest.raises(ConsumerError):
            fixture.bound.derive(pipeline, entry, operation_id=operation)
        assert not mirror.exists(), "prior unadmitted residue was mistaken for admitted mirror"


def test_staging_cleanup_failure_preserves_typed_effect(tmp_path: Path, monkeypatch) -> None:
    with working_case(tmp_path) as (fixture, pipeline, _, _, _, _):
        entry = fixture.bound.curate(
            fixture.request(name="pending.txt")
        ).mutation.affected_entry_ids[0]
        mirror = fixture.bound._shelf.mirror_path(entry)
        operation = identifier()
        import biblios.mirrors as mirrors

        original = mirrors._rename

        def fail_owned(source, target, flag):
            # Cleanup moves the owned mirror to a retained name; fail exactly that move.
            if source == mirror and flag == "noreplace":
                raise OSError("private provider detail")
            return original(source, target, flag)

        def stale(bound, identity, work, control):
            raise ConsumerError("stale-view", stage="index", effect="uncertain")

        monkeypatch.setattr(mirrors, "_rename", fail_owned)
        monkeypatch.setattr(pipeline, "_embed", stale)
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.derive(pipeline, entry, operation_id=operation)
        assert (refused.value.code, refused.value.effect) == ("stale-view", "uncertain"), (
            "cleanup replaced typed original effect"
        )
        assert refused.value.__notes__ == [
            "Owned unadmitted mirror cleanup failed; recovery required."
        ], "cleanup failure lost bounded recovery diagnostic"
