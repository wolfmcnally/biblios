"""A caller-nominated donor lends its recorded converted body and, only for identical
ordered embedding inputs, its vectors; it is never selected automatically, and a nominee
from another binding or with a changed view refuses before any provider call."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from biblios.curation import CreateDirectory, CreateFile, CurationRequest, DeleteEntry
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import ADDED, consumer_fixture, identifier, lane_pipeline


def _pipeline(fixture, calls: list, embedder: HashEmbedder):
    return lane_pipeline(fixture, calls, embedder=embedder, reuse_converted_body=True)


def _submitted(embedder: HashEmbedder, since: int) -> list[str]:
    return [text for call in embedder.calls[since:] for text in call]


def _accepted(fixture, entry_id: str) -> tuple[dict, list[str]]:
    """The entry's accepted stage canonical and its accepted vector digests, in order."""
    import json
    import sqlite3

    from biblios.derived import DERIVED_NAME

    conn = sqlite3.connect(fixture.binding.palace_root / DERIVED_NAME)
    try:
        [canonical] = conn.execute(
            "SELECT canonical FROM accepted_stages WHERE entry_id=?", (entry_id,)
        ).fetchone()
        vectors = [
            row[0]
            for row in conn.execute(
                "SELECT vector_sha256 FROM accepted_chunks WHERE entry_id=? ORDER BY position",
                (entry_id,),
            )
        ]
    finally:
        conn.close()
    return json.loads(bytes(canonical)), vectors


@pytest.mark.parametrize("case", ("identical", "different-name", "path-in-name-input"))
def test_a_nominated_duplicate_reuses_its_name_row_only_for_a_complete_identical_input(
    tmp_path: Path, case: str
) -> None:
    """Failure mode: a duplicate repeats the converter or embeds unchanged rows again;
    the donor's name vector is reused for a name input that differs in any way (name or
    a path the input composes); or the destination keeps answering through the removed
    donor."""
    fixture = consumer_fixture(tmp_path)
    calls: list = []
    embedder = HashEmbedder()
    try:
        pipeline = _pipeline(fixture, calls, embedder)
        if case == "path-in-name-input":
            # Like a breadcrumb input: the name row's input carries the stored path, and
            # every entry's mirror has its own path.
            pipeline.input_view = lambda plan: (
                [row.body for row in plan.rows[:-1]] + [plan.stored + "\n" + plan.rows[-1].body]
            )
        source = fixture.bound.curate(
            fixture.request("one.txt", b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        initial = fixture.bound.derive(pipeline, source, operation_id=identifier())
        donor = fixture.bound.donor(source)
        folder = fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                CreateDirectory(fixture.root_id, "folder"),
            )
        ).mutation.affected_entry_ids[0]
        name = "two.txt" if case == "different-name" else "one.txt"
        destination = fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                CreateFile(folder, name, b"Lighthouse lens maintenance.", "text/plain", ADDED),
            )
        ).mutation.affected_entry_ids[0]
        before = len(embedder.calls)
        second = fixture.bound.derive(pipeline, destination, operation_id=identifier(), donor=donor)
        new_inputs = _submitted(embedder, before)
        outcome = second.outcome(destination)
        canonical, vectors = _accepted(fixture, destination)
        _donor_canonical, donor_vectors = _accepted(fixture, source)
        assert (canonical["donor"]["entry_id"], canonical["donor"]["stage_sha256"]) == (
            source,
            donor.stage_sha256,
        )
        assert (initial.outcome(source).outcome, outcome.outcome, len(calls)) == (
            "searchable",
            "searchable",
            1,
        )
        if case == "identical":
            assert (outcome.reused, outcome.embedded, new_inputs) == (3, 0, [])
            assert vectors == donor_vectors and second.provider_calls == 0
        else:
            assert (outcome.reused, outcome.embedded, len(new_inputs)) == (2, 1, 1)
            assert new_inputs[0].splitlines()[-1] == name
            assert vectors[:2] == donor_vectors[:2] and vectors[2] != donor_vectors[2]
        mirror = fixture.bound._shelf.mirror_path(destination).read_text()
        assert mirror == fixture.bound._shelf.mirror_path(source).read_text()
        assert mirror.startswith("# Body\n\nLighthouse lens maintenance.\n") and name not in mirror
        fixture.bound.curate(
            CurationRequest(identifier(), fixture.bound.view.revision, DeleteEntry(source))
        )
        removed = fixture.bound.derive(pipeline, source, operation_id=identifier())
        assert removed.outcome(source).outcome == "removed"
        found = fixture.bound.search(
            "Lighthouse lens", embedder=HashEmbedder(), reranker=OverlapReranker()
        )
        assert {hit.entry_id for hit in found.hits} == {destination}
        assert fixture.bound.download(destination) == b"Lighthouse lens maintenance."
    finally:
        fixture.close()


