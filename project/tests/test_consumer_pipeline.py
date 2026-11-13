"""The provider bundle's contract through the one core: conversion envelopes, callback
results and their uncertainty, cancellation, the own-name row, crash recovery at the real
index boundary, and excision of a staged mirror."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from biblios.control import CallControl, ConsumerError
from biblios.core import BringCurrentRequest
from biblios.curation import CreateFile, CurationRequest
from biblios.derived import DERIVED_NAME
from biblios.pipeline import (
    ConsumerPipeline,
    ConversionConfigurationError,
    ConversionResult,
    DerivationPolicy,
    MirrorDocument,
    literal_inputs,
    render_escaped,
)
from biblios.testing import HashEmbedder
from tests.consumer_fixtures import (
    ADDED,
    LANE_POLICY_FIELDS,
    binding_from_document,
    consumer_fixture,
    identifier,
    inventory,
    lane_pipeline,
)


def _outcome(result, entry_id: str) -> tuple[str, str | None]:
    item = result.outcome(entry_id)
    return item.outcome, item.code


def _create(fixture, name: str, source: bytes, media_type: str = "text/plain") -> str:
    return fixture.bound.curate(
        CurationRequest(
            identifier(),
            fixture.bound.view.revision,
            CreateFile(fixture.root_id, name, source, media_type, ADDED),
        )
    ).mutation.affected_entry_ids[0]


def _lane(fixture, *, converter=None, renderer=None, embedder=None, reuse=False):
    """The consumer-lane pipeline with an optional injected converter and renderer."""
    calls: list = []
    pipeline = lane_pipeline(fixture, calls, embedder=embedder, reuse_converted_body=reuse)
    if converter is not None:
        pipeline.converter = converter
    if renderer is not None:
        pipeline.renderer = renderer
    return pipeline, calls


def test_explicit_body_and_mirror_envelopes_preserve_the_complete_conversion(
    tmp_path: Path,
) -> None:
    """Failure mode: a body beyond the default envelope is truncated or silently accepted,
    its uncertain dispatch is repeated blindly, or an explicit larger envelope loses
    content or dispatches the converter again on replay."""
    fixture = consumer_fixture(tmp_path)
    body = "lighthouse " * (400 * 1024)  # beyond the default 4 MiB body envelope
    calls = []

    def converter(request):
        calls.append(request.source)
        return ConversionResult("converted", body, "injected", "1")

    try:
        pipeline, _ = _lane(fixture, converter=converter)
        entry = _create(fixture, "one.txt", b"compressed source")
        refused = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        again = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        assert (_outcome(refused, entry), _outcome(again, entry), len(calls)) == (
            ("uncertain", "conversion-result-invalid"),
            ("uncertain", "external-outcome-uncertain"),
            1,
        )
        pipeline.policy = replace(
            pipeline.policy, max_body_bytes=8 * 1024 * 1024, max_mirror_bytes=16 * 1024 * 1024
        )
        operation = identifier()
        done = fixture.bound.derive(pipeline, entry, operation_id=operation)
        mirror = fixture.bound._shelf.mirror_path(entry).read_text()
        assert _outcome(done, entry) == ("searchable", None) and len(calls) == 2
        assert body.strip() in mirror and "one.txt" not in mirror
        fixture.reopen()
        replay = fixture.bound.derive(pipeline, entry, operation_id=operation)
        assert replay.replayed and replay.outcome(entry) == done.outcome(entry)
        assert len(calls) == 2 and fixture.bound.download(entry) == b"compressed source"
    finally:
        fixture.close()


def test_envelope_declarations_are_strict_and_finite() -> None:
    """Failure mode: a nonsensical or unbounded envelope or snapshot budget is admitted,
    or the default declarations drift."""
    policy = DerivationPolicy(*LANE_POLICY_FIELDS)
    for name in ("max_body_bytes", "max_mirror_bytes"):
        for invalid in (True, 0, 1.5, 512 * 1024 * 1024 + 1):
            with pytest.raises(ConsumerError, match="policy-invalid"):
                replace(policy, **{name: invalid}).validate()
        replace(policy, **{name: 512 * 1024 * 1024}).validate()
    with pytest.raises(ConsumerError, match="policy-invalid"):
        replace(policy, max_chunks=16385).validate()
    for invalid in (True, 0, 1.5):
        with pytest.raises(ConsumerError, match="snapshot-budget-invalid"):
            ConsumerPipeline(
                converter=lambda request: None,
                renderer=render_escaped,
                input_view=literal_inputs,
                policy=policy,
                embedder=HashEmbedder(),
                max_snapshot_bytes=invalid,
            )
    assert (policy.max_body_bytes, policy.max_mirror_bytes, policy.max_chunks) == (
        4 * 1024 * 1024,
        8 * 1024 * 1024,
        16384,
    )


def test_an_odd_filename_is_indexed_as_its_own_row_with_exact_inputs(tmp_path: Path) -> None:
    """Failure mode: a filename with separators, odd spacing or maximal length is refused
    or absorbed into the content's row, or the caller's exact supplied inputs are
    altered."""
    fixture = consumer_fixture(tmp_path)
    names = ("Quillfern # second part.txt", "  MiXeD  # café.txt  ", "x" * 251 + ".txt")
    observed, expected = [], []
    try:
        embedder = HashEmbedder()

        def renderer(result):
            return MirrorDocument("# Section\nbody\n")

        pipeline, _ = _lane(fixture, renderer=renderer, embedder=embedder)
        pipeline.input_view = lambda plan: tuple("exact:" + row.body for row in plan.rows)
        for name in names:
            entry = _create(fixture, name, b"body")
            before = len(embedder.calls)
            flattened = " ".join(name.splitlines())
            expected.append(
                (
                    "searchable",
                    ("exact:# Section\nbody", ("exact:## Filename\n\n" + flattened).rstrip()),
                )
            )
            result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
            observed.append(
                (
                    result.outcome(entry).outcome,
                    tuple(text for batch in embedder.calls[before:] for text in batch),
                )
            )
        assert observed == expected
    finally:
        fixture.close()


def test_a_snapshot_budget_bounds_every_committed_read(tmp_path: Path) -> None:
    """Failure mode: the declared snapshot budget is ignored, so an insufficient budget
    reads committed rows anyway, or it collapses into silent reuse instead of refusing
    with Palace's own cause."""
    fixture = consumer_fixture(tmp_path)
    try:
        pipeline, calls = _lane(fixture, reuse=True)
        source = _create(fixture, "one.txt", b"Lighthouse maintenance.")
        fixture.bound.derive(pipeline, source, operation_id=identifier())
        donor = fixture.bound.donor(source)
        destination = _create(fixture, "two.txt", b"Lighthouse maintenance.")
        pipeline.max_snapshot_bytes = 64
        refused = fixture.bound.derive(
            pipeline, destination, operation_id=identifier(), donor=donor
        ).outcome(destination)
        assert refused.code == "donor-view-changed" and refused.cause.__cause__ is not None
        assert type(refused.cause.__cause__).__module__.startswith("palace")
        pipeline.max_snapshot_bytes = 64 * 1024 * 1024
        reused = fixture.bound.derive(
            pipeline, destination, operation_id=identifier(), donor=donor
        ).outcome(destination)
        assert (reused.outcome, reused.reused, len(calls)) == ("searchable", 2, 1)
    finally:
        fixture.close()


