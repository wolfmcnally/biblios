"""Contained injected conversion and durable callback uncertainty boundaries."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from biblios.control import CallControl, ConsumerError
from biblios.curation import CreateDirectory, CreateFile, CurationRequest, DeleteEntry
from biblios.pipeline import (
    ConsumerPipeline,
    ConversionConfigurationError,
    ConversionResult,
    DerivationPolicy,
    MirrorDocument,
    convert,
    literal_inputs,
    render_escaped,
)
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import (
    ADDED,
    binding_from_document,
    consumer_fixture,
    identifier,
    inventory,
)

POLICY = DerivationPolicy(
    "injected", "1", "escaped/1", "body-name/1", "exact/1", "sections/1", "filename/1"
)


def _pipeline(fixture, calls, *, embedder=None, input_view=literal_inputs):
    from palace.index.embedder_config import load_embedder_config, save_embedder_config

    policy = replace(POLICY, reuse_converted_body=True)
    embedder = embedder or HashEmbedder()

    def converter(request):
        calls.append(request.source)
        return ConversionResult("converted", request.source.decode(), "injected", "1")

    def renderer(result, name):
        return MirrorDocument(
            "# Body\n\n" + result.body + "\n\n## Extra\n\nIndependent body row.\n\n"
            "## Filename\n\n" + name + "\n",
            "Filename",
        )

    pipeline = ConsumerPipeline(
        converter=converter,
        renderer=renderer,
        input_view=input_view,
        policy=policy,
        embedder=embedder,
    )
    selector = load_embedder_config(fixture.binding.palace_root)
    save_embedder_config(
        fixture.binding.palace_root, replace(selector, representation=pipeline.representation)
    )
    return pipeline, embedder


def test_integrated_duplicate_reuses_body_but_always_embeds_equal_destination_name(
    tmp_path: Path,
) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []
    try:
        pipeline, embedder = _pipeline(fixture, calls)
        first = fixture.bound.curate(fixture.request("one.txt", b"Lighthouse lens maintenance."))
        source = first.mutation.affected_entry_ids[0]
        operation = identifier()
        initial = fixture.bound.derive(pipeline, source, operation_id=operation)
        assert (
            fixture.bound.read(source).text,
            fixture.bound.read(source, page=1).text,
        ) == (
            "# Body\n\nLighthouse lens maintenance.\n\n## Extra\n\nIndependent body row.\n\n"
            "## Filename\n\none.txt\n",
            "Independent body row.",
        ), "consumer rendered mirror cannot be read as its exact body and section"
        donor = pipeline.donor(fixture.bound, source, operation_id=operation)
        folder = fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                CreateDirectory(fixture.root_id, "folder"),
            )
        ).mutation.affected_entry_ids[0]
        destination = fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                CreateFile(folder, "one.txt", b"Lighthouse lens maintenance.", "text/plain", ADDED),
            )
        ).mutation.affected_entry_ids[0]
        before = len(embedder.calls)
        second = fixture.bound.derive(pipeline, destination, operation_id=identifier(), donor=donor)
        new_inputs = [text for call in embedder.calls[before:] for text in call]
        fixture.bound.curate(
            CurationRequest(identifier(), fixture.bound.view.revision, DeleteEntry(source))
        )
        pipeline.finish(fixture.bound, operation_id=identifier())
        found = fixture.bound.search(
            "Lighthouse lens", embedder=HashEmbedder(), reranker=OverlapReranker()
        )
        assert (
            initial.state,
            second.state,
            second.reused_body_rows,
            second.embedded_rows,
            len(calls),
            len(new_inputs),
            new_inputs[0].splitlines()[-1],
            fixture.bound.download(destination),
            bool(found.hits),
            {hit.entry_id for hit in found.hits},
            fixture.bound.status().lagging,
        ) == (
            "indexed",
            "indexed",
            2,
            1,
            1,
            1,
            "one.txt",
            b"Lighthouse lens maintenance.",
            True,
            {destination},
            False,
        ), "duplicate body/name composition repeated providers or retained donor-dependent rows"
    finally:
        fixture.close()


def test_integrated_observer_failure_replays_durable_index_without_provider_calls(
    tmp_path: Path,
) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []

    def observer(value):
        raise RuntimeError("private-path-marker")

    try:
        pipeline, embedder = _pipeline(fixture, calls)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.derive(pipeline, entry, operation_id=operation, observer=observer)
        counts = (len(calls), len(embedder.calls))
        replay = fixture.bound.derive(pipeline, entry, operation_id=operation)
        status = pipeline.status(fixture.bound, operation_id=operation)
        assert (
            refused.value.effect,
            refused.value.observation.entry_ids,
            "marker" in str(refused.value),
            replay.replayed,
            status.state,
            (len(calls), len(embedder.calls)),
            fixture.bound.status().pending,
        ) == (
            "derived-committed",
            (entry,),
            False,
            True,
            "indexed",
            counts,
            0,
        ), "derived observer failure lost durable status or replay repeated completed providers"
    finally:
        fixture.close()


def test_integrated_uncertain_embedding_requires_explicit_retry(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []

    class Failed(HashEmbedder):
        def embed(self, texts):
            super().embed(texts)
            raise RuntimeError("private-source-marker")

    failed = Failed()
    try:
        pipeline, _ = _pipeline(fixture, calls, embedder=failed)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        with pytest.raises(ConsumerError) as first:
            fixture.bound.derive(pipeline, entry, operation_id=operation)
        with pytest.raises(ConsumerError) as second:
            fixture.bound.derive(pipeline, entry, operation_id=operation)
        uncertain = pipeline.status(fixture.bound, operation_id=operation)
        repaired, working = _pipeline(fixture, calls)
        completed = fixture.bound.derive(
            repaired, entry, operation_id=operation, retry_uncertain=True
        )
        assert (
            first.value.effect,
            second.value.code,
            uncertain.state,
            len(failed.calls),
            len(calls),
            completed.state,
            bool(working.calls),
        ) == (
            "uncertain",
            "external-outcome-uncertain",
            "uncertain",
            1,
            1,
            "indexed",
            True,
        ), "embedding uncertainty was hidden or completed conversion was dispatched again"
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "case", ("missing", "different-input", "reordered-input", "binding", "view")
)
def test_donor_eligibility_uses_actual_ordered_inputs_and_current_same_shelf_view(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []
    try:
        pipeline, embedder = _pipeline(fixture, calls)
        source = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        fixture.bound.derive(pipeline, source, operation_id=operation)
        donor = pipeline.donor(fixture.bound, source, operation_id=operation)
        destination = fixture.bound.curate(fixture.request("copy.txt")).mutation.affected_entry_ids[
            0
        ]
        if case == "missing":
            fixture.bound.curate(
                CurationRequest(identifier(), fixture.bound.view.revision, DeleteEntry(source))
            )
        elif case == "different-input":
            pipeline.input_view = lambda plan: tuple("prefix:" + row.body for row in plan.rows)
        elif case == "reordered-input":
            pipeline.input_view = lambda plan: tuple(row.body for row in reversed(plan.rows))
        elif case == "binding":
            donor = replace(donor, namespace_id=identifier())
        else:
            donor = replace(donor, view_sha256="0" * 64)
        count = len(embedder.calls)
        if case in {"binding", "view"}:
            with pytest.raises(ConsumerError) as refused:
                fixture.bound.derive(pipeline, destination, operation_id=identifier(), donor=donor)
            assert (
                refused.value.code,
                len(embedder.calls),
                len(calls),
                fixture.bound.status().pending,
            ) == (
                "donor-binding-mismatch" if case == "binding" else "donor-view-changed",
                count,
                1,
                1,
            ), "invalid donor binding or changed view dispatched destination providers"
            # SQLite writer open can change its own journal/checkpoint files, but no source changed.
            assert fixture.bound.download(destination) == b"fixture body", (
                "donor refusal modified original destination bytes"
            )
        else:
            result = fixture.bound.derive(
                pipeline, destination, operation_id=identifier(), donor=donor
            )
            submitted = [text for call in embedder.calls[count:] for text in call]
            assert (
                result.reused_body_rows,
                result.embedded_rows,
                len(submitted),
                len(calls),
                fixture.bound.download(destination),
            ) == (
                0,
                3,
                3,
                2 if case == "missing" else 1,
                b"fixture body",
            ), "ineligible body inputs reused vectors or failed ordinary fallback"
    finally:
        fixture.close()


def _crash_index(directory: Path) -> None:
    import biblios.pipeline as module

    fixture = consumer_fixture(directory)
    calls = []
    pipeline, embedder = _pipeline(fixture, calls)
    entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
    operation = identifier()
    (directory / "index-input.json").write_text(
        json.dumps(
            {
                "binding": asdict(fixture.binding),
                "entry": entry,
                "operation": operation,
            },
            default=str,
        )
    )
    original_embed = embedder.embed

    def recorded(texts):
        with (directory / "provider-calls.jsonl").open("a") as stream:
            stream.write(json.dumps(list(texts)) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return original_embed(texts)

    embedder.embed = recorded
    original_commit = module.commit_plan

    def committed(**kwargs):
        original_commit(**kwargs)
        os._exit(79)

    module.commit_plan = committed
    fixture.bound.derive(pipeline, entry, operation_id=operation)
    raise AssertionError("real index commit boundary was not reached")


def test_process_exit_after_actual_index_commit_resumes_without_reembedding(tmp_path: Path) -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from tests.test_consumer_pipeline import _crash_index; "
            "import sys; _crash_index(Path(sys.argv[1]))",
            str(tmp_path),
        ],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 79, "index child missed actual committed boundary: " + child.stderr
    from castle.namespace_environment import LocalSession

    from biblios.binding import open_bound_shelf

    document = json.loads((tmp_path / "index-input.json").read_text())
    binding = replace(binding_from_document(document["binding"]), ownership="owned")
    session = LocalSession(
        binding.namespace_root, binding.castle_root, binding.root / "namespace.lock"
    )
    bound = open_bound_shelf(binding, session)
    try:
        # Configuration was durably declared in the child; no test fixture repairs storage.
        pipeline = ConsumerPipeline(
            converter=lambda request: pytest.fail("completed conversion repeated after crash"),
            renderer=lambda result, name: pytest.fail("completed renderer repeated after crash"),
            input_view=literal_inputs,
            policy=replace(POLICY, reuse_converted_body=True),
            embedder=HashEmbedder(),
        )
        original_calls = (tmp_path / "provider-calls.jsonl").read_bytes()
        result = bound.derive(pipeline, document["entry"], operation_id=document["operation"])
        assert (
            result.state,
            pipeline.embedder.calls,
            bound.status().pending,
            bound.download(document["entry"]),
            (tmp_path / "provider-calls.jsonl").read_bytes(),
        ) == (
            "indexed",
            [],
            0,
            b"fixture body",
            original_calls,
        ), "restart repeated completed external work or accepted uncommitted index state"
    finally:
        bound.close()


@pytest.mark.parametrize("malformed", ("surrogate", "oversized", "wrong-type", "mirror"))
def test_invalid_callback_results_remain_uncertain_and_active_close_is_refused(
    tmp_path: Path,
    malformed: str,
) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []

    def converter(request):
        with pytest.raises(ConsumerError, match="work-active"):
            fixture.bound.close()
        calls.append(request.source)
        body = {
            "surrogate": "\ud800",
            "oversized": "x" * (POLICY.max_body_bytes + 1),
            "wrong-type": None,
            "mirror": "valid body",
        }[malformed]
        return ConversionResult("converted", body, "injected", "1")

    def renderer(result, name):
        return MirrorDocument("\ud800", "Filename")

    try:
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        with pytest.raises(ConsumerError) as invalid:
            convert(
                fixture.bound,
                entry,
                operation_id=operation,
                converter=converter,
                renderer=renderer,
                policy=POLICY,
            )
        with pytest.raises(ConsumerError) as uncertain:
            convert(
                fixture.bound,
                entry,
                operation_id=operation,
                converter=converter,
                renderer=renderer,
                policy=POLICY,
            )
        assert (
            invalid.value.effect,
            uncertain.value.code,
            len(calls),
            fixture.bound.active_work,
        ) == (
            "uncertain",
            "external-outcome-uncertain",
            1,
            0,
        ), "invalid provider result lost uncertainty, repeated effects, or closed active work"
    finally:
        fixture.close()


def test_exact_injected_sources_scratch_and_bounded_rendering(tmp_path: Path) -> None:
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
                request.display_name,
                request.scratch_root.is_relative_to(fixture.binding.scratch_root),
            )
        )
        (request.scratch_root / "source").write_bytes(request.source)
        return ConversionResult("converted", text.decode(), "injected", "1")

    outcomes = []
    try:
        for source, media_type, name in supplied:
            entry_id = fixture.bound.curate(
                CurationRequest(
                    identifier(),
                    fixture.bound.view.revision,
                    CreateFile(fixture.root_id, name, source, media_type, ADDED),
                )
            ).mutation.affected_entry_ids[0]
            status = convert(
                fixture.bound,
                entry_id,
                operation_id=identifier(),
                converter=converter,
                renderer=render_escaped,
                policy=POLICY,
            )
            outcomes.append(
                (
                    fixture.bound.download(entry_id),
                    status.mirror.markdown,
                    tuple((fixture.binding.scratch_root / "consumer").iterdir()),
                )
            )
        expected_body = (
            "| ---\n| source-marker: true\n| ---\n| # Unsafe heading\n| body\n\n## Filename\n\n"
        )
        assert (calls, outcomes, inventory(outside)) == (
            [(source, media, name, True) for source, media, name in supplied],
            [(source, expected_body + name + "\n", ()) for source, media, name in supplied],
            outside_before,
        ), "conversion changed source, scratch boundary, or mirror structure"
    finally:
        fixture.close()


def test_completed_conversion_replays_and_changed_policy_refuses(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []

    def converter(request):
        calls.append(request.source)
        return ConversionResult("converted", "body", "injected", "1")

    try:
        entry_id = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        first = convert(
            fixture.bound,
            entry_id,
            operation_id=operation,
            converter=converter,
            renderer=render_escaped,
            policy=POLICY,
        )
        fixture.reopen()
        before = inventory(fixture.binding.root)
        again = convert(
            fixture.bound,
            entry_id,
            operation_id=operation,
            converter=converter,
            renderer=render_escaped,
            policy=POLICY,
        )
        with pytest.raises(ConsumerError) as conflict:
            convert(
                fixture.bound,
                entry_id,
                operation_id=operation,
                converter=converter,
                renderer=render_escaped,
                policy=replace(POLICY, implementation="changed"),
            )
        assert (
            calls,
            again.replayed,
            again.result,
            again.mirror_sha256,
            conflict.value.code,
            inventory(fixture.binding.root),
        ) == (
            [b"fixture body"],
            True,
            first.result,
            first.mirror_sha256,
            "operation-conflict",
            before,
        ), "conversion repeated completed effects or admitted a different representation policy"
    finally:
        fixture.close()


def test_external_failure_never_blindly_retries_and_diagnostics_are_safe(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []

    def converter(request):
        calls.append(request.source)
        if len(calls) == 1:
            raise RuntimeError("source-name-query-provider-marker")
        return ConversionResult("converted", "reconciled body", "injected", "1")

    try:
        entry_id = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        with pytest.raises(ConsumerError) as failed:
            convert(
                fixture.bound,
                entry_id,
                operation_id=operation,
                converter=converter,
                renderer=render_escaped,
                policy=POLICY,
            )
        with pytest.raises(ConsumerError) as uncertain:
            convert(
                fixture.bound,
                entry_id,
                operation_id=operation,
                converter=converter,
                renderer=render_escaped,
                policy=POLICY,
            )
        count_before_explicit_retry = len(calls)
        recovered = convert(
            fixture.bound,
            entry_id,
            operation_id=operation,
            converter=converter,
            renderer=render_escaped,
            policy=POLICY,
            retry_uncertain=True,
        )
        assert (
            failed.value.code,
            failed.value.effect,
            "marker" in str(failed.value),
            uncertain.value.code,
            uncertain.value.effect,
            count_before_explicit_retry,
            len(calls),
            recovered.state,
        ) == (
            "callback-failed",
            "uncertain",
            False,
            "external-outcome-uncertain",
            "uncertain",
            1,
            2,
            "converted",
        ), "external conversion repeated without reconciliation or leaked diagnostics"
    finally:
        fixture.close()


def test_document_configuration_and_late_cancellation_remain_distinct(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []
    cancelled = []

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
        entry_id = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        refused = convert(
            fixture.bound,
            entry_id,
            operation_id=identifier(),
            converter=document,
            renderer=render_escaped,
            policy=POLICY,
        )
        with pytest.raises(ConsumerError) as bad_config:
            convert(
                fixture.bound,
                entry_id,
                operation_id=identifier(),
                converter=configuration,
                renderer=render_escaped,
                policy=POLICY,
            )
        operation = identifier()
        with pytest.raises(ConsumerError) as late_result:
            convert(
                fixture.bound,
                entry_id,
                operation_id=operation,
                converter=late,
                renderer=render_escaped,
                policy=POLICY,
                control=CallControl(cancelled=lambda: bool(cancelled)),
            )
        with pytest.raises(ConsumerError) as unknown:
            convert(
                fixture.bound,
                entry_id,
                operation_id=operation,
                converter=late,
                renderer=render_escaped,
                policy=POLICY,
            )
        assert (
            refused.state,
            refused.result.reason,
            refused.mirror,
            bad_config.value.code,
            late_result.value.code,
            late_result.value.effect,
            unknown.value.code,
            calls,
        ) == (
            "unconverted",
            "unsupported",
            None,
            "configuration-invalid",
            "cancelled",
            "uncertain",
            "external-outcome-uncertain",
            ["document", "configuration", "late"],
        ), "document refusal, configuration failure and external cancellation were conflated"
    finally:
        fixture.close()


def test_initially_cancelled_derivation_has_no_bootstrap_or_provider_effects(
    tmp_path: Path,
) -> None:
    fixture = consumer_fixture(tmp_path)
    calls = []
    try:
        pipeline, embedder = _pipeline(fixture, calls)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        before = inventory(fixture.binding.root)
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.derive(
                pipeline,
                entry,
                operation_id=identifier(),
                control=CallControl(cancelled=lambda: True),
            )
        assert (
            refused.value.code,
            refused.value.effect,
            calls,
            embedder.calls,
            inventory(fixture.binding.root),
        ) == ("cancelled", "none", [], [], before), (
            "initial cancellation created bootstrap or provider effects"
        )
    finally:
        fixture.close()


def test_conversion_lifecycle_is_observable_before_cross_thread_callback_release(
    tmp_path: Path,
) -> None:
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
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(
                convert,
                fixture.bound,
                entry,
                operation_id=identifier(),
                converter=waiting,
                renderer=render_escaped,
                policy=POLICY,
                control=CallControl(cancelled=cancelled.is_set),
            )
            try:
                assert entered.wait(10), "conversion lifecycle callback window was not entered"
                observed = pool.submit(inspect_lifetime).result(timeout=2)
                cancelled.set()
            finally:
                released.set()
            with pytest.raises(ConsumerError) as late:
                future.result(timeout=10)
        assert (observed, late.value.code, fixture.bound.active_work) == (
            (1, "work-active"),
            "cancelled",
            0,
        ), "conversion lifecycle was not observable before callback release"
    finally:
        released.set()
        fixture.close()


def _crash_mirror_stage(directory: Path) -> None:
    import biblios.pipeline as module

    fixture = consumer_fixture(directory)
    pipeline, _ = _pipeline(fixture, [])
    entry = fixture.bound.curate(
        fixture.request(data=b"selected staged disclosure")
    ).mutation.affected_entry_ids[0]
    (directory / "mirror-stage-input.json").write_text(
        json.dumps(
            {
                "binding": asdict(fixture.binding),
                "entry": entry,
            },
            default=str,
        )
    )
    original_replace = module.os.replace

    def staged(source, destination):
        if Path(source) == fixture.bound._shelf.mirror_path(entry).with_suffix(".partial"):
            os._exit(80)
        return original_replace(source, destination)

    module.os.replace = staged
    fixture.bound.derive(pipeline, entry, operation_id=identifier())
    raise AssertionError("mirror staging boundary was not reached")


def test_excision_disposes_mirror_body_after_real_staging_interruption(tmp_path: Path) -> None:
    from castle.namespace_environment import LocalSession

    from biblios.binding import open_bound_shelf
    from biblios.history import ExcisionRequest, ExcisionSelection

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from tests.test_consumer_pipeline import _crash_mirror_stage; "
            "import sys; _crash_mirror_stage(Path(sys.argv[1]))",
            str(tmp_path),
        ],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 80, "mirror staging child missed durable boundary: " + child.stderr
    document = json.loads((tmp_path / "mirror-stage-input.json").read_text())
    binding = replace(binding_from_document(document["binding"]), ownership="owned")
    bound = open_bound_shelf(
        binding,
        LocalSession(binding.namespace_root, binding.castle_root, binding.root / "namespace.lock"),
    )
    staged = binding.mirror_root / (document["entry"] + ".partial")
    try:
        staged_body = staged.read_bytes()
        request = ExcisionRequest(
            identifier(),
            bound.view.revision,
            ExcisionSelection(entry_ids=(document["entry"],)),
            (),
            binding.root / "successor",
        )
        proposal = bound.preview_excision(request)
        bound.excise(proposal.request)
        assert (
            b"selected staged disclosure" in staged_body,
            tuple(binding.mirror_root.iterdir()),
        ) == (True, ()), "completed excision retained selected staged mirror disclosure"
    finally:
        bound.close()
