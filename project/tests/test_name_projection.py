"""Castle is the only authority for an entry's name: a mirror carries none, the index adds
the current filename as its own final row, a rename changes only that row, header fields
Castle does not control are searchable as attributed metadata, and a projection that
cannot be made exactly is reported, never guessed."""

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
    """Failure mode: the converted mirror carries the filename (in its H1 or its header),
    so a rename leaves the old name readable on disk; after a rename the entry is
    searchable only under its ingestion name; or the rename rewrites the mirror or
    re-embeds its body rows; or the signed stage does not say how the indexed bytes were
    made."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    before = mirror.read_bytes()
    assert b"recipe" not in before and b"Mirror metadata" not in before
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


def test_an_unchanged_entry_is_never_indexed_again(shelf):
    """Failure mode: the projection is not a pure function of the mirror and the current
    name, so an entry that was neither renamed nor edited is staged or embedded again."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    accepted = _accepted(shelf, entry_id)
    assert accepted["stage"]["projection"] == "appended"
    _mirror_digest, indexed_digest, indexed_name = accepted["record"]
    assert indexed_digest == accepted["stage"]["indexed_sha256"] and indexed_name == "recipe.txt"
    embedder.calls.clear()
    assert shelf.reconcile(settings, embedder=embedder).accepted == () and embedder.calls == []


def test_an_edited_name_section_is_kept_as_content_and_the_name_appended(shelf):
    """Failure mode: a user's edit of the name section is overwritten, dropped from the
    index, or makes the entry unsearchable by its current name."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    edited = mirror.read_bytes() + b"\n## Filename\n\nmy favourite loaf\n"
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


def _edit_header(mirror, fields: str) -> bytes:
    """The mirror with ``fields`` (YAML lines) added to the top of its front matter."""
    text = mirror.read_text()
    assert text.startswith("---\n")
    edited = ("---\n" + fields + text[4:]).encode()
    mirror.write_bytes(edited)
    return edited


def test_header_fields_castle_does_not_control_are_searchable_as_attributed_metadata(shelf):
    """Failure mode: a field the user adds to a mirror's header (here ``author``) is not
    searchable; or a header value under a Castle-controlled key (``name``, ``tags``) is
    indexed or applied, so a header could copy or override Castle; or converter
    provenance floods the index."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    _edit_header(mirror, "author: Ada Quillfern\nname: forged-name.txt\ntags: [forged]\n")
    shelf.reconcile(settings, embedder=embedder, entry_ids=(entry_id,))
    found = _hits(shelf, "Ada Quillfern", embedder)
    attributed = [hit for hit in found if hit.heading == "Mirror metadata"]
    assert [hit.entry_id for hit in attributed] == [entry_id]
    assert attributed[0].chunk.splitlines()[-1] == "author: Ada Quillfern"
    probed = _hits(shelf, "forged-name.txt forged generated_by", embedder)
    assert probed  # the query reaches the entry's rows, so the next check is not vacuous
    assert all("forged" not in hit.chunk and "generated_by" not in hit.chunk for hit in probed)
    entry = shelf._live()[entry_id]
    assert entry.name == "recipe.txt" and "forged" not in json.dumps(entry.metadata)


