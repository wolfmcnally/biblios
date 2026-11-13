"""User-owned mirrors: edits are kept, replacement is a new ingestion, corruption isolates."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from biblios.control import ConsumerError
from biblios.curation import CopyIdentity, CurationRequest, DuplicateEntry, ReplaceContent
from biblios.mirrors import retained_files
from biblios.pipeline import ConsumerPipeline, DerivationPolicy
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import LANE_POLICY_FIELDS, consumer_fixture, identifier, lane_pipeline
from tests.support import NOTES, TIDE, make_shelf

POLICY = DerivationPolicy(*LANE_POLICY_FIELDS)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _edited(body: str) -> str:
    return f"# Body\n\n{body}\n"


def _rendered(body: str) -> bytes:
    """The fixture renderer's output for converted ``body``: content only, no name."""
    return f"# Body\n\n{body}\n\n## Extra\n\nIndependent body row.\n".encode()


def _pipeline(fixture, calls: list) -> ConsumerPipeline:
    return lane_pipeline(fixture, calls)


def _hits(fixture, query: str) -> set[str]:
    found = fixture.bound.search(query, embedder=HashEmbedder(), reranker=OverlapReranker())
    return {hit.entry_id for hit in found.hits}


def _outcome(result, entry: str) -> tuple[str, str | None]:
    item = result.outcome(entry)
    return item.outcome, item.code