def test_a_failure_after_the_index_commit_stays_unaccepted_and_resumes_without_providers(
    tmp_path: Path, monkeypatch
) -> None:
    """Failure mode: a failure after Palace committed the rows is reported accepted or
    effect-free, or the next call converts or embeds again."""
    import palace.index

    fixture = consumer_fixture(tmp_path)
    original = palace.index.commit_plan

    def commit_then_fail(**kwargs):
        original(**kwargs)
        raise OSError("unsafe failure after the commit")

    try:
        embedder = HashEmbedder()
        pipeline, calls = _lane(fixture, embedder=embedder)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        monkeypatch.setattr(palace.index, "commit_plan", commit_then_fail)
        failed = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        assert _outcome(failed, entry) == ("recovery-required", "publication-failed")
        assert failed.accepted == ()
        before = (list(calls), list(embedder.calls))
        monkeypatch.undo()
        fixture.reopen()
        resumed = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        assert _outcome(resumed, entry) == ("searchable", None)
        assert (list(calls), list(embedder.calls)) == before
        assert fixture.bound.download(entry) == b"fixture body"
    finally:
        fixture.close()


def test_an_uncertain_embedding_requires_an_explicit_retry(tmp_path: Path) -> None:
    """Failure mode: an embedding call whose outcome is unknown is hidden, or the
    completed conversion is dispatched again when it is retried."""
    fixture = consumer_fixture(tmp_path)

    class Failed(HashEmbedder):
        def embed(self, texts):
            super().embed(texts)
            raise RuntimeError("provider marker")

    failed = Failed()
    try:
        pipeline, calls = _lane(fixture, embedder=failed)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        first = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        assert first.outcome(entry).outcome == "uncertain"
        assert "marker" not in (first.outcome(entry).code or "")
        working = HashEmbedder()
        pipeline.embedder = working
        retried = fixture.bound.derive(
            pipeline, entry, operation_id=identifier(), retry_uncertain=True
        )
        assert _outcome(retried, entry) == ("searchable", None)
        assert (len(failed.calls), len(calls), bool(working.calls)) == (1, 1, True)
    finally:
        fixture.close()