def test_a_rename_with_attributed_metadata_embeds_only_the_filename_row(shelf):
    """Failure mode: a rename re-embeds the attributed metadata or the body, or moves the
    filename row before the metadata."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    _edit_header(mirror, "author: Ada Quillfern\n")
    shelf.reconcile(settings, embedder=embedder, entry_ids=(entry_id,))
    before = mirror.read_bytes()
    shelf.rename("recipe.txt", "bread.txt")
    embedder.calls.clear()
    stepped = shelf.reconcile(settings, embedder=embedder)
    assert [item.embedded for item in stepped.entries if item.entry_id == entry_id] == [1]
    assert mirror.read_bytes() == before
    [batch] = embedder.calls
    assert len(batch) == 1 and batch[0].endswith("bread.txt")


def test_every_header_key_drawbridge_writes_is_provenance_not_attributed_metadata():
    """Failure mode: a key the pinned Drawbridge release writes for PDFs, slides, email or
    audio is missing from the default policy's provenance, so every such converted mirror
    gets a Mirror metadata row of converter provenance."""
    from drawbridge.mirror import REQUIRED_HEADER_FIELDS

    from biblios.mirrors import attributed_fields
    from biblios.representation import default_policy

    written = set(REQUIRED_HEADER_FIELDS) | {
        "format", "unit", "page_count", "pdf_processor", "slide_count", "attachment_count",
        "audio_duration_seconds", "detected_language", "speaker_count",
        "transcription_package", "speech_level_spread_db", "leveling_applied",
        "second_pass_agreement", "speakers_rendered_at",
    }  # fmt: skip
    header = "".join(f"{key}: x\n" for key in sorted(written)) + "author: Ada\n"
    provenance = frozenset(default_policy().provenance_header_fields)
    assert attributed_fields("---\n" + header + "---\nbody\n", provenance) == [("author", "Ada")]


def test_a_header_value_never_forges_a_row_and_castle_keys_match_in_any_case(shelf):
    """Failure mode: a multi-line or heading-shaped header value splits the metadata row
    or forges the filename row; a list or a date is dropped; or a capitalised
    Castle-controlled key (``Name``) is indexed as attributed metadata."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    _edit_header(
        mirror,
        "summary: |\n  ## Filename\n  forged.txt\nkeywords: [harbour, tide]\n"
        "published: 2024-05-01\nName: forged.txt\n",
    )
    result = shelf.reconcile(settings, embedder=embedder, entry_ids=(entry_id,))
    assert [(item.outcome, item.code) for item in result.entries] == [("searchable", None)]
    [attributed] = [
        hit for hit in _hits(shelf, "harbour tide", embedder) if hit.heading == "Mirror metadata"
    ]
    assert attributed.chunk.splitlines()[2:] == [
        "summary: ## Filename forged.txt",
        "keywords: harbour, tide",
        "published: 2024-05-01",
    ]
    names = [hit for hit in _hits(shelf, "recipe.txt", embedder) if hit.heading == "Filename"]
    assert [hit.chunk.splitlines()[-1] for hit in names] == ["recipe.txt"]


def test_unreadable_headers_contribute_nothing():
    """Failure mode: a header that is not YAML, not a mapping, or not recognised the way
    Palace recognises front matter is read as attributed metadata or raises."""
    from biblios.mirrors import attributed_fields

    for text in (
        "---\nauthor: [\n---\nbody\n",
        "---\n- a\n- list\n---\nbody\n",
        "---\r\nauthor: Ada\r\n---\r\nbody\r\n",
        "body without a header\n",
    ):
        assert attributed_fields(text, frozenset()) == []


