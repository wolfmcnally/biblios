"""Excision disposes the selected entries' rows in Biblios's own stores, keeps the
operation and curation replay ledgers, and refuses before any effect while a selected
entry has a persisted stage that no authenticated cancellation has disposed."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from biblios.control import ConsumerError
from biblios.core import StageRefused
from biblios.derived import DERIVED_NAME, VOLATILE_NAME
from biblios.history import ExcisionRequest, ExcisionSelection
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import consumer_fixture, identifier, inventory, lane_pipeline

MARKER = b"selected disclosure marker"


def _rows(database: Path, table: str) -> list[tuple]:
    with sqlite3.connect(database) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def _store_files(palace: Path) -> list[Path]:
    return [file for file in palace.iterdir() if file.name.startswith("biblios")]


def _excise(fixture, entry_id: str):
    request = ExcisionRequest(
        identifier(),
        fixture.bound.view.revision,
        ExcisionSelection(entry_ids=(entry_id,)),
        (),
        fixture.binding.root / "successor",
    )
    return fixture.bound.excise(fixture.bound.preview_excision(request).request)


def test_excision_disposes_selected_rows_and_keeps_the_replay_ledgers(tmp_path: Path) -> None:
    """Failure mode: an excised entry's conversion body, rendered mirror or accepted rows
    survive in Biblios's own stores (or their journals); or the disposal also erases the
    operation or curation ledgers that refuse a replayed request, or another entry's
    records and counts; or the result's read-back inventory cannot see a leftover."""
    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [])
        selected = fixture.bound.curate(
            fixture.request("selected.txt", MARKER)
        ).mutation.affected_entry_ids[0]
        kept = fixture.bound.curate(
            fixture.request("kept.txt", b"lighthouse lens")
        ).mutation.affected_entry_ids[0]
        for entry_id in (selected, kept):
            fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        palace = fixture.binding.palace_root
        derived, volatile = palace / DERIVED_NAME, palace / VOLATILE_NAME
        assert any(MARKER in file.read_bytes() for file in _store_files(palace))
        operations = _rows(derived, "operations")
        curation = _rows(volatile, "curation_requests")
        from biblios.history import _residue

        before = dict(_residue(fixture.binding, [selected], []))
        assert before["derived.entries"] == 1 and before["mirror.files"] == 1
        assert before["palace.files"] == 1
        result = _excise(fixture, selected)
        # The read-back inventory names every class and finds nothing left of the selection.
        assert {name for name, _ in result.residue} == set(before)
        assert all(count == 0 for _, count in result.residue), result.residue
        assert not any(MARKER in file.read_bytes() for file in _store_files(palace))
        assert (_rows(derived, "operations"), _rows(volatile, "curation_requests")) == (
            operations,
            curation,
        )
        assert [row[0] for row in _rows(derived, "entries")] == [kept]
        counters = dict(_rows(derived, "counters"))
        assert counters.get("state:accepted") == 1
        for table in ("checkpoints", "accepted_stages", "accepted_chunks"):
            assert {
                row[1] if table == "checkpoints" else row[0] for row in _rows(derived, table)
            } == {kept}
    finally:
        fixture.close()


def _contents(found: dict) -> dict:
    return {
        name: value[0]
        for name, value in found.items()
        if not name.endswith(("-wal", "-shm")) and value[0] != "directory"
    }


class _Held:
    """A caller that authorizes the stage but never promotes it, so it stays persisted."""

    def authorize_creation(self, creation, entry_id):
        return b"admitted"

    def authorize(self, stage):
        return b"authorized"

    def promote(self, stage, verified):
        raise StageRefused("held")

    def seal_checkpoint(self, checkpoint):
        return checkpoint.sha256.encode()

    def verify_checkpoint(self, checkpoint, seal):
        return seal == checkpoint.sha256.encode()

    def recheck(self, stage, verified, authorization, promotion):
        return "pending"


def test_a_selected_entry_with_a_persisted_stage_refuses_excision_before_any_effect(
    tmp_path: Path,
) -> None:
    """Failure mode: excision removes an entry's accepted and provenance rows while its
    persisted stage stays replayable, or cancels that stage without the caller's
    authenticated decision."""
    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [])
        entry_id = fixture.bound.curate(
            fixture.request("held.txt", MARKER)
        ).mutation.affected_entry_ids[0]
        held = fixture.bound.derive(
            pipeline, entry_id, operation_id=identifier(), authority=_Held()
        )
        assert held.outcome(entry_id).code == "held"
        before = inventory(fixture.binding.root)
        revision = fixture.bound.view.revision
        with pytest.raises(ConsumerError) as refused:
            _excise(fixture, entry_id)
        assert (refused.value.code, refused.value.effect) == ("stage-pending", "none")
        assert fixture.bound.view.revision == revision
        # The read that previews the closure may open SQLite sidecars; no content moves.
        assert _contents(inventory(fixture.binding.root)) == _contents(before)
    finally:
        fixture.close()