def test_without_a_nomination_nothing_is_reused_from_an_identical_entry(tmp_path: Path) -> None:
    """Failure mode: reuse is selected automatically from any identical source."""
    fixture = consumer_fixture(tmp_path)
    calls: list = []
    embedder = HashEmbedder()
    try:
        pipeline = _pipeline(fixture, calls, embedder)
        source = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, source, operation_id=identifier())
        destination = fixture.bound.curate(fixture.request("copy.txt")).mutation.affected_entry_ids[
            0
        ]
        outcome = fixture.bound.derive(pipeline, destination, operation_id=identifier()).outcome(
            destination
        )
        assert (outcome.outcome, outcome.reused, outcome.embedded, len(calls)) == (
            "searchable",
            0,
            3,
            2,
        )
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "case", ("missing", "different-input", "reordered-input", "binding", "view")
)
def test_donor_eligibility_uses_actual_ordered_inputs_and_current_same_shelf_view(
    tmp_path: Path, case: str
) -> None:
    """Failure mode: a stale, foreign or changed nominee dispatches destination providers
    or modifies its bytes, or vectors are reused for inputs that differ or are reordered."""
    fixture = consumer_fixture(tmp_path)
    calls: list = []
    embedder = HashEmbedder()
    try:
        pipeline = _pipeline(fixture, calls, embedder)
        source = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, source, operation_id=identifier())
        donor = fixture.bound.donor(source)
        destination = fixture.bound.curate(fixture.request("copy.txt")).mutation.affected_entry_ids[
            0
        ]
        if case == "missing":
            fixture.bound.curate(
                CurationRequest(identifier(), fixture.bound.view.revision, DeleteEntry(source))
            )
        elif case == "different-input":
            pipeline.input_view = lambda plan: ["prefix:" + row.body for row in plan.rows]
        elif case == "reordered-input":
            pipeline.input_view = lambda plan: [row.body for row in reversed(plan.rows)]
        elif case == "binding":
            donor = replace(donor, namespace_id=identifier())
        else:
            donor = replace(donor, stage_sha256="0" * 64)
        count = len(embedder.calls)
        result = fixture.bound.derive(pipeline, destination, operation_id=identifier(), donor=donor)
        outcome = result.outcome(destination)
        if case in {"binding", "view"}:
            assert (outcome.code, len(embedder.calls), len(calls)) == (
                "donor-binding-mismatch" if case == "binding" else "donor-view-changed",
                count,
                1,
            )
            assert fixture.bound.download(destination) == b"fixture body"
            assert not fixture.bound._shelf.mirror_path(destination).exists()
        else:
            assert (
                outcome.outcome,
                outcome.reused,
                outcome.embedded,
                len(_submitted(embedder, count)),
                len(calls),
                fixture.bound.download(destination),
            ) == (
                "searchable",
                0,
                3,
                3,
                2 if case == "missing" else 1,
                b"fixture body",
            )
    finally:
        fixture.close()


