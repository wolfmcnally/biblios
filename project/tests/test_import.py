"""The bounded verified preservation import and the explicit first-ingestion route
(ONE-10, ONE-12): a shelf written by an earlier release keeps answering for every entry
whose mirror and committed rows are exactly what its record says, with zero embedding,
and nothing else becomes searchable, converted or rewritten without an explicit call."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from biblios.control import ConsumerError
from biblios.derived import ACCEPTED_MARKER, DERIVED_NAME, VOLATILE_NAME, DerivedStore
from biblios.entries import EntryMetadata
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import make_shelf


def _legacy(shelf, entry_ids, *, never_converted=()) -> None:
    """Rewrite a shelf this release built into an earlier release's shape: the conversion
    keys in entry metadata and no derived or volatile store, no accepted marker."""
    store = DerivedStore(shelf.palace_store, read_only=True)
    records = store.records(list(entry_ids))
    store.close()
    for entry_id in entry_ids:
        record = records[entry_id]
        metadata = EntryMetadata.from_mapping(shelf._live()[entry_id].metadata)
        # An earlier release indexed its mirror's bytes exactly, the name section inside
        # the mirror: rewrite each mirror to the bytes this release's index holds.
        mirror = shelf.mirror_path(entry_id)
        indexed = _indexed_bytes(mirror.read_bytes(), shelf._live()[entry_id].name)
        mirror.write_bytes(indexed)
        keys = {
            "converter": record.converter,
            "converter_version": record.converter_version,
            "mirror_sha256": hashlib.sha256(indexed).hexdigest(),
            "source_cas_id": record.source_cas_id or "",
            "conversion": record.conversion,
        }
        if entry_id in never_converted:
            keys = {**keys, "conversion": "", "mirror_sha256": "", "source_cas_id": ""}
            shelf.mirror_path(entry_id).unlink()
        shelf._set_metadata(entry_id, dataclasses.replace(metadata, legacy=keys))
    for name in (DERIVED_NAME, VOLATILE_NAME):
        for suffix in ("", "-wal", "-shm"):
            (shelf.palace_store / (name + suffix)).unlink(missing_ok=True)
    (shelf.palace_store / ACCEPTED_MARKER).unlink(missing_ok=True)


def _indexed_bytes(data: bytes, name: str) -> bytes:
    from biblios.core import FILENAME_HEADING

    gap = "" if data.endswith(b"\n\n") else "\n" if data.endswith(b"\n") else "\n\n"
    return data + (gap + "## " + FILENAME_HEADING + "\n\n" + name + "\n").encode()


def _outcomes(result) -> dict[str, tuple[str, str | None]]:
    return {item.entry_id: (item.outcome, item.code) for item in result.entries}


@pytest.fixture
def legacy(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("notes")
        embedder = HashEmbedder()
        ids = {
            name: shelf.add(f"notes/{name}.md", text).affected_entry_ids[0]
            for name, text in (
                ("matched", b"# M\n\nlighthouse keepers\n"),
                ("edited", b"# E\n\nharbour pilots\n"),
                ("missing", b"# G\n\ntide tables\n"),
                ("fresh", b"# F\n\nkelp forests\n"),
            )
        }
        shelf.reconcile(settings, embedder=embedder)
        _legacy(shelf, list(ids.values()), never_converted={ids["fresh"]})
        edited = shelf.mirror_path(ids["edited"])
        edited.write_bytes(edited.read_bytes() + b"\nseaward beacons\n")
        shelf.mirror_path(ids["missing"]).unlink()
        yield shelf, settings, ids


def test_only_exactly_matched_entries_are_imported_with_no_embedding_or_writes(legacy):
    shelf, settings, ids = legacy
    embedder = HashEmbedder()
    revision = shelf.revision
    mirrors = {
        entry_id: (path.read_bytes(), path.stat().st_ino)
        for entry_id in ids.values()
        if (path := shelf.mirror_path(entry_id)).exists()
    }
    result = shelf.reconcile(settings, embedder=embedder)
    assert _outcomes(result) == {
        ids["matched"]: ("searchable", None),
        ids["edited"]: ("pending", "legacy-unmatched"),
        ids["missing"]: ("pending", "legacy-unmatched"),
        ids["fresh"]: ("pending", "legacy-unmatched"),
    }
    assert embedder.calls == [] and shelf.revision == revision
    assert {
        entry_id: (path.read_bytes(), path.stat().st_ino)
        for entry_id in ids.values()
        if (path := shelf.mirror_path(entry_id)).exists()
    } == mirrors
    hits = shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker()).hits
    assert {hit.entry_id for hit in hits} == {ids["matched"]}
    pilots = shelf.search("pilots", embedder=embedder, reranker=OverlapReranker())
    assert ids["edited"] not in {hit.entry_id for hit in pilots.hits}
    # The import runs once: a second call has nothing to do and spends nothing.
    queries = len(embedder.calls)  # the searches above embedded their queries only
    again = shelf.reconcile(settings, embedder=embedder)
    assert again.entries == () and len(embedder.calls) == queries


def test_an_unmatched_entry_is_reindexed_only_when_named_and_never_converted(legacy):
    shelf, settings, ids = legacy
    embedder = HashEmbedder()
    shelf.reconcile(settings, embedder=embedder)
    named = shelf.reconcile(settings, embedder=embedder, entry_ids=[ids["edited"]])
    assert _outcomes(named) == {ids["edited"]: ("searchable", None)}
    hits = shelf.search("beacons", embedder=embedder, reranker=OverlapReranker()).hits
    assert hits[0].entry_id == ids["edited"]
    missing = shelf.reconcile(settings, embedder=embedder, entry_ids=[ids["missing"]])
    assert _outcomes(missing) == {ids["missing"]: ("isolated:mirror-missing", None)}
    assert not shelf.mirror_path(ids["missing"]).exists()


def test_first_ingestion_is_explicit_and_only_for_an_entry_never_converted(legacy):
    shelf, settings, ids = legacy
    embedder = HashEmbedder()
    with pytest.raises(ConsumerError, match="import-pending"):
        shelf.grant_first_ingestion("notes/fresh.md")
    shelf.reconcile(settings, embedder=embedder)
    with pytest.raises(ConsumerError, match="converted-before"):
        shelf.grant_first_ingestion("notes/missing.md")  # converted once: never recreated
    with pytest.raises(ConsumerError, match="converted-before"):
        shelf.grant_first_ingestion("notes/matched.md")
    assert (
        shelf.reconcile(settings, embedder=embedder, entry_ids=[ids["fresh"]])
        .outcome(ids["fresh"])
        .code
        == "legacy-unmatched"
    )  # naming alone never converts
    assert shelf.grant_first_ingestion("notes/fresh.md") is True
    assert shelf.grant_first_ingestion("notes/fresh.md") is False  # idempotent
    result = shelf.reconcile(settings, embedder=embedder, entry_ids=[ids["fresh"]])
    assert _outcomes(result) == {ids["fresh"]: ("searchable", None)}
    hits = shelf.search("kelp", embedder=embedder, reranker=OverlapReranker()).hits
    assert hits[0].entry_id == ids["fresh"]


def test_a_file_at_the_path_is_never_granted_a_first_mirror(legacy):
    shelf, settings, ids = legacy
    shelf.reconcile(settings, embedder=HashEmbedder())
    shelf.mirror_path(ids["fresh"]).write_bytes(b"# Mine\n\nwritten by hand\n")
    with pytest.raises(ConsumerError, match="mirror-user-owned"):
        shelf.grant_first_ingestion("notes/fresh.md")
    assert shelf.mirror_path(ids["fresh"]).read_bytes() == b"# Mine\n\nwritten by hand\n"


def test_with_a_stage_authority_each_import_is_one_zero_embedding_stage(legacy):
    """Failure mode: an imported entry is served without the caller authorizing and
    promoting it, or its import stage embeds or writes Palace rows."""
    import uuid

    from biblios.consumer import BoundShelf
    from biblios.core import BringCurrentRequest, bring_current
    from biblios.pipeline import ConsumerPipeline
    from biblios.representation import default_policy, drawbridge_converter, envelope_renderer
    from tests.test_core import _Holding

    shelf, settings, ids = legacy
    embedder = HashEmbedder()
    holding = _Holding()
    holding.release = True
    pipeline = ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
    )
    result = bring_current(
        BoundShelf(shelf.local_binding(), shelf),
        BringCurrentRequest(str(uuid.uuid4()), None),
        pipeline=pipeline,
        authority=holding,
    )
    [accepted] = result.accepted
    assert accepted.entry_id == ids["matched"] and embedder.calls == []
    assert len(holding.authorized) == 1 and len(holding.promoted) == 1
    hits = shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker())
    assert {hit.stage_sha256 for hit in hits.hits} == {accepted.stage_sha256}


def _bring(shelf, settings, embedder, *, authority=None, control=None, **fields):
    import uuid

    from biblios.consumer import BoundShelf
    from biblios.control import CallControl
    from biblios.core import BringCurrentRequest, bring_current
    from biblios.pipeline import ConsumerPipeline
    from biblios.representation import default_policy, drawbridge_converter, envelope_renderer

    pipeline = ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
    )
    fields.setdefault("operation_id", str(uuid.uuid4()))
    operation = fields.pop("operation_id")
    entry_ids = fields.pop("entry_ids", None)
    return bring_current(
        BoundShelf(shelf.local_binding(), shelf),
        BringCurrentRequest(operation, entry_ids, **fields),
        pipeline=pipeline,
        authority=authority,
        control=control or CallControl(),
    )


def test_a_declined_import_proceeds_only_through_an_explicit_reembedding(legacy):
    """Failure mode: a verified import the caller declined is presented again on a later
    call, when it is named, or by a retry loop, instead of waiting for the caller's
    explicit re-embedding (which is then the only spend)."""
    from tests.test_core import _Declining

    shelf, settings, ids = legacy
    embedder = HashEmbedder()
    caller = _Declining()
    caller.release, caller.refuse = True, True
    matched = ids["matched"]
    first = _bring(shelf, settings, embedder, authority=caller)
    assert (first.outcome(matched).outcome, first.outcome(matched).code) == ("pending", "not-yet")
    asked = len(caller.authorized)
    for entry_ids in (None, (matched,)):  # a retry, then naming it
        again = _bring(shelf, settings, embedder, authority=caller, entry_ids=entry_ids)
        assert again.outcome(matched).code == "not-yet" and again.accepted == ()
    assert len(caller.authorized) == asked and embedder.calls == []
    caller.refuse = False
    chosen = _bring(shelf, settings, embedder, authority=caller, reembed=(matched,))
    assert chosen.outcome(matched).outcome == "searchable" and len(chosen.accepted) == 1
    assert embedder.calls  # the explicit re-embedding is the only spend


def test_the_import_replays_exactly_and_a_cancelled_batch_leaves_no_partial_record(
    legacy, monkeypatch
):
    """Failure mode: a replayed import call redoes work or answers differently; a call
    cancelled inside the import leaves a partial batch, marks the import done, or
    loses entries for the next call."""
    from biblios import core
    from biblios.control import CallControl

    shelf, settings, ids = legacy
    embedder = HashEmbedder()
    monkeypatch.setattr(core, "IMPORT_BATCH", 1)
    checks = []

    def cancelled() -> bool:
        checks.append(1)
        # The call's own check before the writer lock, then the import's first batch:
        # the third check is inside the import, before its second batch.
        return len(checks) == 3

    with pytest.raises(ConsumerError, match="cancelled"):
        _bring(shelf, settings, embedder, control=CallControl(cancelled=cancelled))
    store = DerivedStore(shelf.palace_store, read_only=True)
    recorded = {entry_id for entry_id in ids.values() if store.record(entry_id) is not None}
    assert len(recorded) == 1 and store.counter("legacy-import") == 0
    store.close()
    result = _bring(shelf, settings, embedder, operation_id="00000000-0000-4000-8000-000000000001")
    assert result.outcome(ids["matched"]).outcome == "searchable" and embedder.calls == []
    replay = _bring(shelf, settings, embedder, operation_id="00000000-0000-4000-8000-000000000001")
    assert replay.replayed and dataclasses.replace(replay, replayed=False) == result
    assert embedder.calls == []