@pytest.fixture
def seeded(tmp_path: Path):
    fixture = consumer_fixture(tmp_path)
    try:
        calls: list = []
        pipeline = _pipeline(fixture, calls)
        entry = fixture.bound.curate(
            fixture.request(data=b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
        assert _outcome(result, entry) == ("searchable", None)
        yield fixture, pipeline, entry, calls
    finally:
        fixture.close()


def _replace(fixture, entry: str, data: bytes) -> None:
    fixture.bound.curate(
        CurationRequest(identifier(), fixture.bound.view.revision, ReplaceContent(entry, data))
    )


def _second(fixture, pipeline) -> str:
    other = fixture.bound.curate(
        fixture.request("tide.txt", b"Harbour tide tables.")
    ).mutation.affected_entry_ids[0]
    fixture.bound.derive(pipeline, other, operation_id=identifier())
    return other


def test_replacement_is_a_new_ingestion_that_keeps_an_unindexed_edit(seeded) -> None:
    """Failure mode: replacing a file overwrites, deletes or moves the user's mirror when
    it holds a direct edit not yet re-indexed, converts over it, or keeps the old mirror
    searchable (plan F034: the edit stays in place, reported, and nothing converts)."""
    fixture, pipeline, entry, calls = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    edit = _edited("Lighthouse lens maintenance, corrected by hand.").encode()
    path.write_bytes(edit)
    inode = path.stat().st_ino
    _replace(fixture, entry, b"Harbour tide tables.")
    before = len(calls)
    result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert _outcome(result, entry) == ("pending", "replaced-mirror-edited")
    assert path.read_bytes() == edit and path.stat().st_ino == inode
    assert retained_files(fixture.binding.mirror_root, entry) == []
    assert len(calls) == before, "a conversion ran over the user's edit"
    assert entry not in _hits(fixture, "Harbour tide tables")
    found = fixture.bound.search(
        "corrected by hand", embedder=HashEmbedder(), reranker=OverlapReranker()
    )
    assert all("corrected" not in hit.chunk for hit in found.hits)


def test_an_edit_during_a_replacement_window_is_kept(seeded) -> None:
    """Failure mode: an edit made while the new conversion embeds is overwritten; the
    exchange displaces only the bytes it expected and swaps back otherwise."""
    fixture, pipeline, entry, _calls = seeded
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
    results: list = []
    worker = threading.Thread(
        target=lambda: results.append(
            fixture.bound.derive(pipeline, entry, operation_id=identifier())
        )
    )
    worker.start()
    try:
        assert entered.wait(10)
        path.write_bytes(edit)
    finally:
        release.set()
        worker.join(15)
    assert _outcome(results[0], entry)[1] == "mirror-changed-concurrently"
    assert path.read_bytes() == edit
    assert all(file.read_bytes() != edit for file in retained_files(path.parent, entry))


def test_a_corrupt_mirror_isolates_only_its_entry(seeded) -> None:
    """Failure mode: one unreadable mirror blocks the shelf or another entry's search."""
    fixture, pipeline, entry, _calls = seeded
    other = _second(fixture, pipeline)
    broken = b"\xff\xfe not unicode"
    fixture.bound._shelf.mirror_path(entry).write_bytes(broken)
    result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert _outcome(result, entry) == ("isolated:mirror-unicode-invalid", None)
    assert _hits(fixture, "Harbour tide tables") == {other}
    with pytest.raises(Exception, match="mirror-unicode-invalid"):
        fixture.bound.read(entry)
    assert fixture.bound._shelf.mirror_path(entry).read_bytes() == broken


def test_an_update_is_validated_at_its_seam_and_keeps_the_previous_mirror(seeded) -> None:
    """Failure mode: a malformed update lands, a stale update overwrites a newer edit,
    or a valid update discards the bytes it replaced."""
    from dataclasses import replace

    fixture, pipeline, entry, _calls = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    original = path.read_bytes()
    policy = pipeline.policy
    pipeline.policy = replace(policy, max_mirror_bytes=len(original))
    invalid = fixture.bound.update_mirror(
        pipeline,
        entry,
        operation_id=identifier(),
        markdown=_edited("x" * len(original)),
        expected_sha256=_sha(original),
    )
    pipeline.policy = policy
    assert _outcome(invalid, entry)[1] == "mirror-update-invalid:mirror-too-large"
    stale = fixture.bound.update_mirror(
        pipeline,
        entry,
        operation_id=identifier(),
        markdown=_edited("Stale."),
        expected_sha256=_sha(b"something else"),
    )
    assert _outcome(stale, entry)[0] != "searchable"
    assert path.read_bytes() == original and not retained_files(path.parent, entry)

    text = _edited("Speaker names corrected in the harbour log.")
    done = fixture.bound.update_mirror(
        pipeline, entry, operation_id=identifier(), markdown=text, expected_sha256=_sha(original)
    )
    assert _outcome(done, entry) == ("searchable", None) and path.read_text() == text
    assert [file.read_bytes() for file in retained_files(path.parent, entry)] == [original]
    assert entry in _hits(fixture, "Speaker names corrected")


def test_a_copy_gets_one_first_mirror_from_its_own_grant(seeded) -> None:
    """Failure mode: a copy never becomes searchable, its grant recreates a mirror the
    user deleted, or a copy with no recorded grant is materialized from Castle alone."""
    import sqlite3

    from biblios.derived import VOLATILE_NAME

    fixture, pipeline, entry, _calls = seeded
    copies = [identifier(), identifier()]
    for copy_id, name in zip(copies, ("copy.txt", "other.txt"), strict=True):
        fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                DuplicateEntry(entry, fixture.root_id, name, (CopyIdentity(entry, copy_id),)),
            )
        )
    first, second = copies
    conn = sqlite3.connect(fixture.binding.palace_root / VOLATILE_NAME, isolation_level=None)
    conn.execute("DELETE FROM grants WHERE entry_id=?", (second,))
    conn.close()
    granted = fixture.bound.derive(pipeline, first, operation_id=identifier())
    path = fixture.bound._shelf.mirror_path(first)
    assert _outcome(granted, first) == ("searchable", None) and path.is_file()
    path.unlink()
    spent = fixture.bound.derive(pipeline, first, operation_id=identifier())
    assert _outcome(spent, first) == ("isolated:mirror-missing", None) and not path.exists()
    ungranted = fixture.bound.derive(pipeline, second, operation_id=identifier())
    assert _outcome(ungranted, second) == ("unrecorded", "creation-grant-required")
    assert not fixture.bound._shelf.mirror_path(second).exists()


def test_a_direct_edit_is_reindexed_only_when_named_without_conversion(seeded) -> None:
    """Failure mode: re-indexing an edited mirror re-converts the source, or keeps
    serving the text the user replaced."""
    fixture, pipeline, entry, calls = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    edit = _rendered("Speaker two: the keeper trims the wick.")
    path.write_bytes(edit)
    converted = len(calls)
    result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert _outcome(result, entry) == ("searchable", None) and len(calls) == converted
    assert path.read_bytes() == edit and not retained_files(path.parent, entry)
    assert entry in _hits(fixture, "keeper trims the wick")
    stale = fixture.bound.search(
        "Lighthouse lens maintenance", embedder=HashEmbedder(), reranker=OverlapReranker()
    )
    assert all("Lighthouse lens maintenance" not in hit.chunk for hit in stale.hits)


