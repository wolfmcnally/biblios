"""A renamed entry is indexed under its current filename from an in-memory projection of
its mirror: the mirror's bytes never change, unchanged entries are untouched, and a
projection that cannot be made exactly is reported, never guessed."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from biblios.derived import DERIVED_NAME
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import make_shelf


@pytest.fixture
def shelf(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        yield shelf, settings


def _accepted(shelf, entry_id: str) -> dict:
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        (canonical,) = conn.execute(
            "SELECT canonical FROM accepted_stages WHERE entry_id=?", (entry_id,)
        ).fetchone()
        record = conn.execute(
            "SELECT mirror_sha256, indexed_sha256, indexed_name FROM entries WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
    return {"stage": json.loads(bytes(canonical))["entry"], "record": record}


def _hits(shelf, query: str, embedder) -> list:
    return list(shelf.search(query, embedder=embedder, reranker=OverlapReranker()).hits)


def _setup(shelf, settings, embedder):
    entry_id = shelf.add(
        "recipe.txt", b"sourdough starter fed daily\n", media_type="text/plain"
    ).affected_entry_ids[0]
    shelf.reconcile(settings, embedder=embedder)
    return entry_id, shelf.mirror_path(entry_id)


def test_a_renamed_entry_is_found_by_its_current_name_with_its_mirror_untouched(shelf):
    """Failure mode: after a rename the entry is searchable only under its ingestion
    name; or the rename rewrites the user's mirror or re-embeds its body rows; or the
    signed stage does not say how the indexed bytes were made. (Drawbridge's envelope
    carries the ingestion name in the converted body itself, which no re-render can
    change, so the current name is appended and the body keeps its own text.)"""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    before = mirror.read_bytes()
    assert b"recipe.txt" in before
    shelf.rename("recipe.txt", "bread.txt")
    embedder.calls.clear()
    stepped = shelf.reconcile(settings, embedder=embedder)
    assert [stage.entry_id for stage in stepped.accepted] == [entry_id]
    assert [item.embedded for item in stepped.entries if item.entry_id == entry_id] == [1]
    assert mirror.read_bytes() == before
    accepted = _accepted(shelf, entry_id)
    assert accepted["stage"]["projection"] == "appended"
    assert accepted["stage"]["path"].endswith("bread.txt")
    mirror_digest, indexed_digest, indexed_name = accepted["record"]
    assert indexed_name == "bread.txt" and indexed_digest == accepted["stage"]["indexed_sha256"]
    assert indexed_digest != mirror_digest
    hits = _hits(shelf, "bread.txt", embedder)
    assert hits and hits[0].entry_id == entry_id and "bread.txt" in hits[0].chunk
    assert shelf.verify().ok, shelf.verify().problems
    embedder.calls.clear()
    again = shelf.reconcile(settings, embedder=embedder)
    assert again.accepted == () and embedder.calls == []


def test_a_renderers_own_name_section_is_replaced_in_the_index_only(tmp_path: Path):
    """Failure mode: a renamed entry whose mirror ends with the renderer's own unedited
    name section keeps the old name in its name row's text, keyword match or vector;
    or the converter runs again; or the mirror on disk changes."""
    from biblios.curation import CurationRequest, RelocateEntry
    from biblios.pipeline import MirrorDocument
    from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline

    fixture = consumer_fixture(tmp_path)
    try:
        calls: list = []
        pipeline = lane_pipeline(fixture, calls)

        def renderer(result, name):
            return MirrorDocument(result.body + "\n\n## Filename\n\n" + name + "\n", "Filename")

        pipeline.renderer = renderer
        entry_id = fixture.bound.curate(
            fixture.request("recipe.txt", b"sourdough starter fed daily")
        ).mutation.affected_entry_ids[0]
        fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        mirror = fixture.bound._shelf.mirror_path(entry_id)
        before, converted = mirror.read_bytes(), len(calls)
        fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                RelocateEntry(entry_id, fixture.root_id, "bread.txt"),
            )
        )
        done = fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        assert (
            done.outcome(entry_id).outcome == "searchable" and done.outcome(entry_id).embedded == 1
        )
        assert mirror.read_bytes() == before and len(calls) == converted
        with sqlite3.connect(fixture.binding.palace_root / DERIVED_NAME) as conn:
            (canonical,) = conn.execute(
                "SELECT canonical FROM accepted_stages WHERE entry_id=?", (entry_id,)
            ).fetchone()
        assert json.loads(bytes(canonical))["entry"]["projection"] == "replaced"
        found = fixture.bound.search(
            "bread.txt", embedder=HashEmbedder(), reranker=OverlapReranker()
        )
        name_hits = [
            hit for hit in found.hits if hit.entry_id == entry_id and hit.heading == "Filename"
        ]
        assert (
            name_hits
            and "bread.txt" in name_hits[0].chunk
            and "recipe.txt" not in name_hits[0].chunk
        )
    finally:
        fixture.close()


def test_an_unchanged_entry_is_indexed_from_its_own_bytes_exactly(shelf):
    """Failure mode: the projection changes an entry that was neither renamed nor
    edited, so its stored vectors, chunk ids or index digest move."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    accepted = _accepted(shelf, entry_id)
    assert accepted["stage"]["projection"] == "exact"
    mirror_digest, indexed_digest, _ = accepted["record"]
    assert indexed_digest == mirror_digest == accepted["stage"]["indexed_sha256"]
    embedder.calls.clear()
    assert shelf.reconcile(settings, embedder=embedder).accepted == () and embedder.calls == []


