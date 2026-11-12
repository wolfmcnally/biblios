"""Read-side observations (ONE-09): a reader that withholds an entry for a served-row or
mirror mismatch reports it; a write-grade call verifies the report under the writer lock
and isolates only damage it proves, dropping stale and disproved reports."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from biblios import BringCurrentRequest, ReadObservation, bring_current, entry_status
from biblios.consumer import BoundShelf
from biblios.derived import DERIVED_NAME
from biblios.pipeline import ConsumerPipeline
from biblios.representation import default_policy, drawbridge_converter, envelope_renderer
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import make_shelf

_TEXT = b"# Lens\n\nlighthouse lens maintenance\n\n## Lamp\n\nlighthouse lamp care\n"


@pytest.fixture
def shelf(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("notes")
        entry_id = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
        embedder = HashEmbedder()
        assert shelf.reconcile(settings, embedder=embedder).outcome(entry_id).outcome == (
            "searchable"
        )
        yield shelf, settings, embedder, entry_id


def _bring(shelf, settings, embedder, entry_ids=None, observations=()):
    return bring_current(
        BoundShelf(shelf.local_binding(), shelf),
        BringCurrentRequest(str(uuid.uuid4()), entry_ids, observations=tuple(observations)),
        pipeline=ConsumerPipeline(
            converter=drawbridge_converter(settings),
            renderer=envelope_renderer,
            input_view=None,
            policy=default_policy(),
            embedder=embedder,
        ),
    )


def _damage_accepted_row(shelf, entry_id) -> None:
    """Change the accepted per-hit row a reader checks a served body against."""
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME, isolation_level=None)
    conn.execute(
        "UPDATE accepted_chunks SET body_sha256=? WHERE entry_id=? AND position=0",
        ("0" * 64, entry_id),
    )
    conn.close()


def _served_seq(shelf, entry_id) -> int:
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME)
    [seq] = conn.execute("SELECT served_seq FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
    conn.close()
    return int(seq)


def _state(shelf, entry_id) -> str:
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME)
    [state] = conn.execute("SELECT state FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
    conn.close()
    return str(state)


def test_a_withheld_mismatch_is_reported_and_marked_only_by_a_writable_binding(shelf):
    """Failure mode: a reader that withholds a mismatched entry reports nothing, so no
    write-grade call ever verifies it; or a read-only search persists a mark."""
    shelf, settings, embedder, entry_id = shelf
    _damage_accepted_row(shelf, entry_id)
    seq = _served_seq(shelf, entry_id)
    plain = shelf.search("lighthouse", embedder=embedder, limit=10)
    assert plain.observations == (ReadObservation(entry_id, "rows-mismatch", seq),)
    bound = BoundShelf(shelf.local_binding(), shelf)
    assert not entry_status(bound, entry_id).dirty  # the plain search persisted nothing
    found = bound.search("lighthouse", embedder=embedder, reranker=OverlapReranker(), limit=10)
    assert all(hit.manifest.position != 0 for hit in found.hits if hit.entry_id == entry_id)
    assert found.observations == plain.observations
    assert entry_status(bound, entry_id).dirty


def test_proven_damage_isolates_the_entry_until_a_request_names_it(shelf):
    """Failure mode: a verified mismatch is served again, repaired by an unnamed call, or
    re-indexed by spending on the provider when the committed rows still reproduce the
    accepted stage."""
    shelf, settings, embedder, entry_id = shelf
    _damage_accepted_row(shelf, entry_id)
    [observed] = shelf.search("lighthouse", embedder=embedder, limit=10).observations
    calls = len(embedder.calls)
    result = _bring(shelf, settings, embedder, observations=[observed])
    assert result.outcome(entry_id).outcome == "isolated:served-rows-damaged"
    assert _state(shelf, entry_id) == "served-rows-damaged"
    again = _bring(shelf, settings, embedder)  # unnamed: nothing repairs it
    assert again.outcome(entry_id) is None or again.outcome(entry_id).outcome == (
        "isolated:served-rows-damaged"
    )
    assert _state(shelf, entry_id) == "served-rows-damaged"
    named = _bring(shelf, settings, embedder, (entry_id,))
    assert named.outcome(entry_id).outcome == "searchable"
    assert _state(shelf, entry_id) == "accepted"
    found = shelf.search("lighthouse", embedder=embedder, limit=10)
    assert entry_id in {hit.entry_id for hit in found.hits} and found.observations == ()
    # Only queries reached the provider: the committed rows were reused.
    assert all(call == ("lighthouse",) for call in embedder.calls[calls:])


@pytest.mark.parametrize("report", ("disproved", "stale"))
def test_a_report_the_write_side_disproves_or_that_is_stale_changes_nothing(shelf, report):
    """Failure mode: a reader's report is taken as evidence, so a false or replayed one
    (or one about an earlier serving sequence) declares healthy rows damaged and creates
    a repair or spend need."""
    shelf, settings, embedder, entry_id = shelf
    seq = _served_seq(shelf, entry_id)
    observation = ReadObservation(
        entry_id, "rows-mismatch", seq if report == "disproved" else seq + 2
    )
    calls = len(embedder.calls)
    for _ in range(3):  # replaying the same report never makes it evidence
        result = _bring(shelf, settings, embedder, observations=[observation])
        assert result.outcome(entry_id) is None or result.outcome(entry_id).outcome == (
            "searchable"
        )
        assert _state(shelf, entry_id) == "accepted"
    assert len(embedder.calls) == calls
    assert entry_id in {
        hit.entry_id for hit in shelf.search("lighthouse", embedder=embedder, limit=10).hits
    }


def test_a_malformed_observation_is_refused_before_any_effect(shelf):
    """Failure mode: an observation of an unknown category or with a non-integer
    sequence is acted on, or aborts with an untyped error."""
    from biblios.control import ConsumerError

    shelf, settings, embedder, entry_id = shelf
    for bad in (
        ReadObservation(entry_id, "integrity-failure", 0),  # type: ignore[arg-type]
        ReadObservation(entry_id, "rows-mismatch", "2"),  # type: ignore[arg-type]
    ):
        with pytest.raises(ConsumerError, match="request-invalid"):
            _bring(shelf, settings, embedder, observations=[bad])
    assert _state(shelf, entry_id) == "accepted"


def test_an_old_report_never_acts_on_a_later_serving_sequence(shelf):
    """Failure mode: a report from an earlier serving sequence (the entry was committed
    again since) is applied to whatever the entry is now, so an old hint decides the
    entry's current state instead of a reader of that state."""
    shelf, settings, embedder, entry_id = shelf
    _damage_accepted_row(shelf, entry_id)
    [old] = shelf.search("lighthouse", embedder=embedder, limit=10).observations
    assert _bring(shelf, settings, embedder, observations=[old]).outcome(entry_id).outcome == (
        "isolated:served-rows-damaged"
    )
    assert _bring(shelf, settings, embedder, (entry_id,)).outcome(entry_id).outcome == (
        "searchable"
    )
    assert _served_seq(shelf, entry_id) > old.served_seq
    _damage_accepted_row(shelf, entry_id)  # damaged again, but no reader has seen it yet
    _bring(shelf, settings, embedder, observations=[old])
    assert _state(shelf, entry_id) == "accepted"


