"""User-owned mirrors: edits are kept, replacement is a new ingestion, corruption isolates."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from biblios.control import ConsumerError
from biblios.curation import CopyIdentity, CurationRequest, DuplicateEntry, ReplaceContent
from biblios.mirrors import retained_files, retention_record
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import consumer_fixture, identifier
from tests.support import NOTES, TIDE, make_shelf
from tests.test_consumer_pipeline import _pipeline


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _edited(body: str, name: str = "note.txt") -> str:
    return f"# Body\n\n{body}\n\n## Filename\n\n{name}\n"


def _rendered(body: str, name: str = "note.txt") -> bytes:
    """The fixture renderer's output for converted ``body``."""
    return (
        f"# Body\n\n{body}\n\n## Extra\n\nIndependent body row.\n\n## Filename\n\n{name}\n"
    ).encode()


def _hits(fixture, query: str) -> set[str]:
    found = fixture.bound.search(query, embedder=HashEmbedder(), reranker=OverlapReranker())
    return {hit.entry_id for hit in found.hits}


@pytest.fixture
def seeded(tmp_path: Path):
    fixture = consumer_fixture(tmp_path)
    try:
        pipeline, _ = _pipeline(fixture, [])
        entry = fixture.bound.curate(
            fixture.request(data=b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry, operation_id=identifier())
        yield fixture, pipeline, entry
    finally:
        fixture.close()


def _replace(fixture, entry: str, data: bytes) -> None:
    fixture.bound.curate(
        CurationRequest(identifier(), fixture.bound.view.revision, ReplaceContent(entry, data))
    )


def test_replacement_is_a_new_ingestion_that_keeps_an_unindexed_edit(seeded) -> None:
    """Failure mode: replacing a file overwrites or deletes the user's mirror, including
    a direct edit not yet re-indexed, or the old mirror stays searchable."""
    fixture, pipeline, entry = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    edit = _edited("Lighthouse lens maintenance, corrected by hand.").encode()
    recorded = fixture.bound.resolve(entry)
    path.write_bytes(edit)
    _replace(fixture, entry, b"Harbour tide tables.")
    with pytest.raises(ConsumerError) as refused:
        pipeline.finish(fixture.bound, operation_id=identifier())
    assert refused.value.code == "index-incomplete", "a replaced entry passed as current"

    status = fixture.bound.derive(pipeline, entry, operation_id=identifier())
    kept = retained_files(fixture.binding.mirror_root, entry)
    assert status.state == "indexed"
    assert path.read_bytes() == _rendered("Harbour tide tables.")
    assert [file.read_bytes() for file in kept] == [edit], "the unindexed edit was not kept"
    assert kept[0].name.endswith(_sha(edit))
    # Provenance comes from Biblios's own records, not from the user-editable bytes.
    record = retention_record(fixture.binding.palace_root, kept[0])
    assert record is not None, "the retained edit has no provenance record"
    provenance = (
        record["reason"],
        record["source_cas_id"],
        record["recorded_mirror_sha256"],
        record["mirror_sha256"],
    )
    expected = ("source-replaced", recorded.cas_id, recorded.metadata.mirror_sha256, _sha(edit))
    assert provenance == expected, "provenance does not name the recorded source and bytes"
    assert record["recorded_mirror_sha256"] != record["mirror_sha256"]
    assert entry in _hits(fixture, "Harbour tide tables")
    pipeline.finish(fixture.bound, operation_id=identifier())
    found = fixture.bound.search(
        "corrected by hand", embedder=HashEmbedder(), reranker=OverlapReranker()
    )
    assert all("corrected" not in hit.chunk for hit in found.hits)


def test_an_edit_during_a_replacement_window_is_kept_and_refuses(seeded) -> None:
    """Failure mode: an edit made while the new conversion embeds is overwritten.

    The edit changes the admitted view, so the publication fence refuses before any
    write; the exchange's own swap-back covers an edit landing after that fence."""
    fixture, pipeline, entry = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    _replace(fixture, entry, b"Harbour tide tables.")
    entered, release = threading.Event(), threading.Event()
    edit = _edited("Edited while the provider was working.").encode()

    class Held(HashEmbedder):
        def embed(self, texts):
            entered.set()
            assert release.wait(10)
            return super().embed(texts)

    pipeline.embedder = Held()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            fixture.bound.derive(pipeline, entry, operation_id=identifier())
        except BaseException as error:  # noqa: BLE001 - reported to the test thread
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(10)
        path.write_bytes(edit)
    finally:
        release.set()
        worker.join(15)
    assert [getattr(error, "code", None) for error in errors] == ["stale-view"]
    assert path.read_bytes() == edit
    assert all(file.read_bytes() != edit for file in retained_files(path.parent, entry))


def test_reconcile_replacement_keeps_the_previous_mirror(tmp_path: Path) -> None:
    """Failure mode: the directory lane overwrites a replaced file's mirror."""
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        previous = shelf.mirror_path(notes.entry_id).read_bytes()
        shelf.update("notes.txt", TIDE.encode())
        report = shelf.reconcile(settings, embedder=HashEmbedder())
        kept = retained_files(shelf.mirror_root, notes.entry_id)
        assert report.converted == (notes.entry_id,)
        assert [file.read_bytes() for file in kept] == [previous]
        assert shelf.mirror_path(notes.entry_id).read_bytes() != previous
        assert shelf.verify().ok, shelf.verify().problems


def test_a_corrupt_mirror_isolates_only_its_entry(seeded) -> None:
    """Failure mode: one unreadable mirror blocks finishing or searching the shelf."""
    fixture, pipeline, entry = seeded
    other = fixture.bound.curate(
        fixture.request("tide.txt", b"Harbour tide tables.")
    ).mutation.affected_entry_ids[0]
    fixture.bound.derive(pipeline, other, operation_id=identifier())
    broken = b"\xff\xfe not unicode"
    fixture.bound._shelf.mirror_path(entry).write_bytes(broken)

    pipeline.finish(fixture.bound, operation_id=identifier())
    status = fixture.bound.status()
    assert status.isolated == 1 and not status.pending
    assert _hits(fixture, "Harbour tide tables") == {other}
    with pytest.raises(Exception, match="mirror-unicode-invalid"):
        fixture.bound.read(entry)
    assert fixture.bound._shelf.mirror_path(entry).read_bytes() == broken


def test_an_update_is_validated_at_its_seam_and_keeps_the_previous_mirror(seeded) -> None:
    """Failure mode: a malformed update lands, a stale update overwrites a newer edit,
    or a valid update discards the bytes it replaced."""
    fixture, pipeline, entry = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    original = path.read_bytes()

    with pytest.raises(ConsumerError) as invalid:
        fixture.bound.update_mirror(
            pipeline,
            entry,
            operation_id=identifier(),
            markdown="---\n[unclosed\n---\n" + _edited("x"),
            expected_sha256=_sha(original),
        )
    assert invalid.value.code.startswith("mirror-update-invalid:")
    with pytest.raises(ConsumerError) as stale:
        fixture.bound.update_mirror(
            pipeline,
            entry,
            operation_id=identifier(),
            markdown=_edited("Stale."),
            expected_sha256=_sha(b"something else"),
        )
    assert stale.value.code == "mirror-changed-concurrently"
    assert path.read_bytes() == original and not retained_files(path.parent, entry)

    text = _edited("Speaker names corrected in the harbour log.")
    status = fixture.bound.update_mirror(
        pipeline, entry, operation_id=identifier(), markdown=text, expected_sha256=_sha(original)
    )
    assert status.state == "indexed" and path.read_text() == text
    assert [file.read_bytes() for file in retained_files(path.parent, entry)] == [original]
    assert entry in _hits(fixture, "Speaker names corrected")


def test_a_copy_materializes_once_from_its_own_receipt(seeded) -> None:
    """Failure mode: a copy never becomes searchable, or one receipt (or a copy made
    without a recorded intent) recreates a mirror the user deleted."""
    fixture, pipeline, entry = seeded
    copy_id, operation = identifier(), identifier()
    fixture.bound.curate(
        CurationRequest(
            operation,
            fixture.bound.view.revision,
            DuplicateEntry(entry, fixture.root_id, "copy.txt", (CopyIdentity(entry, copy_id),)),
        )
    )
    with pytest.raises(ConsumerError) as missing:
        fixture.bound.derive(pipeline, copy_id, operation_id=identifier())
    assert missing.value.code == "mirror-missing"

    status = fixture.bound.derive(pipeline, copy_id, operation_id=identifier(), copied_by=operation)
    path = fixture.bound._shelf.mirror_path(copy_id)
    assert status.state == "indexed" and path.is_file()

    path.unlink()
    with pytest.raises(ConsumerError) as spent:
        fixture.bound.derive(pipeline, copy_id, operation_id=identifier(), copied_by=operation)
    assert spent.value.code == "materialize-evidence-stale" and not path.exists()


def test_a_copy_without_a_recorded_intent_fails_closed(seeded) -> None:
    """Failure mode: a copy made by older code is materialized from Castle state alone."""
    fixture, pipeline, entry = seeded
    copy_id, operation = identifier(), identifier()
    fixture.bound.curate(
        CurationRequest(
            operation,
            fixture.bound.view.revision,
            DuplicateEntry(entry, fixture.root_id, "copy.txt", (CopyIdentity(entry, copy_id),)),
        )
    )
    intent = fixture.binding.palace_root / "copy-intents" / f"{operation}.json"
    assert intent.is_file()
    intent.unlink()
    with pytest.raises(ConsumerError) as refused:
        fixture.bound.derive(pipeline, copy_id, operation_id=identifier(), copied_by=operation)
    assert refused.value.code == "materialize-evidence-unavailable"
    assert not fixture.bound._shelf.mirror_path(copy_id).exists()


def test_a_directory_copy_materializes_only_when_named(tmp_path: Path) -> None:
    """Failure mode: reconcile recreates a missing mirror without a copy receipt, or a
    named copy never becomes searchable."""
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        copy = shelf.copy("notes.txt", "copy.txt")
        plain = shelf.reconcile(settings, embedder=HashEmbedder())
        assert (copy.entry_id, "mirror-missing") in plain.isolated
        assert not shelf.mirror_path(copy.entry_id).exists()
        named = shelf.reconcile(settings, embedder=HashEmbedder(), materialize=(copy.operation_id,))
        assert copy.entry_id in named.converted and shelf.verify().ok, shelf.verify().problems


def test_an_exchange_over_unexpected_bytes_is_undone(tmp_path: Path) -> None:
    """Failure mode: a write displaces bytes it did not expect (an edit made after the
    caller read the mirror) and loses or retains them instead of leaving them in place."""
    from biblios.mirrors import Provenance, write_mirror

    path = tmp_path / "entry.md"
    path.write_bytes(b"user edit")
    provenance = Provenance(tmp_path / "palace", "update", None, None, None, None)
    with pytest.raises(ConsumerError) as refused:
        write_mirror(
            path, b"new bytes", expected=_sha(b"what the caller saw"), provenance=provenance
        )
    assert refused.value.code == "mirror-changed-concurrently"
    assert path.read_bytes() == b"user edit", "the unexpected edit was displaced"
    # Biblios's own staged bytes are kept under a retained name too: deleting them after
    # a compare would race a writer that opened or renamed over the staging file.
    kept = retained_files(tmp_path, "entry")
    assert [file.read_bytes() for file in kept] == [b"new bytes"], "staging bytes not retained"
    assert not (tmp_path / "entry.partial").exists(), "staging name left occupied"


# ---- review-driven proofs (CODE-F001, F002, F003, F008) ------------------------------


def _two_entries(fixture, pipeline) -> str:
    other = fixture.bound.curate(
        fixture.request("tide.txt", b"Harbour tide tables.")
    ).mutation.affected_entry_ids[0]
    fixture.bound.derive(pipeline, other, operation_id=identifier())
    return other


def test_a_direct_edit_is_reindexed_without_conversion_or_any_write(tmp_path: Path) -> None:
    """Failure mode: re-indexing an edited mirror re-converts the source, rewrites the
    file, or keeps serving the text the user replaced."""
    fixture = consumer_fixture(tmp_path)
    try:
        calls: list = []
        pipeline, _ = _pipeline(fixture, calls)
        entry = fixture.bound.curate(
            fixture.request(data=b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry, operation_id=identifier())
        path = fixture.bound._shelf.mirror_path(entry)
        edit = _rendered("Speaker two: the keeper trims the wick.")
        path.write_bytes(edit)
        before = path.stat()
        converted = len(calls)

        status = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        after = path.stat()
        assert status.state == "indexed" and len(calls) == converted, "an edit was re-converted"
        assert path.read_bytes() == edit, "the edited mirror was rewritten"
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ), "the edited mirror file was replaced"
        assert not path.with_suffix(".partial").exists()
        assert not retained_files(path.parent, entry)
        assert entry in _hits(fixture, "keeper trims the wick")
        stale = fixture.bound.search(
            "Lighthouse lens maintenance", embedder=HashEmbedder(), reranker=OverlapReranker()
        )
        assert all("Lighthouse lens maintenance" not in hit.chunk for hit in stale.hits)
        assert fixture.bound.resolve(entry).metadata.converter == "biblios.mirror-edit"
    finally:
        fixture.close()


def test_reconcile_reindexes_a_direct_edit_without_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: the directory lane re-converts over an edited mirror."""
    import biblios.reconcile as reconcile_module

    shelf, settings = make_shelf(tmp_path)
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        path = shelf.mirror_path(notes.entry_id)
        edit = path.read_bytes() + b"\nSpeaker two adds a note on the fog signal.\n"
        path.write_bytes(edit)
        calls: list = []
        original = reconcile_module.convert_entry

        def counted(*args, **kwargs):
            calls.append(args[1].entry_id)
            return original(*args, **kwargs)

        monkeypatch.setattr(reconcile_module, "convert_entry", counted)
        report = shelf.reconcile(settings, embedder=HashEmbedder())
        assert calls == [] and report.converted == (), "reconcile re-converted an edit"
        assert notes.entry_id in report.indexed and path.read_bytes() == edit
        found = shelf.search("fog signal", embedder=HashEmbedder(), rerank=False)
        assert [hit.entry_id for hit in found.hits][:1] == [notes.entry_id]
        assert shelf.verify().ok, shelf.verify().problems


def test_an_update_serves_the_prior_mirror_until_publication_and_replays(seeded) -> None:
    """Failure mode: an update is visible before its index commit, or a retry after a
    failed publication embeds again."""
    fixture, pipeline, entry = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    original = path.read_bytes()
    entered, release = threading.Event(), threading.Event()

    class Held(HashEmbedder):
        def embed(self, texts):
            self.calls.append(tuple(texts))
            entered.set()
            assert release.wait(10)
            return HashEmbedder.embed(self, texts)

    held = Held()
    pipeline.embedder = held
    operation = identifier()
    text = _edited("Speaker names corrected.")
    errors: list[BaseException] = []

    def run() -> None:
        try:
            fixture.bound.update_mirror(
                pipeline,
                entry,
                operation_id=operation,
                markdown=text,
                expected_sha256=_sha(original),
            )
        except BaseException as error:  # noqa: BLE001 - reported to the test thread
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(10)
        assert path.read_bytes() == original, "the update was written before publication"
        assert fixture.bound.read(entry).text == original.decode()
    finally:
        release.set()
        worker.join(15)
    assert not errors and path.read_text() == text
    embedded = len(held.calls)
    replay = fixture.bound.update_mirror(
        pipeline, entry, operation_id=operation, markdown=text, expected_sha256=_sha(original)
    )
    assert replay.state == "indexed" and len(held.calls) == embedded, "replay embedded again"


def test_an_edit_racing_a_rolled_back_update_is_kept(seeded, monkeypatch) -> None:
    """Failure mode: rolling back a failed update overwrites an edit made after it."""
    import biblios.pipeline as pipeline_module

    fixture, pipeline, entry = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    original = path.read_bytes()
    late = _edited("A later edit by hand.").encode()

    def failing_commit(**kwargs):
        path.write_bytes(late)
        raise RuntimeError("palace refused")

    monkeypatch.setattr(pipeline_module, "commit_plan", failing_commit)
    with pytest.raises(Exception) as refused:
        fixture.bound.update_mirror(
            pipeline,
            entry,
            operation_id=identifier(),
            markdown=_edited("Update."),
            expected_sha256=_sha(original),
        )
    assert path.read_bytes() == late, "rollback overwrote a later edit"
    kept = {file.read_bytes() for file in retained_files(path.parent, entry)}
    assert original in kept, "the displaced original was not retained"
    assert getattr(refused.value, "code", None) == "derivation-failed"


def test_policy_refusals_of_an_edit_isolate_only_that_entry(seeded) -> None:
    """Failure mode: an edit refused only by the pipeline's policy stays pending forever
    and holds back finishing the shelf."""
    from dataclasses import replace

    fixture, pipeline, entry = seeded
    other = _two_entries(fixture, pipeline)
    path = fixture.bound._shelf.mirror_path(entry)
    pipeline.policy = replace(pipeline.policy, max_mirror_bytes=len(path.read_bytes()) + 16)
    path.write_bytes(_rendered("x" * 64))
    with pytest.raises(ConsumerError) as large:
        fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert large.value.code == "mirror-too-large"
    rows = {row.entry_id: row.mirror for row in fixture.bound.status().entries}
    assert rows.get(entry) == "mirror-too-large", "the refusal was not persisted"
    pipeline.finish(fixture.bound, operation_id=identifier())
    assert _hits(fixture, "Harbour tide tables") == {other}
    with pytest.raises(Exception, match="mirror-too-large"):
        fixture.bound.read(entry)


def test_a_missing_required_header_field_isolates_the_edit(seeded) -> None:
    """Failure mode: a consumer-declared required header field is not enforced, or its
    absence blocks the shelf."""
    from dataclasses import replace

    fixture, pipeline, entry = seeded
    other = _two_entries(fixture, pipeline)
    pipeline.policy = replace(pipeline.policy, required_header_fields=("speakers",))
    path = fixture.bound._shelf.mirror_path(entry)
    path.write_bytes(b"---\ntitle: notes\n---\n" + _rendered("Edited."))
    with pytest.raises(ConsumerError) as refused:
        fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert refused.value.code == "mirror-header-invalid"
    assert fixture.bound.status().isolated == 1, "the required-field refusal was not honoured"
    pipeline.finish(fixture.bound, operation_id=identifier())
    assert _hits(fixture, "Harbour tide tables") == {other}


@pytest.mark.parametrize(
    ("lane", "text", "required", "valid"),
    (
        pytest.param("consumer", "# Body\n\nNo front matter.\n", (), True, id="no-front-matter"),
        pytest.param(
            "consumer",
            "---\r\nspeakers: [a, b]\r\n---\r\n# Body\r\n",
            ("speakers",),
            True,
            id="crlf-required-present",
        ),
        pytest.param(
            "consumer", "---\n- not\n- a mapping\n---\n# Body\n", (), False, id="non-mapping"
        ),
        pytest.param("consumer", "---\nspeakers: [\n---\n# Body\n", (), False, id="unparseable"),
        pytest.param(
            "consumer", "---\ntitle: x\n---\n# Body\n", ("speakers",), False, id="required-missing"
        ),
        pytest.param(
            "envelope", "# Body without the Drawbridge header\n", (), False, id="envelope-unheaded"
        ),
    ),
)
def test_header_lanes_follow_their_contract(lane, text, required, valid) -> None:
    """Failure mode: the consumer lane imposes a schema it never declared, accepts
    unparseable or non-mapping front matter, or the envelope lane skips Drawbridge."""
    from biblios.mirrors import MirrorInvalid, validate_mirror

    if valid:
        assert validate_mirror(text.encode(), lane=lane, required_header_fields=required)
    else:
        with pytest.raises(MirrorInvalid) as refused:
            validate_mirror(text.encode(), lane=lane, required_header_fields=required)
        assert refused.value.code == "mirror-header-invalid"


def test_a_symlinked_mirror_isolates_without_being_followed(seeded, tmp_path: Path) -> None:
    """Failure mode: Biblios follows a symlink at a mirror path, or it blocks the shelf."""
    fixture, pipeline, entry = seeded
    other = _two_entries(fixture, pipeline)
    outside = tmp_path / "outside.md"
    outside.write_bytes(_rendered("Outside the shelf."))
    path = fixture.bound._shelf.mirror_path(entry)
    path.unlink()
    path.symlink_to(outside)
    rows = {row.entry_id: row.mirror for row in fixture.bound.status().entries}
    assert rows.get(entry) == "mirror-path-unsafe", "the symlinked mirror was followed"
    pipeline.finish(fixture.bound, operation_id=identifier())
    assert _hits(fixture, "Harbour tide tables") == {other}
    assert path.is_symlink() and outside.read_bytes() == _rendered("Outside the shelf.")


def test_isolation_survives_checkpoint_reopen_and_refuses_seal(tmp_path: Path) -> None:
    """Failure mode: a readable checkpoint hides an isolated entry, reopening forgets
    it, or a shelf seals with a mirror Biblios cannot index."""
    import json

    from biblios.errors import BibliosError

    shelf, settings = make_shelf(tmp_path / "dir")
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        shelf.mirror_path(notes.entry_id).write_bytes(b"\xff not unicode")
        report = shelf.reconcile(settings, embedder=HashEmbedder())
        assert (notes.entry_id, "mirror-unicode-invalid") in report.isolated
        with pytest.raises(BibliosError, match="mirror-states-unsettled"):
            shelf.seal()
        assert not shelf.sealed

    fixture = consumer_fixture(tmp_path / "consumer")
    try:
        pipeline, _ = _pipeline(fixture, [])
        entry = fixture.bound.curate(fixture.request(data=b"Lighthouse.")).mutation
        entry = entry.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry, operation_id=identifier())
        _two_entries(fixture, pipeline)
        fixture.bound._shelf.mirror_path(entry).write_bytes(b"\xff not unicode")
        pipeline.finish(fixture.bound, operation_id=identifier())
        fixture.bound.checkpoint()
        marker = json.loads((fixture.binding.palace_root / "consumer-readable.json").read_text())
        assert marker["withheld"] == {entry: "mirror-unicode-invalid"}
        assert entry not in marker["membership"]
        fixture.reopen()
        assert fixture.bound.status().isolated == 1, "isolation lost across reopen"
    finally:
        fixture.close()


def test_an_isolated_mirror_is_neither_a_donor_nor_a_stack_hit(tmp_path: Path) -> None:
    """Failure mode: an isolated entry donates its rows or answers in a stack search."""
    from biblios.consumer import open_bound_stack

    fixtures = [consumer_fixture(tmp_path / name) for name in ("a", "b")]
    try:
        pipelines = [_pipeline(fixture, [])[0] for fixture in fixtures]
        ids = []
        for fixture, pipeline in zip(fixtures, pipelines, strict=True):
            entry = fixture.bound.curate(
                fixture.request(data=b"Lighthouse lens maintenance.")
            ).mutation.affected_entry_ids[0]
            fixture.bound.derive(pipeline, entry, operation_id=identifier())
            ids.append(entry)
        fixtures[0].bound._shelf.mirror_path(ids[0]).write_bytes(b"\xff not unicode")
        with pytest.raises(ConsumerError) as refused:
            pipelines[0].donor(fixtures[0].bound, ids[0], operation_id=identifier())
        assert refused.value.code == "donor-not-complete"
        stack = open_bound_stack(tuple(fixture.bound for fixture in fixtures))
        found = stack.search("Lighthouse lens", embedder=HashEmbedder(), reranker=OverlapReranker())
        assert {hit.entry_id for hit in found.hits} == {ids[1]}, (
            "an isolated entry answered a stack search"
        )
    finally:
        for fixture in fixtures:
            fixture.close()


def test_a_crash_after_the_exchange_keeps_the_displaced_bytes(tmp_path: Path, monkeypatch) -> None:
    """Failure mode: an interruption between the exchange and retention strands the
    displaced bytes under the staging name, and the retry skips them."""
    import biblios.mirrors as mirrors

    path = tmp_path / "entry.md"
    previous = b"the displaced version"
    path.write_bytes(previous)
    provenance = mirrors.Provenance(
        tmp_path / "palace", "source-replaced", "cas-old", _sha(previous), "op", 3
    )
    original = mirrors._rename

    def crash_after_swap(source, target, flag):
        original(source, target, flag)
        if flag == "swap":
            raise KeyboardInterrupt("simulated crash")

    monkeypatch.setattr(mirrors, "_rename", crash_after_swap)
    with pytest.raises(KeyboardInterrupt):
        mirrors.write_mirror(path, b"new", expected=_sha(previous), provenance=provenance)
    monkeypatch.setattr(mirrors, "_rename", original)
    assert path.with_suffix(".partial").read_bytes() == previous

    outcome = mirrors.write_mirror(path, b"new", expected=_sha(previous), provenance=provenance)
    assert path.read_bytes() == b"new"
    assert [file.read_bytes() for file in outcome.retained] == [previous], (
        "the interrupted displaced bytes were not retained"
    )
    record = mirrors.retention_record(tmp_path / "palace", outcome.retained[0])
    assert record is not None and (record["reason"], record["source_cas_id"]) == (
        "interrupted-write",
        "cas-old",
    ), "the interrupted bytes lost their recorded provenance"


@pytest.mark.parametrize("mode", ("rename-save", "in-place", "open-descriptor"))
def test_discarding_staging_keeps_whatever_a_concurrent_writer_put_there(
    tmp_path: Path, mode: str
) -> None:
    """Failure mode: discarding Biblios's own unpublished mirror deletes bytes a user
    saved, wrote in place, or is still writing through an open descriptor."""
    import os

    from biblios.mirrors import Provenance, discard_staged

    path = tmp_path / "entry.md"
    own = b"biblios staged bytes"
    path.write_bytes(own)
    stream = None
    if mode == "rename-save":
        saved = tmp_path / "save.tmp"
        saved.write_bytes(b"user saved this")
        os.replace(saved, path)
    elif mode == "in-place":
        path.write_bytes(b"user wrote in place")
    else:
        stream = path.open("ab")
    provenance = Provenance(tmp_path / "palace", "staging-discarded", None, None, "op", 1)
    kept = discard_staged(path, own, provenance)
    if stream is not None:
        stream.write(b" and kept writing")
        stream.close()
    assert len(kept) == 1, "the staged file was deleted, not retained"
    assert not path.exists()
    expected = {
        "rename-save": b"user saved this",
        "in-place": b"user wrote in place",
        "open-descriptor": own + b" and kept writing",
    }[mode]
    assert kept[0].read_bytes() == expected, "a concurrent writer's bytes were lost"


def test_an_update_interrupted_after_its_vectors_resumes_without_embedding(
    seeded, monkeypatch
) -> None:
    """Failure mode: an update interrupted after its vectors were durably recorded embeds
    again when retried, or publishes nothing."""
    fixture, pipeline, entry = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    original = path.read_bytes()
    embedder = HashEmbedder()
    pipeline.embedder = embedder
    operation = identifier()
    text = _edited("Speaker three joins the watch.")
    publish = type(pipeline)._publish

    def interrupted(self, *args, **kwargs):
        raise RuntimeError("process stopped after vectors were recorded")

    monkeypatch.setattr(type(pipeline), "_publish", interrupted)
    with pytest.raises(ConsumerError):
        fixture.bound.update_mirror(
            pipeline, entry, operation_id=operation, markdown=text, expected_sha256=_sha(original)
        )
    assert path.read_bytes() == original and embedder.calls, "nothing was embedded first"
    embedded = len(embedder.calls)
    monkeypatch.setattr(type(pipeline), "_publish", publish)
    status = fixture.bound.update_mirror(
        pipeline, entry, operation_id=operation, markdown=text, expected_sha256=_sha(original)
    )
    assert len(embedder.calls) == embedded, "the interrupted update embedded again"
    assert status.state == "indexed" and path.read_text() == text
    assert entry in _hits(fixture, "Speaker three joins the watch")