def test_an_edited_name_section_is_kept_as_content_and_the_name_appended(shelf):
    """Failure mode: a user's edit of the name section is overwritten, dropped from the
    index, or makes the entry unsearchable by its current name."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    edited = mirror.read_bytes().replace(b"recipe.txt", b"my favourite loaf")
    mirror.write_bytes(edited)
    shelf.rename("recipe.txt", "bread.txt")
    shelf.reconcile(settings, embedder=embedder)
    assert mirror.read_bytes() == edited
    assert _accepted(shelf, entry_id)["stage"]["projection"] == "appended"
    assert any("favourite loaf" in hit.chunk for hit in _hits(shelf, "favourite loaf", embedder))
    assert any("bread.txt" in hit.chunk for hit in _hits(shelf, "bread.txt", embedder))


def test_a_name_section_that_cannot_be_projected_is_reported_not_guessed(shelf, monkeypatch):
    """Failure mode: a projected name section that does not chunk as the one final name
    row (here a chunker that absorbs it into the user's last section) is published as if
    it were the name row; or the entry loses its accepted state."""
    import dataclasses

    import palace.index

    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    accepted_before = _accepted(shelf, entry_id)
    header = mirror.read_text().split("\n---\n", 1)[0] + "\n---\n"
    mirror.write_text(header + "\n# Notes\n\nmore text\n")
    shelf.rename("recipe.txt", "bread.txt")
    real = palace.index.plan_one

    def absorbing(**kwargs):
        plan = real(**kwargs)
        *body, last = plan.rows
        return dataclasses.replace(plan, rows=(*body, dataclasses.replace(last, heading="Notes")))

    monkeypatch.setattr(palace.index, "plan_one", absorbing)
    out = shelf.reconcile(settings, embedder=embedder)
    item = next(item for item in out.entries if item.entry_id == entry_id)
    assert (item.outcome, item.code) == ("pending", "name-section-unprojectable")
    assert out.accepted == () and _accepted(shelf, entry_id) == accepted_before


def test_a_users_own_section_under_the_name_heading_is_content(shelf):
    """Failure mode: a document whose own text has a section titled like the core's name
    section can never be re-indexed after an edit or a rename."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    header = mirror.read_text().split("\n---\n", 1)[0] + "\n---\n"
    mirror.write_text(header + "\n## Filename\n\nmy own label\n\n# Notes\n\nmore text\n")
    shelf.rename("recipe.txt", "bread.txt")
    out = shelf.reconcile(settings, embedder=embedder)
    item = next(item for item in out.entries if item.entry_id == entry_id)
    assert item.outcome == "searchable" and len(out.accepted) == 1
    assert "my own label" in mirror.read_text()  # the mirror is never rewritten


def test_a_persisted_projection_that_no_longer_matches_its_signed_digest_is_refused(shelf):
    """Failure mode: a persisted stage's recorded projection is altered after B2 and the
    altered bytes are served on resume as if the caller had authorized them."""
    from biblios.derived import VOLATILE_NAME
    from tests.test_core import _bring, _Holding

    shelf, settings = shelf
    embedder = HashEmbedder()
    shelf.mkdir("notes")
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    holding = _Holding()
    holding.release = True
    first = _bring(shelf, settings, embedder, entry_id, holding)
    holding.head.update(stage.stage_sha256 for stage in first.accepted)
    _bring(shelf, settings, embedder, entry_id, holding)  # acknowledged: cleanup only
    shelf.rename("notes/a.md", "b.md")
    holding.release = False
    assert _bring(shelf, settings, embedder, entry_id, holding).outcome(entry_id).code == "held"
    volatile = shelf.palace_store / VOLATILE_NAME
    with sqlite3.connect(volatile) as conn:
        (document,) = conn.execute(
            "SELECT document FROM stage_resume WHERE entry_id=?", (entry_id,)
        ).fetchone()
        altered = json.loads(document)
        altered["indexed"][1] = altered["indexed"][1].replace("b.md", "forged.md")
        conn.execute(
            "UPDATE stage_resume SET document=? WHERE entry_id=?", (json.dumps(altered), entry_id)
        )
    holding.release = True
    out = _bring(shelf, settings, embedder, entry_id, holding)
    assert (out.outcome(entry_id).outcome, out.outcome(entry_id).code) == (
        "recovery-required",
        "stage-tampered",
    )
    assert out.accepted == ()