def test_an_update_serves_the_prior_mirror_until_publication_and_replays(seeded) -> None:
    """Failure mode: an update is visible before its index commit, or a replay embeds
    again."""
    fixture, pipeline, entry, _calls = seeded
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
    results: list = []

    def run() -> None:
        results.append(
            fixture.bound.update_mirror(
                pipeline,
                entry,
                operation_id=operation,
                markdown=text,
                expected_sha256=_sha(original),
            )
        )

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(10)
        assert path.read_bytes() == original, "the update was written before publication"
        assert fixture.bound.read(entry).text == original.decode()
    finally:
        release.set()
        worker.join(15)
    assert _outcome(results[0], entry) == ("searchable", None) and path.read_text() == text
    embedded = len(held.calls)
    replay = fixture.bound.update_mirror(
        pipeline, entry, operation_id=operation, markdown=text, expected_sha256=_sha(original)
    )
    assert replay.replayed and len(held.calls) == embedded, "replay embedded again"


def test_an_edit_racing_a_failed_update_is_kept(seeded, monkeypatch) -> None:
    """Failure mode: a failed update, or its later resumption, overwrites an edit made
    after the exchange, or loses the bytes the exchange displaced."""
    import palace.index

    fixture, pipeline, entry, _calls = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    original = path.read_bytes()
    late = _edited("A later edit by hand.").encode()

    def failing_commit(**kwargs):
        path.write_bytes(late)
        raise palace.index.IndexError("palace refused")

    monkeypatch.setattr(palace.index, "commit_plan", failing_commit)
    failed = fixture.bound.update_mirror(
        pipeline,
        entry,
        operation_id=identifier(),
        markdown=_edited("Update."),
        expected_sha256=_sha(original),
    )
    assert _outcome(failed, entry) == ("recovery-required", "publication-failed")
    assert path.read_bytes() == late
    assert original in {file.read_bytes() for file in retained_files(path.parent, entry)}
    monkeypatch.undo()
    fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert path.read_bytes() == late, "resuming the update overwrote a later edit"


def test_policy_refusals_of_an_edit_isolate_only_that_entry(seeded) -> None:
    """Failure mode: an edit refused by the declared policy blocks the shelf, or a header
    that is not YAML isolates the entry (a header is never an authority, so it can only
    contribute searchable metadata, never refuse the content)."""
    from dataclasses import replace

    fixture, pipeline, entry, _calls = seeded
    other = _second(fixture, pipeline)
    path = fixture.bound._shelf.mirror_path(entry)
    pipeline.policy = replace(pipeline.policy, max_mirror_bytes=len(path.read_bytes()) + 16)
    path.write_bytes(_rendered("x" * 64))
    large = fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert "mirror-too-large" in str(_outcome(large, entry))
    assert _hits(fixture, "Harbour tide tables") == {other}
    pipeline.policy = POLICY
    path.write_bytes(b"---\nspeakers: [\n---\n" + _rendered("Edited lighthouse ledger."))
    header = fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert _outcome(header, entry) == ("searchable", None)
    assert entry in _hits(fixture, "Edited lighthouse ledger")


def test_a_symlinked_mirror_isolates_without_being_followed(seeded, tmp_path: Path) -> None:
    """Failure mode: Biblios follows a symlink at a mirror path, or it blocks the shelf."""
    fixture, pipeline, entry, _calls = seeded
    other = _second(fixture, pipeline)
    outside = tmp_path / "outside.md"
    outside.write_bytes(_rendered("Outside the shelf."))
    path = fixture.bound._shelf.mirror_path(entry)
    path.unlink()
    path.symlink_to(outside)
    result = fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert "mirror-path-unsafe" in str(_outcome(result, entry))
    assert _hits(fixture, "Harbour tide tables") == {other}
    assert path.is_symlink() and outside.read_bytes() == _rendered("Outside the shelf.")