def _crash_index(directory: Path) -> None:
    """Child process: bring one entry current and exit right after Palace's real commit."""
    import palace.index

    fixture = consumer_fixture(directory)
    pipeline, _ = _lane(fixture)
    entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
    (directory / "index-input.json").write_text(
        json.dumps({"binding": asdict(fixture.binding), "entry": entry}, default=str)
    )
    embed = pipeline.embedder.embed

    def recorded(texts):
        with (directory / "provider-calls.jsonl").open("a") as stream:
            stream.write(json.dumps(list(texts)) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return embed(texts)

    pipeline.embedder.embed = recorded
    original = palace.index.commit_plan

    def committed(**kwargs):
        original(**kwargs)
        os._exit(79)

    palace.index.commit_plan = committed
    fixture.bound.derive(pipeline, entry, operation_id=identifier())
    raise AssertionError("real index commit boundary was not reached")


def _child(function: str, directory: Path, code: int) -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            f"from tests.test_consumer_pipeline import {function}; "
            f"import sys; {function}(Path(sys.argv[1]))",
            str(directory),
        ],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert child.returncode == code, "child missed its real boundary: " + child.stderr


def _reopen(directory: Path, name: str):
    from castle.namespace_environment import LocalSession

    from biblios.binding import open_bound_shelf

    document = json.loads((directory / name).read_text())
    binding = replace(binding_from_document(document["binding"]), ownership="owned")
    session = LocalSession(
        binding.namespace_root, binding.castle_root, binding.root / "namespace.lock"
    )
    return document, binding, open_bound_shelf(binding, session)


def test_process_exit_after_the_actual_index_commit_resumes_without_reembedding(
    tmp_path: Path,
) -> None:
    """Failure mode: a process that dies right after Palace's commit leaves an entry that
    is re-converted or re-embedded on restart, or never accepted."""
    _child("_crash_index", tmp_path, 79)
    document, _binding, bound = _reopen(tmp_path, "index-input.json")
    try:
        pipeline = ConsumerPipeline(
            converter=lambda request: pytest.fail("completed conversion repeated after crash"),
            renderer=lambda result, name: pytest.fail("completed renderer repeated after crash"),
            input_view=literal_inputs,
            policy=DerivationPolicy(*LANE_POLICY_FIELDS),
            embedder=HashEmbedder(),
        )
        provider_calls = (tmp_path / "provider-calls.jsonl").read_bytes()
        entry = document["entry"]
        result = bound.derive(pipeline, entry, operation_id=identifier())
        assert _outcome(result, entry) == ("searchable", None)
        assert pipeline.embedder.calls == []
        assert (tmp_path / "provider-calls.jsonl").read_bytes() == provider_calls
        assert bound.download(entry) == b"fixture body"
    finally:
        bound.close()