def test_a_read_only_binding_returns_observations_and_persists_nothing(tmp_path):
    """Failure mode: a search through a read-only binding writes a mark (or anything
    else) into the shelf's stores, or drops the report its caller needs."""
    from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline

    fixture = consumer_fixture(tmp_path)
    try:
        embedder = HashEmbedder()
        pipeline = lane_pipeline(fixture, [], embedder=embedder)
        entry_id = fixture.bound.curate(
            fixture.request("one.txt", b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        root = fixture.binding.palace_root
        conn = sqlite3.connect(root / DERIVED_NAME, isolation_level=None)
        conn.execute(
            "UPDATE accepted_chunks SET body_sha256=? WHERE entry_id=? AND position=0",
            ("0" * 64, entry_id),
        )
        conn.close()
        fixture.bound.checkpoint()  # a read-only binding opens a readable checkpoint
        reader = fixture.reopen(read_only=True)
        found = reader.search(
            "Lighthouse lens", embedder=embedder, reranker=OverlapReranker(), limit=10
        )
        assert [item.entry_id for item in found.observations] == [entry_id]
        conn = sqlite3.connect(root / "biblios-volatile.sqlite")
        marks = conn.execute("SELECT COUNT(*) FROM dirty WHERE entry_id=?", (entry_id,))
        assert marks.fetchone()[0] == 0
        conn.close()
    finally:
        fixture.close()


def test_a_stack_keeps_each_report_with_its_own_shelf(tmp_path):
    """Failure mode: a stack search's observations lose their shelf, so one member's
    report is marked (or later acted on) in another member's stores by entry id alone."""
    from biblios.consumer import open_bound_stack
    from tests.consumer_fixtures import indexed_fixture

    fixtures = [indexed_fixture(tmp_path / name) for name in ("one", "two")]
    try:
        damaged = fixtures[0]
        conn = sqlite3.connect(damaged.binding.palace_root / DERIVED_NAME, isolation_level=None)
        [entry_id] = conn.execute("SELECT entry_id FROM entries").fetchone()
        conn.execute(
            "UPDATE accepted_chunks SET body_sha256=? WHERE entry_id=? AND position=0",
            ("0" * 64, entry_id),
        )
        conn.close()
        stack = open_bound_stack(tuple(item.bound for item in fixtures))
        found = stack.search(
            "lighthouse lens", embedder=HashEmbedder(), reranker=OverlapReranker(), limit=10
        )
        assert {
            key: [item.entry_id for item in value] for key, value in found.observations.items()
        } == {damaged.binding.shelf_id: [entry_id]}
        marks = []
        for fixture in fixtures:
            conn = sqlite3.connect(fixture.binding.palace_root / "biblios-volatile.sqlite")
            marks.append(
                conn.execute(
                    "SELECT entry_id FROM dirty WHERE source='read-observation'"
                ).fetchall()
            )
            conn.close()
        assert marks == [[(entry_id,)], []]
    finally:
        for fixture in fixtures:
            fixture.close()


def test_a_check_that_cannot_run_is_reported_with_its_cause_not_as_damage_or_disproof(
    shelf, monkeypatch
):
    """Failure mode: an unreadable store during verification is silently treated as a
    disproved report (the caller cannot tell the check never ran) or as proof of damage
    (healthy rows isolated), or loses the dependency's own error and trace."""
    from palace.index import IndexError as PalaceIndexError

    from biblios import core

    shelf, settings, embedder, entry_id = shelf
    seq = _served_seq(shelf, entry_id)
    failure = PalaceIndexError("file vector snapshot unreadable")

    def unreadable(self, entry_id, stage):
        raise failure

    monkeypatch.setattr(core._Run, "_served_as", unreadable)
    result = _bring(
        shelf, settings, embedder, observations=[ReadObservation(entry_id, "rows-mismatch", seq)]
    )
    outcome = result.outcome(entry_id)
    assert (outcome.outcome, outcome.code, outcome.cause) == (
        "pending",
        "observation-unverified",
        failure,
    )
    assert _state(shelf, entry_id) == "accepted"
    assert entry_status(BoundShelf(shelf.local_binding(), shelf), entry_id).dirty


@pytest.fixture
def authorized(tmp_path: Path):
    """An entry accepted under a caller's stage authority, with its accepted per-hit row
    damaged and the writable binding's search report of it."""
    from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline
    from tests.test_checkpoint_seal import _Sealer

    fixture = consumer_fixture(tmp_path)
    try:
        embedder = HashEmbedder()
        pipeline = lane_pipeline(fixture, [], embedder=embedder)
        sealer = _Sealer()
        entry_id = fixture.bound.curate(
            fixture.request("one.txt", b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier(), authority=sealer)
        _damage_accepted_row(SimpleNamespace(palace_store=fixture.binding.palace_root), entry_id)
        found = fixture.bound.search(
            "Lighthouse lens", embedder=embedder, reranker=OverlapReranker(), limit=10
        )
        assert [item.entry_id for item in found.observations] == [entry_id]
        yield fixture, pipeline, embedder, sealer, entry_id, found.observations
    finally:
        fixture.close()


def _repair(fixture, pipeline, sealer, observations):
    from tests.consumer_fixtures import identifier

    return fixture.bound.bring_current(
        pipeline,
        BringCurrentRequest(identifier(), None, observations=tuple(observations)),
        authority=sealer,
    )


def test_proven_derived_damage_is_recommitted_from_palace_s_rows_under_current_authority(
    authorized,
):
    """Failure mode: an entry whose accepted per-hit rows were damaged, while Palace still
    serves exactly its accepted rows, stays withheld until someone names it; or its repair
    re-embeds (spends), reuses the damaged rows, or re-signs persisted rows without the
    caller authorizing a new stage now."""
    fixture, pipeline, embedder, sealer, entry_id, observations = authorized
    calls, authorized_before = len(embedder.calls), list(sealer.authorized)
    result = _repair(fixture, pipeline, sealer, observations)
    assert result.outcome(entry_id).outcome == "searchable"
    assert _state(SimpleNamespace(palace_store=fixture.binding.palace_root), entry_id) == (
        "accepted"
    )
    assert len(sealer.authorized) == len(authorized_before) + 1  # authorized in this call
    assert embedder.calls[calls:] == []  # Palace's rows were reused: no provider call
    found = fixture.bound.search(
        "Lighthouse lens", embedder=embedder, reranker=OverlapReranker(), limit=10
    )
    assert entry_id in {hit.entry_id for hit in found.hits} and found.observations == ()


@pytest.mark.parametrize(
    "case", ("palace-rows-changed", "mirror-edited", "renamed", "stage-refused")
)
def test_a_repair_without_exact_retained_rows_or_current_authority_stays_isolated(
    authorized, monkeypatch, case
):
    """Failure mode: an entry is re-committed although Palace no longer holds its exact
    accepted rows, its mirror changed or it was renamed since acceptance (new rows, so
    spend), or the caller refuses the new stage; or such a refusal un-isolates it."""
    from biblios import core

    fixture, pipeline, embedder, sealer, entry_id, observations = authorized
    if case == "palace-rows-changed":
        monkeypatch.setattr(core._Run, "_served_as", lambda self, entry_id, stage: False)
    elif case == "mirror-edited":
        mirror = fixture.bound._shelf.mirror_path(entry_id)
        mirror.write_bytes(mirror.read_bytes() + b"\nedited\n")
    elif case == "renamed":
        from biblios.curation import CurationRequest, RelocateEntry
        from tests.consumer_fixtures import identifier

        fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                RelocateEntry(entry_id, fixture.root_id, "renamed.txt"),
            )
        )
    else:
        sealer.refuse_stage = True
    calls, authorized_before = len(embedder.calls), list(sealer.authorized)
    result = _repair(fixture, pipeline, sealer, observations)
    assert result.outcome(entry_id).outcome != "searchable"
    assert _state(SimpleNamespace(palace_store=fixture.binding.palace_root), entry_id) == (
        "served-rows-damaged"
    )
    assert embedder.calls[calls:] == []
    if case != "stage-refused":
        assert sealer.authorized == authorized_before
    found = fixture.bound.search(
        "Lighthouse lens", embedder=embedder, reranker=OverlapReranker(), limit=10
    )
    assert entry_id not in {hit.entry_id for hit in found.hits}