def test_a_donor_that_is_not_exactly_its_accepted_record_is_never_captured_or_reused(
    tmp_path: Path,
) -> None:
    """Failure mode: an unaccepted entry is captured as a donor, or a donor edited after
    capture lends vectors or a body its current mirror no longer carries."""
    from biblios.control import ConsumerError

    fixture = consumer_fixture(tmp_path)
    calls: list = []
    embedder = HashEmbedder()
    try:
        pipeline = _pipeline(fixture, calls, embedder)
        source = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.donor(source)
        assert refused.value.code == "donor-not-complete"
        fixture.bound.derive(pipeline, source, operation_id=identifier())
        donor = fixture.bound.donor(source)
        mirror = fixture.bound._shelf.mirror_path(source)
        mirror.write_bytes(mirror.read_bytes().replace(b"fixture body", b"edited body"))
        destination = fixture.bound.curate(fixture.request("copy.txt")).mutation.affected_entry_ids[
            0
        ]
        count = len(embedder.calls)
        outcome = fixture.bound.derive(
            pipeline, destination, operation_id=identifier(), donor=donor
        ).outcome(destination)
        assert (outcome.outcome, outcome.reused, outcome.embedded, len(calls)) == (
            "searchable",
            0,
            3,
            2,
        )
        assert len(_submitted(embedder, count)) == 3
        assert b"fixture body" in fixture.bound._shelf.mirror_path(destination).read_bytes()
    finally:
        fixture.close()


def test_a_store_that_cannot_be_read_as_donor_proof_refuses_with_palaces_cause(
    tmp_path: Path, monkeypatch
) -> None:
    """Failure mode: a real Palace refusal while proving the donor is collapsed into an
    ordinary miss, or reported without the dependency error that caused it."""
    import palace.index

    fixture = consumer_fixture(tmp_path)
    calls: list = []
    embedder = HashEmbedder()
    try:
        pipeline = _pipeline(fixture, calls, embedder)
        source = fixture.bound.curate(fixture.request()).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, source, operation_id=identifier())
        donor = fixture.bound.donor(source)
        destination = fixture.bound.curate(fixture.request("copy.txt")).mutation.affected_entry_ids[
            0
        ]
        origin = palace.index.IndexError("committed file vector payload is malformed")

        def unreadable(**kwargs):
            raise origin

        monkeypatch.setattr(palace.index, "read_file_vectors", unreadable)
        outcome = fixture.bound.derive(
            pipeline, destination, operation_id=identifier(), donor=donor
        ).outcome(destination)
        assert outcome.code == "donor-view-changed" and outcome.cause.__cause__ is origin
        assert len(calls) == 1
    finally:
        fixture.close()


def test_a_store_whose_recorded_identity_disagrees_refuses_and_never_rebuilds(
    tmp_path: Path,
) -> None:
    """Failure mode: committed vectors recorded under a different provider, model or
    representation than the store declares are reused, treated as an ordinary miss that
    silently re-embeds, or trigger an automatic rebuild."""
    import sqlite3

    fixture = consumer_fixture(tmp_path)
    calls: list = []
    embedder = HashEmbedder()
    try:
        pipeline = _pipeline(fixture, calls, embedder)
        source = fixture.bound.curate(fixture.request("one.txt")).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, source, operation_id=identifier())
        donor = fixture.bound.donor(source)
        destination = fixture.bound.curate(fixture.request("two.txt")).mutation.affected_entry_ids[
            0
        ]
        database = fixture.bound._shelf.chunks_db
        conn = sqlite3.connect(database)
        conn.execute("UPDATE index_meta SET value='another-model' WHERE key='embed_model'")
        conn.commit()
        recorded = conn.execute("SELECT key, value FROM index_meta ORDER BY key").fetchall()
        conn.close()
        from biblios.control import ConsumerError

        count = len(embedder.calls)
        for nominated in (donor, None):
            # With or without a nominee: refused before any download, provider or write.
            with pytest.raises(ConsumerError) as refused:
                fixture.bound.derive(
                    pipeline, destination, operation_id=identifier(), donor=nominated
                )
            assert (refused.value.code, refused.value.effect) == (
                "index-identity-mismatch",
                "recovery-required",
            )
            assert type(refused.value.__cause__).__module__.startswith("palace")
        assert (len(embedder.calls), len(calls)) == (count, 1)
        conn = sqlite3.connect(database)
        assert conn.execute("SELECT key, value FROM index_meta ORDER BY key").fetchall() == recorded
        conn.close()
        assert not fixture.bound._shelf.mirror_path(destination).exists()
    finally:
        fixture.close()