@pytest.mark.parametrize("malformed", ("surrogate", "oversized", "wrong-type", "mirror"))
def test_invalid_callback_results_remain_uncertain_and_active_close_is_refused(
    tmp_path: Path, malformed: str
) -> None:
    """Failure mode: an invalid converter or renderer result loses its uncertainty, is
    repeated blindly, or the shelf closes underneath an active callback."""
    fixture = consumer_fixture(tmp_path)
    calls, closing = [], []

    def converter(request):
        try:
            fixture.bound.close()
        except ConsumerError as error:
            closing.append(error.code)
        calls.append(request.source)
        body = {
            "surrogate": "\ud800",
            "oversized": "x" * (4 * 1024 * 1024 + 1),
            "wrong-type": None,
            "mirror": "valid body",
        }[malformed]
        return ConversionResult("converted", body, "injected", "1")

    try:
        pipeline, _ = _lane(
            fixture,
            converter=converter,
            renderer=lambda result, name: MirrorDocument("\ud800", "Filename"),
        )
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        invalid = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        unknown = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        assert invalid.outcome(entry).outcome == "uncertain"
        assert _outcome(unknown, entry) == ("uncertain", "external-outcome-uncertain")
        assert (len(calls), closing, fixture.bound.active_work) == (1, ["work-active"], 0)
    finally:
        fixture.close()


def test_exact_injected_sources_scratch_and_bounded_rendering(tmp_path: Path) -> None:
    """Failure mode: the converter receives altered source bytes, a wrong media type,
    scratch outside the shelf's scratch root, or scratch left behind; or the escaped
    rendering lets source structure through or writes the entry's name into the mirror."""
    fixture = consumer_fixture(tmp_path)
    text = b"---\nsource-marker: true\n---\n# Unsafe heading\nbody"
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Jm0AAAAASUVORK5CYII="
    )
    supplied = [(text, "text/plain", "note.txt"), (png, "image/png", "pixel.png")]
    calls = []
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"unchanged")
    outside_before = inventory(outside)

    def converter(request):
        calls.append(
            (
                request.source,
                request.media_type,
                request.scratch_root.is_relative_to(fixture.binding.scratch_root),
            )
        )
        (request.scratch_root / "source").write_bytes(request.source)
        return ConversionResult("converted", text.decode(), "injected", "1")

    outcomes = []
    try:
        pipeline, _ = _lane(fixture, converter=converter, renderer=render_escaped)
        for source, media_type, name in supplied:
            entry = _create(fixture, name, source, media_type)
            result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
            outcomes.append(
                (
                    result.outcome(entry).outcome,
                    fixture.bound.download(entry),
                    fixture.bound._shelf.mirror_path(entry).read_text(),
                    tuple((fixture.binding.scratch_root / "consumer").iterdir()),
                )
            )
        expected_body = "| ---\n| source-marker: true\n| ---\n| # Unsafe heading\n| body\n"
        assert calls == [(source, media, True) for source, media, _name in supplied]
        assert outcomes == [("searchable", source, expected_body, ()) for source, *_ in supplied]
        assert inventory(outside) == outside_before
    finally:
        fixture.close()