def test_isolation_survives_reopen_and_refuses_seal(tmp_path: Path) -> None:
    """Failure mode: reopening forgets an isolated entry, or a shelf seals with a mirror
    Biblios cannot index."""
    from biblios import open_shelf
    from biblios.errors import BibliosError
    from biblios.progress import diagnose

    shelf, settings = make_shelf(tmp_path)
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        shelf.mirror_path(notes.entry_id).write_bytes(b"\xff not unicode")
        report = shelf.reconcile(settings, embedder=HashEmbedder(), entry_ids=[notes.entry_id])
        assert report.outcome(notes.entry_id).outcome == "isolated:mirror-unicode-invalid"
        with pytest.raises(BibliosError, match="mirror-states-unsettled"):
            shelf.seal()
        assert not shelf.sealed
    with open_shelf(shelf.root) as reopened:
        assert diagnose(reopened)[0].isolated == 1, "isolation lost across reopen"


def test_an_isolated_mirror_is_never_a_stack_hit(tmp_path: Path) -> None:
    """Failure mode: an isolated entry answers in a stack search."""
    from biblios.consumer import open_bound_stack

    fixtures = [consumer_fixture(tmp_path / name) for name in ("a", "b")]
    try:
        ids = []
        for fixture in fixtures:
            pipeline = _pipeline(fixture, [])
            entry = fixture.bound.curate(
                fixture.request(data=b"Lighthouse lens maintenance.")
            ).mutation.affected_entry_ids[0]
            fixture.bound.derive(pipeline, entry, operation_id=identifier())
            ids.append(entry)
        fixtures[0].bound._shelf.mirror_path(ids[0]).write_bytes(b"\xff not unicode")
        stack = open_bound_stack(tuple(fixture.bound for fixture in fixtures))
        found = stack.search("Lighthouse lens", embedder=HashEmbedder(), reranker=OverlapReranker())
        assert {hit.entry_id for hit in found.hits} == {ids[1]}
    finally:
        for fixture in fixtures:
            fixture.close()


def test_an_update_interrupted_after_its_vectors_resumes_without_embedding(
    seeded, monkeypatch
) -> None:
    """Failure mode: an update interrupted after its vectors were persisted embeds again
    when the entry is next brought current, or publishes nothing."""
    import palace.index

    fixture, pipeline, entry, _calls = seeded
    path = fixture.bound._shelf.mirror_path(entry)
    original = path.read_bytes()
    embedder = HashEmbedder()
    pipeline.embedder = embedder
    text = _edited("Speaker three joins the watch.")

    def interrupted(**kwargs):
        raise palace.index.IndexError("process stopped after vectors were recorded")

    monkeypatch.setattr(palace.index, "commit_plan", interrupted)
    failed = fixture.bound.update_mirror(
        pipeline, entry, operation_id=identifier(), markdown=text, expected_sha256=_sha(original)
    )
    assert _outcome(failed, entry)[1] == "publication-failed" and embedder.calls
    embedded = len(embedder.calls)
    monkeypatch.undo()
    resumed = fixture.bound.derive(pipeline, entry, operation_id=identifier())
    assert len(embedder.calls) == embedded, "the interrupted update embedded again"
    assert _outcome(resumed, entry) == ("searchable", None) and path.read_text() == text
    assert entry in _hits(fixture, "Speaker three joins the watch")


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


def test_a_digest_bound_retirement_never_takes_bytes_written_meanwhile(
    tmp_path: Path, monkeypatch
) -> None:
    """Failure mode: retiring the core's own bytes moves away a user's write that landed
    between the ownership check and the move."""
    import biblios.mirrors as mirrors
    from biblios.mirrors import Provenance, retire_mirror

    path = tmp_path / "entry.md"
    path.write_bytes(b"own staging")
    own = _sha(b"own staging")
    original = mirrors._retain

    def user_writes_first(file, root, entry_id, provenance, label=None):
        file.write_bytes(b"user edit")  # lands after the caller's check
        return original(file, root, entry_id, provenance, label)

    monkeypatch.setattr(mirrors, "_retain", user_writes_first)
    provenance = Provenance(tmp_path / "palace", "staging", None, None, None, None)
    with pytest.raises(ConsumerError) as refused:
        retire_mirror(path, provenance=provenance, expected=own)
    assert refused.value.code == "mirror-changed-concurrently"
    assert path.read_bytes() == b"user edit"
    assert [p.name for p in tmp_path.iterdir() if ".retained-" in p.name] == []