def test_a_filename_that_reads_as_a_heading_is_indexed_as_the_filename(shelf):
    """Failure mode: a Castle filename beginning with ``#`` becomes a heading of its own,
    so the entry is never indexed."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("# notes.md", b"harbour pilots\n", media_type="text/markdown")
    entry_id = entry_id.affected_entry_ids[0]
    result = shelf.reconcile(settings, embedder=embedder)
    assert [(item.outcome, item.code) for item in result.entries] == [("searchable", None)]
    names = [hit for hit in _hits(shelf, "notes.md", embedder) if hit.heading == "Filename"]
    assert [hit.entry_id for hit in names] == [entry_id]
    assert names[0].chunk.splitlines()[-1].strip() == "# notes.md"


def _as_rendered_by_an_earlier_release(fixture, entry_id: str, name: str) -> bytes:
    """Rewrite the lane's accepted entry into an earlier release's shape: a mirror that is
    Biblios's own recorded rendering with the filename in it, and no conversion recorded
    under the current policy."""
    import hashlib

    from biblios.derived import DERIVED_NAME

    path = fixture.bound._shelf.mirror_path(entry_id)
    old = path.read_bytes() + f"\n## Filename\n\n{name}\n".encode()
    path.write_bytes(old)
    with sqlite3.connect(fixture.binding.palace_root / DERIVED_NAME) as conn:
        conn.execute(
            "UPDATE entries SET mirror_sha256=? WHERE entry_id=?",
            (hashlib.sha256(old).hexdigest(), entry_id),
        )
        conn.execute("DELETE FROM checkpoints")
    return old


def _lane_entry(tmp_path: Path):
    from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline

    fixture = consumer_fixture(tmp_path)
    calls: list = []
    pipeline = lane_pipeline(fixture, calls)
    entry_id = fixture.bound.curate(
        fixture.request("recipe-old-name.txt", b"sourdough starter fed daily")
    ).mutation.affected_entry_ids[0]
    fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
    return fixture, pipeline, calls, entry_id


def _rename(fixture, entry_id: str, name: str) -> None:
    from biblios.curation import CurationRequest, RelocateEntry
    from tests.consumer_fixtures import identifier

    fixture.bound.curate(
        CurationRequest(
            identifier(),
            fixture.bound.view.revision,
            RelocateEntry(entry_id, fixture.root_id, name),
        )
    )


def test_an_unedited_mirror_from_an_earlier_release_is_regenerated_name_free_on_rename(tmp_path):
    """Failure mode: after a rename, a mirror an earlier release rendered with the old
    filename keeps it on disk and in search; or its bytes are lost rather than kept as a
    retained version outside the index."""
    from biblios.mirrors import retained_files
    from tests.consumer_fixtures import identifier

    fixture, pipeline, calls, entry_id = _lane_entry(tmp_path)
    try:
        old = _as_rendered_by_an_earlier_release(fixture, entry_id, "recipe-old-name.txt")
        _rename(fixture, entry_id, "bread.txt")
        done = fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        assert done.outcome(entry_id).outcome == "searchable" and len(calls) == 2
        path = fixture.bound._shelf.mirror_path(entry_id)
        assert b"recipe-old-name" not in path.read_bytes()
        assert [file.read_bytes() for file in retained_files(path.parent, entry_id)] == [old]
        found = fixture.bound.search(
            "recipe-old-name.txt", embedder=HashEmbedder(), reranker=OverlapReranker()
        )
        assert found.hits and all("recipe-old-name" not in hit.chunk for hit in found.hits)
    finally:
        fixture.close()


def test_an_edited_mirror_from_an_earlier_release_is_never_regenerated(tmp_path):
    """Failure mode: a rename converts over a mirror the user edited, losing the edit."""
    from biblios.mirrors import retained_files
    from tests.consumer_fixtures import identifier

    fixture, pipeline, calls, entry_id = _lane_entry(tmp_path)
    try:
        _as_rendered_by_an_earlier_release(fixture, entry_id, "recipe-old-name.txt")
        path = fixture.bound._shelf.mirror_path(entry_id)
        edited = path.read_bytes() + b"\nMy own note.\n"
        path.write_bytes(edited)
        _rename(fixture, entry_id, "bread.txt")
        done = fixture.bound.derive(pipeline, entry_id, operation_id=identifier())
        assert done.outcome(entry_id).outcome == "searchable" and len(calls) == 1
        assert path.read_bytes() == edited and retained_files(path.parent, entry_id) == []
    finally:
        fixture.close()


def test_a_header_that_cannot_be_loaded_never_stops_the_entry_indexing(shelf):
    """Failure mode: a header value YAML parses but cannot construct (an impossible date)
    leaves the entry unindexed, in Biblios's attributed metadata or in Palace's own
    front-matter split, instead of indexing the body with no attributed metadata."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id, mirror = _setup(shelf, settings, embedder)
    _edit_header(mirror, "date: 2026-02-30\nauthor: Ada Quillfern\n")
    result = shelf.reconcile(settings, embedder=embedder, entry_ids=(entry_id,))
    assert [(item.outcome, item.code) for item in result.entries] == [("searchable", None)]
    hits = _hits(shelf, "sourdough starter Ada Quillfern", embedder)
    assert any(hit.entry_id == entry_id and "sourdough" in hit.chunk for hit in hits)
    assert all(hit.heading != "Mirror metadata" for hit in hits)