def test_a_completed_operation_replays_and_a_conflicting_request_refuses(
    tmp_path: Path,
) -> None:
    """Failure mode: replaying a completed operation repeats its effects or changes the
    shelf, or a different request under the same operation identifier is admitted."""
    fixture = consumer_fixture(tmp_path)
    try:
        pipeline, calls = _lane(fixture)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        first = fixture.bound.derive(pipeline, entry, operation_id=operation)
        fixture.reopen()
        before = inventory(fixture.binding.mirror_root)
        again = fixture.bound.derive(pipeline, entry, operation_id=operation)
        with pytest.raises(ConsumerError) as conflict:
            fixture.bound.bring_current(
                pipeline, BringCurrentRequest(operation, (entry,), retry_uncertain=True)
            )
        assert (again.replayed, again.outcome(entry), conflict.value.code, calls) == (
            True,
            first.outcome(entry),
            "operation-conflict",
            [b"fixture body"],
        )
        assert inventory(fixture.binding.mirror_root) == before
    finally:
        fixture.close()


def test_an_external_failure_is_never_blindly_retried(tmp_path: Path) -> None:
    """Failure mode: a converter failure with an unknown outcome is dispatched again
    without the caller's explicit retry, or its message becomes the reported code."""
    fixture = consumer_fixture(tmp_path)
    calls = []

    def converter(request):
        calls.append(request.source)
        if len(calls) == 1:
            raise RuntimeError("source-name-query-provider-marker")
        return ConversionResult("converted", "reconciled body", "injected", "1")

    try:
        pipeline, _ = _lane(fixture, converter=converter)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        failed = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        unknown = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        counted = len(calls)
        recovered = fixture.bound.derive(
            pipeline, entry, operation_id=identifier(), retry_uncertain=True
        )
        assert (
            _outcome(failed, entry),
            _outcome(unknown, entry),
            counted,
            len(calls),
            _outcome(recovered, entry),
        ) == (
            ("uncertain", "callback-failed"),
            ("uncertain", "external-outcome-uncertain"),
            1,
            2,
            ("searchable", None),
        )
        assert isinstance(failed.outcome(entry).cause, RuntimeError)
    finally:
        fixture.close()


def test_document_refusal_configuration_failure_and_late_cancellation_stay_distinct(
    tmp_path: Path,
) -> None:
    """Failure mode: a document the converter defers, a configuration failure and a
    cancellation that arrives after dispatch are conflated."""
    fixture = consumer_fixture(tmp_path)
    calls, cancelled = [], []

    def document(request):
        calls.append("document")
        return ConversionResult("unconverted", "", "injected", "1", "unsupported")

    def configuration(request):
        calls.append("configuration")
        raise ConversionConfigurationError("unsafe configuration marker")

    def late(request):
        calls.append("late")
        cancelled.append(True)
        return ConversionResult("converted", "late body", "injected", "1")

    try:
        pipeline, _ = _lane(fixture, converter=document)
        first = fixture.bound.curate(fixture.request("a.txt")).mutation.affected_entry_ids[0]
        refused = fixture.bound.derive(pipeline, first, operation_id=identifier())
        pipeline.converter = configuration
        second = fixture.bound.curate(fixture.request("b.txt", b"b")).mutation.affected_entry_ids[0]
        operation = identifier()
        bad = fixture.bound.derive(pipeline, second, operation_id=operation)
        # The converter's own exception locates the failure for this caller only: it is
        # the outcome's cause, and neither the code nor the recorded result carries it.
        cause = bad.outcome(second).cause
        assert isinstance(cause, ConversionConfigurationError) and cause.__traceback__
        replayed = fixture.bound.derive(pipeline, second, operation_id=operation)
        assert replayed.replayed and replayed.outcome(second).cause is None
        derived = sqlite3.connect(fixture.binding.palace_root / DERIVED_NAME)
        recorded = derived.execute("SELECT result FROM operations").fetchall()
        derived.close()
        assert not any("unsafe configuration marker" in (row[0] or "") for row in recorded)
        pipeline.converter = late
        third = fixture.bound.curate(fixture.request("c.txt", b"c")).mutation.affected_entry_ids[0]
        late_result = fixture.bound.derive(
            pipeline,
            third,
            operation_id=identifier(),
            control=CallControl(cancelled=lambda: bool(cancelled)),
        )
        unknown = fixture.bound.derive(pipeline, third, operation_id=identifier())
        assert (
            _outcome(refused, first),
            _outcome(bad, second),
            _outcome(late_result, third),
            _outcome(unknown, third),
            calls,
        ) == (
            ("unconverted", "unsupported"),
            ("pending", "configuration-invalid"),
            ("uncertain", "cancelled"),
            ("uncertain", "external-outcome-uncertain"),
            ["document", "configuration", "late"],
        )
        assert not fixture.bound._shelf.mirror_path(first).exists()
    finally:
        fixture.close()