def test_a_selected_earlier_content_is_disposed_under_the_entry_that_recorded_it(
    tmp_path: Path,
) -> None:
    """Failure mode: excising an earlier source content of a live entry selects no entry,
    so its recorded conversion body survives in Biblios's stores; or the disposal widens
    to the entry's current content and its accepted records."""
    from biblios.curation import CurationRequest, ReplaceContent

    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [])
        entry_id = fixture.bound.curate(
            fixture.request("a.txt", MARKER)
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        earlier = fixture.bound.resolve(entry_id).cas_id
        fixture.bound.curate(
            CurationRequest(
                identifier(), fixture.bound.view.revision, ReplaceContent(entry_id, b"current text")
            )
        )
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        palace = fixture.binding.palace_root
        derived = palace / DERIVED_NAME
        current = [row for row in _rows(derived, "checkpoints") if row[2] != earlier]
        accepted = _rows(derived, "accepted_stages")
        request = ExcisionRequest(
            identifier(),
            fixture.bound.view.revision,
            ExcisionSelection(content_ids=(earlier,)),
            (),
            fixture.binding.root / "successor",
        )
        proposal = fixture.bound.preview_excision(request)
        assert proposal.entry_ids == ()  # the closure names no entry for this selection
        fixture.bound.excise(proposal.request)
        assert not any(MARKER in file.read_bytes() for file in _store_files(palace))
        assert _rows(derived, "checkpoints") == current and current
        assert _rows(derived, "accepted_stages") == accepted
    finally:
        fixture.close()


@pytest.mark.parametrize("edited", (False, True))
def test_a_content_selection_disposes_only_retained_files_it_provably_produced(
    tmp_path: Path, edited: bool
) -> None:
    """Failure mode: excising an earlier content leaves the core's own unedited retained
    rendering of it on disk; or deletes a retained file the user edited, or one with no
    provenance record, on the strength of the selection; or does not report the edited
    file it kept."""
    from biblios.curation import CurationRequest, ReplaceContent

    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [])
        entry_id = fixture.bound.curate(
            fixture.request("a.txt", MARKER)
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        earlier = fixture.bound.resolve(entry_id).cas_id
        mirror = fixture.bound._shelf.mirror_path(entry_id)
        fixture.bound.curate(
            CurationRequest(
                identifier(), fixture.bound.view.revision, ReplaceContent(entry_id, b"current text")
            )
        )
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        retained = sorted(mirror.parent.glob(f"{mirror.name}.retained-*"))
        assert len(retained) == 1 and MARKER in retained[0].read_bytes()
        if edited:  # the user edits the retained copy itself
            retained[0].write_bytes(retained[0].read_bytes() + b"\nthe user's own note\n")
        stray = mirror.parent / f"{mirror.name}.retained-{'0' * 64}"
        stray.write_bytes(b"no provenance record")
        current = mirror.read_bytes()
        request = ExcisionRequest(
            identifier(),
            fixture.bound.view.revision,
            ExcisionSelection(content_ids=(earlier,)),
            (),
            fixture.binding.root / "successor",
        )
        result = fixture.bound.excise(fixture.bound.preview_excision(request).request)
        assert mirror.read_bytes() == current and stray.read_bytes() == b"no provenance record"
        # Only the selected content's own retention records are read: a file with no
        # record is neither disposed of nor reported as related to the selection.
        if edited:
            assert retained[0].exists() and result.kept_retained == (retained[0].name,)
        else:
            assert not retained[0].exists() and result.kept_retained == ()
    finally:
        fixture.close()


def test_bytes_written_after_the_retained_files_check_are_never_disposed_of(
    tmp_path: Path, monkeypatch
) -> None:
    """Failure mode: a retained file verified as the core's own is removed after the
    user wrote new bytes at its name, because the check and the removal were two steps
    on the same path."""
    import biblios.mirrors
    from biblios.curation import CurationRequest, ReplaceContent

    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [])
        entry_id = fixture.bound.curate(
            fixture.request("a.txt", MARKER)
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        earlier = fixture.bound.resolve(entry_id).cas_id
        fixture.bound.curate(
            CurationRequest(
                identifier(), fixture.bound.view.revision, ReplaceContent(entry_id, b"current text")
            )
        )
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        mirror = fixture.bound._shelf.mirror_path(entry_id)
        (retained,) = sorted(mirror.parent.glob(f"{mirror.name}.retained-*"))
        real = biblios.mirrors._rename
        users = b"the user's new words, written after the check began"
        fired = []

        def claiming(source, target, flag):
            real(source, target, flag)
            if source == retained and target.name.endswith(".excising"):
                retained.write_bytes(users)  # the user writes at the name meanwhile
                fired.append(1)

        monkeypatch.setattr(biblios.mirrors, "_rename", claiming)
        request = ExcisionRequest(
            identifier(),
            fixture.bound.view.revision,
            ExcisionSelection(content_ids=(earlier,)),
            (),
            fixture.binding.root / "successor",
        )
        fixture.bound.excise(fixture.bound.preview_excision(request).request)
        assert fired == [1] and retained.read_bytes() == users
        assert not (mirror.parent / (retained.name + ".excising")).exists()
    finally:
        fixture.close()


def test_a_replayed_request_after_excision_is_refused_and_recreates_nothing(
    tmp_path: Path,
) -> None:
    """Failure mode: after an entry is excised, re-sending the curation request that
    created it, or the indexing request that published it, is taken as new work (the
    replay ledgers were erased), so the excised entry comes back."""
    from dataclasses import replace

    from castle.namespace_environment import LocalSession

    from biblios.binding import open_bound_shelf

    fixture = consumer_fixture(tmp_path)
    successor = None
    try:
        pipeline = lane_pipeline(fixture, [])
        created = fixture.request("selected.txt", MARKER)
        entry_id = fixture.bound.curate(created).mutation.affected_entry_ids[0]
        indexing = identifier()
        fixture.bound.derive(pipeline, entry_id, operation_id=indexing)
        result = _excise(fixture, entry_id)
        declared = replace(result.successor_binding, ownership="owned")
        session = LocalSession(
            declared.namespace_root, declared.castle_root, declared.root / "namespace.lock"
        )
        successor = open_bound_shelf(declared, session)
        files = successor.files()
        outcomes = []
        for attempt in (
            lambda: successor.curate(created),
            lambda: successor.derive(pipeline, entry_id, operation_id=indexing),
        ):
            try:
                outcomes.append(attempt())
            except ConsumerError as error:
                outcomes.append(error.code)
        assert successor.files() == files
        assert entry_id not in {item.entry_id for item in successor.files()}
        curation, indexed = outcomes
        # Castle's ledger refuses the creating request; the indexing request replays its
        # recorded result exactly (replayed, no new effect) and recreates nothing.
        assert curation == "namespace-operation-retired"
        from biblios import pending_stages

        assert indexed.replayed and pending_stages(successor).stages == ()
        # The replay is the original call's record (history): it still names the excised
        # entry as it was then, which the current shelf contradicts.
        assert indexed.outcome(entry_id).outcome == "searchable"
        found = successor.search("selected", embedder=HashEmbedder(), reranker=OverlapReranker())
        assert entry_id not in {hit.entry_id for hit in found.hits}
    finally:
        if successor is not None:
            successor.close()
        fixture.close()


@pytest.mark.parametrize("edited", (False, True))
def test_excising_a_replaced_content_not_yet_reconverted_leaves_none_of_it_at_rest(
    tmp_path: Path, edited: bool
) -> None:
    """Failure mode: a content excised after its entry's source was replaced but before
    the new source was converted leaves its text in the live mirror and in Palace's rows
    while the residue reads clean; or a mirror the user edited is disposed of."""
    from biblios.curation import CurationRequest, ReplaceContent

    fixture = consumer_fixture(tmp_path)
    try:
        pipeline = lane_pipeline(fixture, [])
        entry_id = fixture.bound.curate(
            fixture.request("a.txt", MARKER)
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        earlier = fixture.bound.resolve(entry_id).cas_id
        mirror = fixture.binding.mirror_root / (entry_id + ".md")
        if edited:
            mirror.write_bytes(mirror.read_bytes() + b"\nmy own note\n")
        user_bytes = mirror.read_bytes()
        fixture.bound.curate(
            CurationRequest(
                identifier(), fixture.bound.view.revision, ReplaceContent(entry_id, b"current text")
            )
        )
        request = ExcisionRequest(
            identifier(),
            fixture.bound.view.revision,
            ExcisionSelection(content_ids=(earlier,)),
            (),
            fixture.binding.root / "successor",
        )
        result = fixture.bound.excise(fixture.bound.preview_excision(request).request)
        palace = fixture.binding.palace_root
        residue = dict(result.residue)
        assert residue["mirror.source-renderings"] == 0
        assert residue["derived.entries.source-accepted"] == 0
        assert residue.get("palace.files.source", 0) == 0
        assert not any(MARKER in file.read_bytes() for file in _store_files(palace))
        if edited:
            # The user's edit is kept in place and reported, never disposed of.
            assert mirror.read_bytes() == user_bytes
            assert entry_id + ".md" in result.kept_retained
        else:
            assert not mirror.exists() and result.kept_retained == ()
    finally:
        fixture.close()
