"""Contained injected conversion and durable callback uncertainty boundaries."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

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


def _conversion_envelope(
    fixture,
    *,
    source: bytes,
    body: str,
    mirror: str,
    name: str = "note.txt",
    media_type: str = "text/plain",
) -> dict:
    """Exercise one complete returned envelope, including original uncertain retry."""
    calls = []

    def converter(request):
        calls.append(request.source)
        return ConversionResult("converted", body, "injected", "1")

    def renderer(result, name):
        return MirrorDocument(mirror, "Filename")

    entry = fixture.bound.curate(
        CurationRequest(
            identifier(),
            fixture.bound.view.revision,
            CreateFile(fixture.root_id, name, source, media_type, ADDED),
        )
    ).mutation.affected_entry_ids[0]
    refused_operation = identifier()
    arguments = dict(converter=converter, renderer=renderer)
    with pytest.raises(ConsumerError) as refused:
        convert(fixture.bound, entry, operation_id=refused_operation, policy=POLICY, **arguments)
    with pytest.raises(ConsumerError) as uncertain:
        convert(fixture.bound, entry, operation_id=refused_operation, policy=POLICY, **arguments)
    assert (refused.value.code, refused.value.effect, uncertain.value.code, len(calls)) == (
        "conversion-result-invalid",
        "uncertain",
        "external-outcome-uncertain",
        1,
    ), "default envelope or original uncertain dispatch was changed"
    policy = replace(POLICY, max_body_bytes=8 * 1024 * 1024, max_mirror_bytes=16 * 1024 * 1024)
    operation = identifier()
    try:
        result = convert(fixture.bound, entry, operation_id=operation, policy=policy, **arguments)
    except ConsumerError:
        pytest.fail("explicit finite envelope was refused instead of preserving complete content")
    file = fixture.binding.palace_root / "consumer-records" / (operation + ".json")
    size = file.stat().st_size
    fixture.reopen()
    replay = convert(fixture.bound, entry, operation_id=operation, policy=policy, **arguments)
    with pytest.raises(ConsumerError) as changed:
        convert(
            fixture.bound,
            entry,
            operation_id=operation,
            policy=replace(policy, max_body_bytes=policy.max_body_bytes + 1),
            **arguments,
        )
    assert (
        result.result.body,
        result.mirror.markdown,
        replay.result.body,
        replay.mirror.markdown,
        replay.replayed,
        result.mirror_sha256,
        calls,
        changed.value.code,
        fixture.bound.download(entry),
    ) == (
        body,
        mirror,
        body,
        mirror,
        True,
        hashlib.sha256(mirror.encode()).hexdigest(),
        [source, source],
        "operation-conflict",
        source,
    ), "explicit envelope truncated content, changed source, or redispatched completed work"
    return {"conversion_checkpoint_bytes": size, "entry": entry, "operation": operation}


def test_explicit_body_mirror_envelopes_preserve_complete_conversion(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    body = "x" * (4 * 1024 * 1024 + 1)
    mirror = body + body + "\n\n## Filename\n\none.txt\n"
    try:
        _conversion_envelope(fixture, source=b"compressed source", body=body, mirror=mirror)
    finally:
        fixture.close()


def test_envelope_declarations_are_strict_and_finite() -> None:
    for field in ("max_body_bytes", "max_mirror_bytes"):
        for invalid in (True, 0, 1.5, 512 * 1024 * 1024 + 1):
            with pytest.raises(ConsumerError, match="policy-invalid"):
                replace(POLICY, **{field: invalid}).validate()
        replace(POLICY, **{field: 512 * 1024 * 1024}).validate()
    with pytest.raises(ConsumerError, match="policy-invalid"):
        replace(POLICY, max_chunks=16385).validate()
    for invalid in (True, 0, 1.5):
        with pytest.raises(ConsumerError, match="snapshot-budget-invalid"):
            ConsumerPipeline(
                converter=lambda request: None,
                renderer=render_escaped,
                input_view=literal_inputs,
                policy=POLICY,
                embedder=HashEmbedder(),
                max_snapshot_bytes=invalid,
            )
    assert (POLICY.max_body_bytes, POLICY.max_mirror_bytes, POLICY.max_chunks) == (
        4 * 1024 * 1024,
        8 * 1024 * 1024,
        16384,
    ), "explicit envelope support changed default resource declarations"


def test_checkpoint_encoded_boundary_preserves_prior_record(tmp_path: Path, monkeypatch) -> None:
    import biblios.pipeline as module
    from biblios.control import save_checkpoint

    file = tmp_path / "checkpoint.json"
    document = {"body": "\u2603\n\x00", "vectors": [[0.123456789, -0.25]], "escaped": '"\\'}
    encoded = json.dumps(document, sort_keys=True).encode("utf-8")
    monkeypatch.setattr(module, "_MAX_CHECKPOINT_BYTES", len(encoded))
    save_checkpoint(file, document, stage="index", max_bytes=len(encoded))
    assert (file.read_bytes(), module._load_checkpoint(file)) == (encoded, document), (
        "exact encoded checkpoint boundary refused or changed serialized content"
    )
    prior = b'{"prior": true}'
    file.write_bytes(prior)
    with pytest.raises(ConsumerError) as refused:
        save_checkpoint(file, document, stage="index", max_bytes=len(encoded) - 1)
    assert (refused.value.code, refused.value.effect, file.read_bytes()) == (
        "checkpoint-write-failed",
        "recovery-required",
        prior,
    ), "oversized encoded checkpoint replaced the prior durable record"
    file.write_bytes(encoded + b" ")
    actual_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda current, **kwargs: (
            SimpleNamespace(st_size=len(encoded))
            if current == file and kwargs.get("follow_symlinks", True)
            else actual_stat(current, **kwargs)
        ),
    )

    def forbidden_parse(*args, **kwargs):
        pytest.fail("oversized checkpoint reached JSON parser")

    monkeypatch.setattr(module.json, "loads", forbidden_parse)
    with pytest.raises(ConsumerError) as corrupt:
        module._load_checkpoint(file, stage="conversion")
    assert (corrupt.value.code, corrupt.value.stage, corrupt.value.effect) == (
        "checkpoint-corrupt",
        "conversion",
        "recovery-required",
    ), "encoded read boundary lost conversion-stage recovery classification"

    # Public conversion must use the same bound before considering a prior record.
    monkeypatch.undo()
    fixture = consumer_fixture(tmp_path / "shelf")
    calls = []
    try:
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        record = fixture.binding.palace_root / "consumer-records" / (operation + ".json")
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{} ")
        monkeypatch.setattr(module, "_MAX_CHECKPOINT_BYTES", 2)
        parsed = []

        def observed_parse(payload):
            parsed.append(payload)
            return json.loads(payload)

        monkeypatch.setattr(module, "json", SimpleNamespace(loads=observed_parse, dumps=json.dumps))
        with pytest.raises(ConsumerError) as public:
            convert(
                fixture.bound,
                entry,
                operation_id=operation,
                converter=lambda request: calls.append(request),
                renderer=render_escaped,
                policy=replace(POLICY, max_body_bytes=2, max_mirror_bytes=2),
            )
        assert (public.value.code, public.value.stage, calls, parsed, record.read_bytes()) == (
            "checkpoint-corrupt",
            "conversion",
            [],
            [],
            b"{} ",
        ), "public conversion bypassed the encoded checkpoint read boundary"
        record.unlink()
        with pytest.raises(ConsumerError) as write:
            convert(
                fixture.bound,
                entry,
                operation_id=operation,
                converter=lambda request: calls.append(request),
                renderer=render_escaped,
                policy=replace(POLICY, max_body_bytes=2, max_mirror_bytes=2),
            )
        assert (write.value.code, write.value.effect, calls, record.exists()) == (
            "checkpoint-write-failed",
            "recovery-required",
            [],
            False,
        ), "pipeline save bypassed the checkpoint bound before callback dispatch"
    finally:
        fixture.close()


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


def test_heading_only_own_name_preserves_projection_and_inputs(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    names = ("Quillfern\u2028# second\u2029part.txt", "  MiXeD  # café.txt  ", "x" * 251 + ".txt")
    observed = []
    expected = []
    try:
        pipeline, embedder = _pipeline(fixture, [])

        def renderer(result, name):
            flattened = " ".join(name.splitlines())
            return MirrorDocument("# Section\nbody\n\n# " + flattened + "\n", flattened.strip())

        pipeline.renderer = renderer
        pipeline.input_view = lambda plan: tuple("exact:" + row.body for row in plan.rows)
        for name in names:
            entry = fixture.bound.curate(
                CurationRequest(
                    identifier(),
                    fixture.bound.view.revision,
                    CreateFile(fixture.root_id, name, b"body", "text/plain", ADDED),
                )
            ).mutation.affected_entry_ids[0]
            before = len(embedder.calls)
            flattened = " ".join(name.splitlines())
            expected.append(
                ("indexed", ("exact:# Section\nbody", "exact:" + ("# " + flattened).rstrip()))
            )
            try:
                result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
                observed.append(
                    (
                        result.state,
                        tuple(text for batch in embedder.calls[before:] for text in batch),
                    )
                )
            except ConsumerError as error:
                observed.append((error.code, ()))
        assert observed == expected, (
            "heading-only own-name projection or exact supplied inputs changed"
        )
    finally:
        fixture.close()


def test_heading_only_name_refuses_forged_or_ambiguous_rows(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    mirrors = (
        ("# forged.txt\n", "forged.txt"),
        ("# own.txt\nextra text\n", "own.txt"),
        ("# own.txt\n# extra\n", "own.txt"),
        ("# own.txt\n\n# Body\nbody\n", "own.txt"),
        ("# own.txt\n\n# own.txt\n", "own.txt"),
        ("body without name\n", "own.txt"),
        ("# own.txt extra\n", "own.txt extra"),
    )
    observed = []
    try:
        pipeline, embedder = _pipeline(fixture, [])
        for index, (markdown, heading) in enumerate(mirrors):
            folder = fixture.bound.curate(
                CurationRequest(
                    identifier(),
                    fixture.bound.view.revision,
                    CreateDirectory(fixture.root_id, str(index)),
                )
            ).mutation.affected_entry_ids[0]
            entry = fixture.bound.curate(
                CurationRequest(
                    identifier(),
                    fixture.bound.view.revision,
                    CreateFile(folder, "own.txt", b"body", "text/plain", ADDED),
                )
            ).mutation.affected_entry_ids[0]
            pipeline.renderer = lambda result, name: MirrorDocument(markdown, heading)
            try:
                result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
                observed.append(result.state)
            except ConsumerError as error:
                observed.append(error.code)
        assert (observed, embedder.calls) == (["mirror-name-row-invalid"] * len(mirrors), []), (
            "heading-only admission accepted forged, extra, absent, duplicate or nonfinal name rows"
        )
    finally:
        fixture.close()


def _snapshot_envelope(
    fixture,
    *,
    source: bytes,
    body: str,
    renderer,
    embedder,
    expected_mirror: bytes,
    expected_rows: int,
    name: str = "note.txt",
    media_type: str = "text/plain",
    insufficient: int = 4096,
    adequate: int = 128 * 1024 * 1024,
    progress=None,
) -> dict:
    """One shared public resource-retry scenario; large inputs are explicit qualification."""
    import sqlite_vec
    from palace.index import read_file_vectors
    from palace.index.embedder_config import (
        identity_for,
        load_embedder_config,
        save_embedder_config,
    )
    from palace.writer_lock import writer_lock

    calls = []

    def converter(request):
        calls.append(request.source)
        return ConversionResult("converted", body, "injected", "1")

    def pipeline(budget):
        return ConsumerPipeline(
            converter=converter,
            renderer=renderer,
            input_view=literal_inputs,
            policy=replace(
                POLICY,
                max_body_bytes=8 * 1024 * 1024,
                max_mirror_bytes=8 * 1024 * 1024,
                reuse_converted_body=True,
            ),
            embedder=embedder,
            max_snapshot_bytes=budget,
            progress=progress,
        )

    limited, allowed = pipeline(insufficient), pipeline(adequate)
    selector = load_embedder_config(fixture.binding.palace_root)
    save_embedder_config(
        fixture.binding.palace_root, replace(selector, representation=allowed.representation)
    )
    identity = identity_for(load_embedder_config(fixture.binding.palace_root))

    def snapshot(entry):
        with writer_lock(fixture.binding.palace_root, operation="test committed envelope"):
            with closing(
                sqlite3.connect(fixture.bound._shelf.chunks_db, isolation_level=None)
            ) as conn:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                return read_file_vectors(
                    conn=conn,
                    watch_root=fixture.binding.mirror_root.resolve(),
                    path=entry + ".md",
                    expected_identity=identity,
                    max_bytes=adequate,
                    max_chunks=16384,
                )

    entry = fixture.bound.curate(
        CurationRequest(
            identifier(),
            fixture.bound.view.revision,
            CreateFile(fixture.root_id, name, source, media_type, ADDED),
        )
    ).mutation.affected_entry_ids[0]
    operation = identifier()
    revision_before = fixture.bound.view.revision
    with pytest.raises(ConsumerError) as refused:
        fixture.bound.derive(limited, entry, operation_id=operation)
    state = limited.status(fixture.bound, operation_id=operation)
    records = fixture.binding.palace_root / "consumer-records"
    durable = json.loads((records / (operation + ".derived.json")).read_text())
    before = (list(calls), list(embedder.calls))
    assert (
        refused.value.code,
        refused.value.stage,
        refused.value.effect,
        state.state,
        durable["state"],
        snapshot(entry),
        fixture.bound._shelf.mirror_path(entry).read_bytes(),
        fixture.bound.view.revision,
        fixture.bound.resolve(entry).searchable,
    ) == (
        "derivation-failed",
        "index",
        "recovery-required",
        "indexing",
        "vectors-ready",
        None,
        expected_mirror,
        revision_before,
        False,
    ), "snapshot refusal hid committed rows, mirror residue, or truthful recovery status"
    accepted = fixture.bound.derive(allowed, entry, operation_id=operation)
    actual = snapshot(entry)
    assert (
        accepted.state,
        len(actual.chunks),
        list(calls),
        list(embedder.calls),
        fixture.bound.download(entry),
        fixture.bound.read(entry).text.encode(),
    ) == (
        "indexed",
        expected_rows,
        *before,
        source,
        expected_mirror,
    ), "adequate same-operation retry repeated providers or lost complete committed content"
    for action in (
        lambda: fixture.bound.derive(limited, entry, operation_id=operation),
        lambda: limited.donor(fixture.bound, entry, operation_id=operation),
    ):
        with pytest.raises(ConsumerError) as read_refused:
            action()
        assert read_refused.value.code in {"derivation-failed", "read-failed"}, (
            "insufficient committed snapshot read lost conservative diagnostics"
        )
    fixture.reopen()
    replay = fixture.bound.derive(allowed, entry, operation_id=operation)
    donor = allowed.donor(fixture.bound, entry, operation_id=operation)
    after_replay = (list(calls), list(embedder.calls))
    folder = fixture.bound.curate(
        CurationRequest(
            identifier(),
            fixture.bound.view.revision,
            CreateDirectory(fixture.root_id, "copy"),
        )
    ).mutation.affected_entry_ids[0]
    destination = fixture.bound.curate(
        CurationRequest(
            identifier(),
            fixture.bound.view.revision,
            CreateFile(folder, name, source, media_type, ADDED),
        )
    ).mutation.affected_entry_ids[0]
    offset = len(embedder.calls)
    copied_operation = identifier()
    copied = fixture.bound.derive(allowed, destination, operation_id=copied_operation, donor=donor)
    own_name = renderer(ConversionResult("converted", body, "injected", "1"), name).name_heading
    fresh_inputs = [text for batch in embedder.calls[offset:] for text in batch]
    fixture.bound.curate(
        CurationRequest(identifier(), fixture.bound.view.revision, DeleteEntry(entry))
    )
    allowed.finish(fixture.bound, operation_id=identifier())
    fixture.reopen()
    again = fixture.bound.derive(allowed, destination, operation_id=copied_operation)
    assert (
        replay.replayed,
        after_replay,
        copied.reused_body_rows,
        copied.embedded_rows,
        len(fresh_inputs),
        bool(fresh_inputs) and own_name in fresh_inputs[0],
        calls,
        again.replayed,
        fixture.bound.download(destination),
        fixture.bound.read(destination).text.encode(),
        len(snapshot(destination).chunks),
    ) == (
        True,
        before,
        expected_rows - 1,
        1,
        1,
        True,
        [source],
        True,
        source,
        expected_mirror,
        expected_rows,
    ), "resource-bound donor reuse repeated body work or coupled destination/name ownership"
    return {
        "rows": expected_rows,
        "dimensions": identity.dim,
        "body_vector_lower_bound_bytes": sum(
            len(row.vector) * 4 + len(row.chunk.body.encode()) + len(row.chunk.body_hash.encode())
            for row in actual.chunks
        ),
        "conversion_checkpoint_bytes": (records / (copied_operation + ".json")).stat().st_size,
        "derived_checkpoint_bytes": (records / (copied_operation + ".derived.json")).stat().st_size,
        "adequate_snapshot_bytes": adequate,
    }


def test_snapshot_budget_propagates_through_publication_replay_and_donor(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    body = "Lighthouse maintenance."

    def renderer(result, name):
        return MirrorDocument(
            "# Body\n\n" + result.body + "\n\n## Filename\n\n" + name + "\n", "Filename"
        )

    try:
        _snapshot_envelope(
            fixture,
            source=body.encode(),
            body=body,
            renderer=renderer,
            embedder=HashEmbedder(),
            expected_mirror=b"# Body\n\nLighthouse maintenance.\n\n## Filename\n\nnote.txt\n",
            expected_rows=2,
        )
    finally:
        fixture.close()


def test_snapshot_postcommit_failure_remains_unaccepted(tmp_path: Path, monkeypatch) -> None:
    import biblios.pipeline as module

    fixture = consumer_fixture(tmp_path)
    calls, committed = [], []
    original = module.read_file_vectors

    def fail_after_commit(**kwargs):
        snapshot = original(**kwargs)
        if snapshot is not None and not committed:
            committed.append(snapshot.view_sha256)
            raise OSError("unsafe readback failure")
        return snapshot

    monkeypatch.setattr(module, "read_file_vectors", fail_after_commit)
    try:
        pipeline, embedder = _pipeline(fixture, calls)
        entry = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        operation = identifier()
        with pytest.raises(ConsumerError) as failed:
            fixture.bound.derive(pipeline, entry, operation_id=operation)
        before = (list(calls), list(embedder.calls))
        record = fixture.binding.palace_root / "consumer-records" / (operation + ".derived.json")
        assert (
            len(committed),
            failed.value.code,
            failed.value.effect,
            pipeline.status(fixture.bound, operation_id=operation).state,
            json.loads(record.read_text())["state"],
        ) == (1, "derivation-failed", "recovery-required", "indexing", "vectors-ready"), (
            "postcommit read failure was accepted or misreported as effect-free rollback"
        )
        fixture.reopen()
        result = fixture.bound.derive(pipeline, entry, operation_id=operation)
        assert (result.state, list(calls), list(embedder.calls), fixture.bound.download(entry)) == (
            "indexed",
            *before,
            b"fixture body",
        ), "committed-but-unaccepted retry repeated providers or lost source content"
    finally:
        fixture.close()


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