def test_an_initially_cancelled_call_has_no_provider_or_mirror_effects(tmp_path: Path) -> None:
    """Failure mode: a call cancelled before it starts downloads, dispatches a provider,
    or writes a mirror."""
    fixture = consumer_fixture(tmp_path)
    try:
        embedder = HashEmbedder()
        pipeline, calls = _lane(fixture, embedder=embedder)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        before = inventory(fixture.binding.mirror_root)
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.derive(
                pipeline,
                entry,
                operation_id=identifier(),
                control=CallControl(cancelled=lambda: True),
            )
        assert (refused.value.code, refused.value.effect, calls, embedder.calls) == (
            "cancelled",
            "none",
            [],
            [],
        )
        assert inventory(fixture.binding.mirror_root) == before
    finally:
        fixture.close()


def test_the_conversion_lifecycle_is_observable_before_a_held_callback_returns(
    tmp_path: Path,
) -> None:
    """Failure mode: a held converter's work is invisible to the active lifetime, so the
    shelf closes underneath it, or a cancellation during the hold is lost."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    fixture = consumer_fixture(tmp_path)
    entered, released, cancelled = Event(), Event(), Event()

    def waiting(request):
        entered.set()
        if not released.wait(10):
            raise RuntimeError("fixture converter deadline")
        return ConversionResult("converted", "body", "injected", "1")

    def inspect_lifetime():
        active = fixture.bound.active_work
        try:
            fixture.bound.close()
        except ConsumerError as error:
            return active, error.code
        return active, "closed"

    try:
        pipeline, _ = _lane(fixture, converter=waiting)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(
                fixture.bound.derive,
                pipeline,
                entry,
                operation_id=identifier(),
                control=CallControl(cancelled=cancelled.is_set),
            )
            try:
                assert entered.wait(10), "conversion callback window was not entered"
                observed = pool.submit(inspect_lifetime).result(timeout=2)
                cancelled.set()
            finally:
                released.set()
            late = future.result(timeout=10)
        assert (observed, _outcome(late, entry), fixture.bound.active_work) == (
            (1, "work-active"),
            ("uncertain", "cancelled"),
            0,
        )
    finally:
        released.set()
        fixture.close()


def _crash_mirror_stage(directory: Path) -> None:
    """Child process: exit while the first mirror's staging file is on disk."""
    import biblios.mirrors as mirrors

    fixture = consumer_fixture(directory)
    pipeline, _ = _lane(fixture)
    entry = fixture.bound.curate(
        fixture.request(data=b"selected staged disclosure")
    ).mutation.affected_entry_ids[0]
    (directory / "mirror-stage-input.json").write_text(
        json.dumps({"binding": asdict(fixture.binding), "entry": entry}, default=str)
    )
    original = mirrors._rename

    def staged(source, destination, flag):
        if Path(source) == fixture.bound._shelf.mirror_path(entry).with_suffix(".partial"):
            os._exit(80)
        return original(source, destination, flag)

    mirrors._rename = staged
    fixture.bound.derive(pipeline, entry, operation_id=identifier())
    raise AssertionError("mirror staging boundary was not reached")


def test_excision_disposes_a_mirror_body_left_by_a_real_staging_interruption(
    tmp_path: Path,
) -> None:
    """Failure mode: a staging file left by a crash survives excision of its entry, so
    the selected disclosure stays on disk."""
    from biblios.history import ExcisionRequest, ExcisionSelection

    _child("_crash_mirror_stage", tmp_path, 80)
    document, binding, bound = _reopen(tmp_path, "mirror-stage-input.json")
    staged = binding.mirror_root / (document["entry"] + ".partial")
    try:
        assert b"selected staged disclosure" in staged.read_bytes()
        request = ExcisionRequest(
            identifier(),
            bound.view.revision,
            ExcisionSelection(entry_ids=(document["entry"],)),
            (),
            binding.root / "successor",
        )
        bound.excise(bound.preview_excision(request).request)
        assert tuple(binding.mirror_root.iterdir()) == ()
    finally:
        bound.close()


def test_a_call_cancelled_before_it_starts_creates_nothing(tmp_path: Path) -> None:
    """Failure mode: a call refused as cancelled with no effect still bootstraps the
    derived store or the writer lock under the binding root."""
    from tests.consumer_fixtures import consumer_fixture, identifier, inventory, lane_pipeline

    fixture = consumer_fixture(tmp_path)
    try:
        calls: list = []
        pipeline = lane_pipeline(fixture, calls)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        before = inventory(fixture.binding.root)
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.derive(
                pipeline,
                entry,
                operation_id=identifier(),
                control=CallControl(cancelled=lambda: True),
            )
        assert (refused.value.code, refused.value.effect) == ("cancelled", "none")
        assert inventory(fixture.binding.root) == before and calls == []
    finally:
        fixture.close()


def test_a_rename_that_lands_before_dispatch_spends_no_provider_call(tmp_path: Path) -> None:
    """Failure mode: a rename committed after planning but before the provider call is
    found only at the commit fence, after the embedding was already paid for."""
    from biblios.curation import RelocateEntry
    from biblios.testing import HashEmbedder
    from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline

    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [])
        entry = fixture.bound.curate(fixture.request(name="pending.txt")).mutation
        entry_id = entry.affected_entry_ids[0]
        embedder = HashEmbedder()
        pipeline.embedder = embedder
        original = pipeline.input_view

        def renaming(plan):
            fixture.bound.curate(
                CurationRequest(
                    identifier(),
                    fixture.bound.view.revision,
                    RelocateEntry(entry_id, fixture.root_id, "renamed.txt"),
                )
            )
            return original(plan)

        pipeline.input_view = renaming
        result = fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        outcome = result.outcome(entry_id)
        assert (outcome.outcome, outcome.code, embedder.calls) == ("pending", "stale-view", [])
    finally:
        fixture.close()


def test_an_isolated_mirror_is_refused_as_a_donor(tmp_path: Path) -> None:
    """Failure mode: an entry whose mirror is damaged is still offered as a reuse donor,
    so its unverifiable rows could seed another entry's stage."""
    from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline

    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [], reuse_converted_body=True)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry, operation_id=identifier())
        fixture.bound._shelf.mirror_path(entry).write_bytes(b"\xff not unicode")
        with pytest.raises(ConsumerError, match="donor-not-complete"):
            fixture.bound.donor(entry)
    finally:
        fixture.close()


def test_the_readable_checkpoint_lists_an_isolated_entry_as_withheld(tmp_path: Path) -> None:
    """Failure mode: a checkpoint taken after an entry was isolated publishes it as a
    member, or omits why it is withheld, so a reader of the checkpoint serves it."""
    from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline

    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [])
        entry = fixture.bound.curate(fixture.request(data=b"Lighthouse.")).mutation
        entry_id = entry.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        other = fixture.bound.curate(fixture.request("tide.txt", b"Harbour tide tables."))
        fixture.bound.derive(
            pipeline, other.mutation.affected_entry_ids[0], operation_id=identifier()
        )
        fixture.bound._shelf.mirror_path(entry_id).write_bytes(b"\xff not unicode")
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        fixture.bound.checkpoint()
        marker = json.loads((fixture.binding.palace_root / "consumer-readable.json").read_text())
        assert marker["withheld"].get(entry_id) == "mirror-unicode-invalid"
        assert entry_id not in marker["membership"]
    finally:
        fixture.close()
