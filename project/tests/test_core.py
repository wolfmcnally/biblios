"""The one indexing core: positive creation authority, user-owned mirrors, no Castle
writes, exact replay, and no re-embedding of unchanged entries."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from biblios.consumer import BoundShelf
from biblios.core import BringCurrentRequest, StageRefused, bring_current
from biblios.pipeline import ConsumerPipeline
from biblios.representation import default_policy, drawbridge_converter, envelope_renderer
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import make_shelf


@pytest.fixture
def shelf(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("notes")
        yield shelf, settings


def _paths(result) -> dict[str, str]:
    return {item.entry_id: item.outcome for item in result.entries}


def test_unchanged_entries_are_not_re_embedded_and_indexing_never_writes_castle(shelf):
    shelf, settings = shelf
    embedder = HashEmbedder()
    first = shelf.add("notes/a.md", b"# A\n\nlighthouse keepers\n")
    shelf.add("notes/b.md", b"# B\n\nharbour pilots\n")
    revision = shelf.revision
    result = shelf.reconcile(settings, embedder=embedder)
    assert sorted(_paths(result).values()) == ["searchable", "searchable"]
    assert len(result.accepted) == 2 and result.provider_calls == 2
    assert shelf.revision == revision  # indexing commits no Castle mutation
    again = shelf.reconcile(settings, embedder=embedder)
    assert again.entries == () and again.provider_calls == 0
    hits = shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker()).hits
    assert hits and hits[0].entry_id == first.affected_entry_ids[0]


def test_direct_edit_is_indexed_only_when_named_and_keeps_the_users_bytes(shelf):
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nfirst\n").affected_entry_ids[0]
    shelf.reconcile(settings, embedder=embedder)
    path = shelf.mirror_path(entry_id)
    edited = path.read_bytes() + b"\nseaward beacons\n"
    path.write_bytes(edited)
    inode = path.stat().st_ino
    # Nothing observed the edit: discovery does not find it without a name or a sweep.
    assert shelf.reconcile(settings, embedder=embedder).entries == ()
    named = shelf.reconcile(settings, embedder=embedder, entry_ids=[entry_id])
    assert _paths(named) == {entry_id: "searchable"}
    assert path.read_bytes() == edited and path.stat().st_ino == inode
    hits = shelf.search("beacons", embedder=embedder, reranker=OverlapReranker()).hits
    assert hits and hits[0].entry_id == entry_id


def test_a_file_at_an_ungranted_path_is_user_owned_and_never_replaced(shelf):
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nbody\n").affected_entry_ids[0]
    # The grant is consumed by the first conversion; a later file is the user's.
    shelf.reconcile(settings, embedder=embedder)
    path = shelf.mirror_path(entry_id)
    path.unlink()
    assert _paths(shelf.reconcile(settings, embedder=embedder, entry_ids=[entry_id])) == {
        entry_id: "isolated:mirror-missing"
    }
    assert not path.exists()  # a converted entry's missing mirror is never reconverted
    assert embedder.calls[-1:] != [] and len(embedder.calls) == 1


def test_copy_gets_one_first_mirror_and_removal_retires_only_with_its_grant(shelf):
    shelf, settings = shelf
    embedder = HashEmbedder()
    source = shelf.add("notes/a.md", b"# A\n\nbody\n").affected_entry_ids[0]
    shelf.reconcile(settings, embedder=embedder)
    copied = shelf.copy("notes/a.md", "notes/c.md")
    new_id = next(item for item in copied.affected_entry_ids if item != source)
    assert _paths(shelf.reconcile(settings, embedder=embedder))[new_id] == "searchable"
    assert shelf.mirror_path(new_id).is_file()
    shelf.remove("notes/a.md")
    removed = shelf.reconcile(settings, embedder=embedder)
    assert _paths(removed) == {source: "removed"}
    assert not shelf.mirror_path(source).exists()
    assert any(shelf.mirror_root.glob(f"{source}.md.retained-*"))


def test_replay_returns_the_recorded_result_and_a_different_request_conflicts(shelf):
    shelf, settings = shelf
    embedder = HashEmbedder()
    shelf.add("notes/a.md", b"# A\n\nbody\n")
    operation = str(uuid.uuid4())
    first = shelf.reconcile(settings, embedder=embedder, operation_id=operation)
    calls = len(embedder.calls)
    replayed = shelf.reconcile(settings, embedder=embedder, operation_id=operation)
    assert replayed.replayed and replayed.entries == first.entries
    assert len(embedder.calls) == calls
    from biblios.control import ConsumerError

    with pytest.raises(ConsumerError, match="operation-conflict"):
        shelf.reconcile(settings, embedder=embedder, operation_id=operation, sweep=True)


class _Refusing:
    def __init__(self) -> None:
        self.creations = 0

    def authorize_creation(self, creation, entry_id):
        self.creations += 1
        raise StageRefused("not-my-curation")

    def authorize(self, stage):  # pragma: no cover - never reached
        raise AssertionError("authorize reached after a refused creation")

    def promote(self, stage, verified):  # pragma: no cover
        raise AssertionError

    def recheck(self, stage, verified, authorization, promotion):  # pragma: no cover
        raise AssertionError


def test_a_grant_row_alone_is_not_creation_authority_under_a_stage_authority(shelf):
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nbody\n").affected_entry_ids[0]
    authority = _Refusing()
    pipeline = ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
    )
    result = bring_current(
        BoundShelf(shelf.local_binding(), shelf),
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=pipeline,
        authority=authority,
    )
    assert authority.creations == 1
    assert result.outcome(entry_id).outcome == "unrecorded"
    assert embedder.calls == [] and not shelf.mirror_path(entry_id).exists()


def test_a_replaced_source_is_never_served_from_its_old_stage_and_keeps_old_bytes(shelf):
    shelf, settings = shelf
    embedder = HashEmbedder()
    reranker = OverlapReranker()
    entry_id = shelf.add("notes/a.md", b"# A\n\nkelp forests\n").affected_entry_ids[0]
    shelf.reconcile(settings, embedder=embedder)
    old = shelf.mirror_path(entry_id).read_bytes()
    shelf.update("notes/a.md", b"# A\n\ncoral reefs\n")
    # Between the replacement and its conversion, the old rows are withheld.
    assert shelf.search("kelp", embedder=embedder, reranker=reranker).hits == ()
    result = shelf.reconcile(settings, embedder=embedder)
    assert _paths(result) == {entry_id: "searchable"}
    hits = shelf.search("coral", embedder=embedder, reranker=reranker).hits
    assert hits and hits[0].entry_id == entry_id
    kept = list(shelf.mirror_root.glob(f"{entry_id}.md.retained-*"))
    assert [path.read_bytes() for path in kept] == [old]


def test_curation_replays_exactly_after_its_paths_changed(shelf):
    shelf, settings = shelf
    first = shelf.add("notes/a.md", b"# A\n\nbody\n", operation_id="add-a")
    shelf.rename("notes/a.md", "b.md")
    shelf.mkdir("archive")
    shelf.move("notes/b.md", "archive/b.md")
    revision = shelf.revision
    again = shelf.add("notes/a.md", b"# A\n\nbody\n", operation_id="add-a")
    assert again.replayed and again.entry_id == first.entry_id
    assert again.affected_entry_ids == first.affected_entry_ids
    assert shelf.revision == revision  # a replay commits nothing


def _volatile(shelf):
    import sqlite3

    from biblios.derived import VOLATILE_NAME

    return sqlite3.connect(shelf.palace_store / VOLATILE_NAME, isolation_level=None)


def test_a_tampered_curation_record_refuses_and_has_no_effect(shelf):
    from biblios.control import ConsumerError

    shelf, _settings = shelf
    first = shelf.add("notes/a.md", b"# A\n\nbody\n", operation_id="add-a")
    conn = _volatile(shelf)
    request = conn.execute(
        "SELECT request FROM curation_requests WHERE request_id=?", (first.operation_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE curation_requests SET request=? WHERE request_id=?",
        (request.replace('"a.md"', '"forged.md"'), first.operation_id),
    )
    conn.close()
    revision = shelf.revision
    with pytest.raises(ConsumerError, match="operation-conflict"):
        shelf.add("notes/a.md", b"# A\n\nbody\n", operation_id="add-a")
    assert shelf.revision == revision
    assert [entry.path for entry in shelf.files()] == ["notes/a.md"]


def test_without_a_record_replay_is_exact_or_refuses_an_ambiguous_root(shelf):
    """A retry of an operation committed before Biblios kept its own record of the
    request is answered from Castle's public result and targeted operation read: a copy
    and a subtree removal (whose results name many entries) replay with their exact root
    after their paths changed, and nothing is executed again."""
    shelf, _settings = shelf
    added = shelf.add("notes/a.md", b"# A\n\nbody\n", operation_id="add-a")
    shelf.add("notes/b.md", b"# B\n\nother\n")
    copied = shelf.copy("notes", "notes-copy", operation_id="copy-notes")
    shelf.mkdir("old")
    shelf.add("old/c.md", b"# C\n\ngone\n")
    shelf.add("old/d.md", b"# D\n\nalso gone\n")
    removed = shelf.remove("old", operation_id="remove-old")
    assert len(copied.affected_entry_ids) > 1 and len(removed.affected_entry_ids) > 1
    conn = _volatile(shelf)
    conn.execute("DELETE FROM curation_requests")
    conn.close()
    shelf.move("notes-copy", "notes/nested-copy")
    revision, paths = shelf.revision, sorted(entry.path for entry in shelf.files())

    again = shelf.add("notes/a.md", b"# A\n\nbody\n", operation_id="add-a")
    assert again.replayed and again.entry_id == added.entry_id
    copy_again = shelf.copy("notes", "notes-copy", operation_id="copy-notes")
    assert copy_again.replayed
    assert (copy_again.entry_id, copy_again.affected_entry_ids) == (
        copied.entry_id,
        copied.affected_entry_ids,
    )
    remove_again = shelf.remove("old", operation_id="remove-old")
    assert remove_again.replayed
    assert (remove_again.entry_id, remove_again.affected_entry_ids) == (
        removed.entry_id,
        removed.affected_entry_ids,
    )
    assert shelf.revision == revision
    assert sorted(entry.path for entry in shelf.files()) == paths


def test_settle_leaves_both_stores_openable_read_only_with_no_sidecars(shelf):
    import sqlite3

    from biblios.derived import DERIVED_NAME

    shelf, settings = shelf
    embedder = HashEmbedder()
    shelf.add("notes/a.md", b"# A\n\nbody\n")
    shelf.reconcile(settings, embedder=embedder)
    bound = BoundShelf(shelf.local_binding(), shelf)
    assert bound.settle_for_readers().state == "settled"
    for db in (shelf.chunks_db, shelf.palace_store / DERIVED_NAME):
        assert not db.with_name(db.name + "-wal").exists()
        assert not db.with_name(db.name + "-shm").exists()
        assert db.read_bytes()[18:20] == b"\x01\x01"  # rollback journal, not WAL
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    # The next write-mode call returns both stores to write-ahead logging and works.
    shelf.add("notes/b.md", b"# B\n\nmore\n")
    assert list(_paths(shelf.reconcile(settings, embedder=embedder)).values()) == ["searchable"]
    assert (shelf.palace_store / DERIVED_NAME).read_bytes()[18:20] == b"\x02\x02"


def test_a_busy_store_reports_a_partial_settle_and_a_retry_completes_it(shelf):
    import sqlite3

    from biblios.derived import DERIVED_NAME

    shelf, settings = shelf
    shelf.add("notes/a.md", b"# A\n\nbody\n")
    shelf.reconcile(settings, embedder=HashEmbedder())
    bound = BoundShelf(shelf.local_binding(), shelf)
    derived = shelf.palace_store / DERIVED_NAME
    reader = sqlite3.connect(derived, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
    try:
        partial = bound.settle_for_readers()
    finally:
        reader.close()
    # The chunk store was already switched when the held reader stopped the call; the
    # result says so instead of claiming nothing changed.
    assert partial.state == "busy" and partial.store == DERIVED_NAME
    assert partial.settled_stores == (shelf.chunks_db.name,)
    assert shelf.chunks_db.read_bytes()[18:20] == b"\x01\x01"
    retried = bound.settle_for_readers()
    assert retried.state == "settled"
    assert retried.settled_stores == (shelf.chunks_db.name, DERIVED_NAME)


def test_a_source_replaced_during_its_first_conversion_is_indexed_on_the_next_call(shelf):
    """Failure mode: the first mirror written for the old source is later mistaken for a
    user-owned file, so the replaced entry is isolated forever, or its bytes are lost."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nkelp forests\n").affected_entry_ids[0]
    replaced: list[int] = []

    def progress(row) -> None:
        if row.stage == "converting" and not replaced:
            replaced.append(1)
            shelf.update("notes/a.md", b"# A\n\ncoral reefs\n")

    first = shelf.reconcile(settings, embedder=embedder, progress=progress)
    assert (first.outcome(entry_id).outcome, first.outcome(entry_id).code) == (
        "pending",
        "stale-view",
    )
    assert shelf.index_status().lagging
    second = shelf.reconcile(settings, embedder=embedder)
    assert _paths(second) == {entry_id: "searchable"}
    hits = shelf.search("coral", embedder=embedder, reranker=OverlapReranker()).hits
    assert hits and hits[0].entry_id == entry_id
    assert shelf.verify().ok, shelf.verify().problems


def test_the_accepted_marker_is_an_optimization_never_authority(shelf):
    """Failure mode: a deleted marker lets a shelf whose accepted index was lost read as
    never built, or a forged marker refuses a never-built shelf with no way back."""
    import shutil

    from biblios.control import ConsumerError
    from biblios.derived import ACCEPTED_MARKER

    shelf, settings = shelf
    embedder = HashEmbedder()
    shelf.add("notes/a.md", b"# A\n\nbody\n")
    shelf.reconcile(settings, embedder=embedder)
    marker = shelf.palace_store / ACCEPTED_MARKER
    assert marker.is_file()
    marker.unlink()
    shutil.rmtree(shelf.chunks_db.parent)
    # The accepted counters still decide: the lost index is refused, not rebuilt silently.
    with pytest.raises(ConsumerError, match="index-missing"):
        shelf.reconcile(settings, embedder=embedder)
    assert sorted(_paths(shelf.reconcile(settings, embedder=embedder, sweep=True)).values()) == [
        "searchable"
    ]
    assert marker.is_file()

    other, other_settings = make_shelf(shelf.root.parent / "other")
    with other:
        other.add("c.md", b"# C\n\nbody\n")
        # A forged marker on a shelf that was never indexed refuses (fail closed).
        (other.palace_store / ACCEPTED_MARKER).write_bytes(b"")
        with pytest.raises(ConsumerError, match="index-missing"):
            other.reconcile(other_settings, embedder=embedder)
        # The explicit sweep is admitted past it and indexes the shelf.
        other.reconcile(other_settings, embedder=embedder, sweep=True)
        assert other.chunks_db.is_file() and other.index_status().searchable == 1


class _Holding:
    """A verifying caller: it signs each authorization and promotion with its own key
    over the exact stage digest (and verified content), rechecks those signatures on
    every reread, and refuses promotion until released."""

    def __init__(self) -> None:
        import secrets

        self.key = secrets.token_bytes(32)
        self.release = False
        self.authorized: list[str] = []
        self.promoted: list[str] = []
        self.rechecked: list[tuple[str, bool]] = []
        self.head: set[str] = set()
        """The caller's signed head: stages it has recorded as accepted."""
        self.sealed: list = []
        self.verified: list = []

    def _sign(self, *parts: str) -> bytes:
        import hashlib
        import hmac

        return hmac.new(self.key, "|".join(parts).encode(), hashlib.sha256).digest()

    def authorize_creation(self, creation, entry_id):
        return b"ok"

    def authorize(self, stage):
        self.authorized.append(stage.stage_sha256)
        return self._sign("authorize", stage.stage_sha256)

    def promote(self, stage, verified):
        if not self.release:
            raise StageRefused("held")
        self.promoted.append(stage.stage_sha256)
        return self._sign("promote", stage.stage_sha256, str(verified.staged_content_sha256))

    def seal_checkpoint(self, checkpoint):
        self.sealed.append(checkpoint)
        return self._sign("checkpoint", checkpoint.sha256)

    def verify_checkpoint(self, checkpoint, seal):
        import hmac

        self.verified.append(checkpoint)
        return hmac.compare_digest(seal, self._sign("checkpoint", checkpoint.sha256))

    def recheck(self, stage, verified, authorization, promotion):
        import hmac

        ok = hmac.compare_digest(authorization, self._sign("authorize", stage.stage_sha256))
        if promotion is not None:
            expected = self._sign(
                "promote", stage.stage_sha256, str(verified.staged_content_sha256)
            )
            ok = ok and hmac.compare_digest(promotion, expected)
        self.rechecked.append((stage.stage_sha256, ok))
        if not ok:
            raise StageRefused("evidence-not-ours")
        return "accepted" if stage.stage_sha256 in self.head else "pending"


def _bring(shelf, settings, embedder, entry_id, authority=None):
    pipeline = ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
    )
    return bring_current(
        BoundShelf(shelf.local_binding(), shelf),
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=pipeline,
        authority=authority,
    )


def test_a_persisted_stage_is_finished_on_restart_without_re_embedding(shelf, monkeypatch):
    """Failure mode: a stage whose serving failed is reported busy forever, re-embedded,
    or discarded while Palace may already hold its rows."""
    import palace.index

    from biblios import discard_stage, pending_stages
    from biblios.control import ConsumerError

    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    real = palace.index.commit_plan

    def failing(**kwargs):
        raise palace.index.IndexError("disk full")

    monkeypatch.setattr(palace.index, "commit_plan", failing)
    failed = shelf.reconcile(settings, embedder=embedder)
    assert failed.outcome(entry_id).code == "publication-failed"
    bound = BoundShelf(shelf.local_binding(), shelf)
    [pending] = pending_stages(bound).stages
    assert pending.entry_id == entry_id and pending.serving
    with pytest.raises(ConsumerError, match="stage-serving"):
        discard_stage(bound, entry_id, pending.stage_sha256)
    monkeypatch.setattr(palace.index, "commit_plan", real)
    calls = len(embedder.calls)
    resumed = shelf.reconcile(settings, embedder=embedder)
    assert resumed.outcome(entry_id).outcome == "searchable"
    assert [s.stage_sha256 for s in resumed.accepted] == [pending.stage_sha256]
    assert len(embedder.calls) == calls and pending_stages(bound).stages == ()
    hits = shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker()).hits
    assert hits and hits[0].entry_id == entry_id


def test_a_tampered_or_epoch_fenced_stage_is_never_served(shelf, monkeypatch):
    """Failure mode: persisted rows altered between calls are served, or a stage from
    an earlier epoch is re-presented after the epoch changed."""
    import dataclasses
    import sqlite3

    import biblios.binding as binding_module
    from biblios import pending_stages
    from biblios.derived import VOLATILE_NAME

    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    holding = _Holding()
    held = _bring(shelf, settings, embedder, entry_id, holding)
    assert held.outcome(entry_id).outcome == "busy"
    bound = BoundShelf(shelf.local_binding(), shelf)
    [pending] = pending_stages(bound).stages
    assert not pending.serving
    conn = sqlite3.connect(shelf.palace_store / VOLATILE_NAME, isolation_level=None)
    conn.execute("UPDATE stage_rows SET body = body || ' forged' WHERE entry_id=?", (entry_id,))
    conn.close()
    holding.release = True
    tampered = _bring(shelf, settings, embedder, entry_id, holding)
    assert tampered.outcome(entry_id).code == "stage-tampered" and holding.promoted == []
    assert shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker()).hits == ()

    # A clean stage persisted under one epoch is fenced when the epoch changes.
    other = shelf.add("notes/b.md", b"# B\n\nharbour\n").affected_entry_ids[0]
    holding.release = False
    _bring(shelf, settings, embedder, other, holding)
    [fenced] = [s for s in pending_stages(bound).stages if s.entry_id == other]
    real = binding_module.validate_handle

    def next_epoch(binding, handle):
        return dataclasses.replace(real(binding, handle), epoch="next-epoch")

    monkeypatch.setattr(binding_module, "validate_handle", next_epoch)
    holding.release = True
    rechecks = len(holding.rechecked)
    result = _bring(shelf, settings, embedder, other, holding)
    # Fenced before any recheck or effect, and never destroyed implicitly: the stage and
    # its evidence stay until the caller discards it.
    assert result.outcome(other).code == "stage-fenced"
    assert fenced.stage_sha256 not in holding.promoted and len(holding.rechecked) == rechecks
    assert [s.stage_sha256 for s in pending_stages(bound).stages if s.entry_id == other] == [
        fenced.stage_sha256
    ]
    from biblios import discard_stage

    assert discard_stage(bound, other, fenced.stage_sha256)


def test_a_discarded_stage_is_never_re_presented(shelf):
    """Failure mode: a caller-cancelled stage is later promoted or served, or a repeated
    cancel fails."""
    from biblios import discard_stage, pending_stages

    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    holding = _Holding()
    _bring(shelf, settings, embedder, entry_id, holding)
    bound = BoundShelf(shelf.local_binding(), shelf)
    [pending] = pending_stages(bound).stages
    assert discard_stage(bound, entry_id, pending.stage_sha256)
    assert not discard_stage(bound, entry_id, pending.stage_sha256)  # idempotent
    holding.release = True
    again = _bring(shelf, settings, embedder, entry_id, holding)
    assert again.outcome(entry_id).outcome == "searchable"
    assert pending.stage_sha256 not in holding.promoted
    assert [s.stage_sha256 for s in again.accepted] != [pending.stage_sha256]


def _forge(shelf, entry_id, *, promoted: bytes | None, body_suffix: str = "") -> None:
    """Rewrite a persisted stage coherently: rows, content digests, canonical bytes and
    stage digest all recomputed so every self-check passes; only caller evidence can
    tell."""
    import dataclasses
    import sqlite3

    from biblios.core import _parse_stage, build_stage, chunk_root, content_digest
    from biblios.derived import VOLATILE_NAME

    conn = sqlite3.connect(shelf.palace_store / VOLATILE_NAME, isolation_level=None)
    sha, canonical = conn.execute(
        "SELECT stage_sha256, canonical FROM stages WHERE entry_id=?", (entry_id,)
    ).fetchone()
    stage = _parse_stage(bytes(canonical), sha)
    if body_suffix:
        conn.execute(
            "UPDATE stage_rows SET body = body || ? WHERE entry_id=?", (body_suffix, entry_id)
        )
    rows = [
        (c, b, i, bytes(v))
        for c, b, i, v in conn.execute(
            "SELECT chunk_id, body, input_sha256, vector FROM stage_rows WHERE entry_id=? "
            "ORDER BY position",
            (entry_id,),
        )
    ]
    content, manifest = content_digest(rows)
    fields = {f.name: getattr(stage, f.name) for f in dataclasses.fields(stage)}
    for name in ("stage_sha256", "canonical"):
        fields.pop(name)
    fields["entry"] = dataclasses.replace(
        stage.entry,
        content_sha256=content,
        chunk_manifest_sha256=manifest,
        chunk_root_sha256=chunk_root(rows),
    )
    forged = build_stage(**fields)
    conn.execute(
        "UPDATE stages SET stage_sha256=?, canonical=?, state='promoted', promotion=? "
        "WHERE entry_id=?",
        (forged.stage_sha256, forged.canonical, promoted, entry_id),
    )
    conn.close()


@pytest.mark.parametrize("forgery", ("promoted-flag", "coherent-rows"))
def test_a_coherently_forged_stage_never_skips_the_callers_recheck(shelf, forgery):
    """Failure mode: a persisted stage rewritten so that every digest self-check passes,
    and flagged promoted, reaches serving without the caller verifying its own evidence."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    holding = _Holding()
    _bring(shelf, settings, embedder, entry_id, holding)
    _forge(
        shelf,
        entry_id,
        promoted=b"forged-promotion",
        body_suffix=" kerosene" if forgery == "coherent-rows" else "",
    )
    holding.release = True
    result = _bring(shelf, settings, embedder, entry_id, holding)
    outcome = result.outcome(entry_id)
    assert (outcome.outcome, outcome.code) == ("pending", "evidence-not-ours")
    assert holding.rechecked and not any(ok for _sha, ok in holding.rechecked)
    assert holding.promoted == [] and result.accepted == ()
    assert shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker()).hits == ()


def test_interrupted_b2_b3_and_accepted_stages_resume_with_no_reembed_or_extra_signature(
    shelf, monkeypatch
):
    """Failure mode: an authentic stage interrupted after B2 or B3, or acknowledged after
    acceptance, is re-embedded, signed again without need, or served twice."""
    import palace.index

    from biblios import pending_stages

    shelf, settings = shelf
    embedder = HashEmbedder()
    bound = BoundShelf(shelf.local_binding(), shelf)

    # B2 only (promotion withheld): resume rechecks, promotes once, serves.
    a = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    holding = _Holding()
    _bring(shelf, settings, embedder, a, holding)
    calls, authorized = len(embedder.calls), list(holding.authorized)
    holding.release = True
    done = _bring(shelf, settings, embedder, a, holding)
    assert done.outcome(a).outcome == "searchable"
    assert len(embedder.calls) == calls and holding.authorized == authorized
    assert len(holding.promoted) == 1 and holding.rechecked[-1][1]

    # B3 (promoted, serving failed): resume rechecks both signatures, never re-promotes.
    b = shelf.add("notes/b.md", b"# B\n\nharbour\n").affected_entry_ids[0]
    real = palace.index.commit_plan

    def failing(**kwargs):
        raise palace.index.IndexError("disk full")

    monkeypatch.setattr(palace.index, "commit_plan", failing)
    assert _bring(shelf, settings, embedder, b, holding).outcome(b).code == "publication-failed"
    monkeypatch.setattr(palace.index, "commit_plan", real)
    calls, promoted = len(embedder.calls), list(holding.promoted)
    resumed = _bring(shelf, settings, embedder, b, holding)
    assert resumed.outcome(b).outcome == "searchable"
    assert len(embedder.calls) == calls and holding.promoted == promoted

    holding.head.update(stage.stage_sha256 for stage in done.accepted)
    holding.head.update(stage.stage_sha256 for stage in resumed.accepted)
    assert {s.entry_id for s in pending_stages(bound).stages} <= {a, b}
    for entry_id in (a, b):  # acknowledged: the next call finishes the cleanup only
        assert _bring(shelf, settings, embedder, entry_id, holding).accepted == ()
    assert pending_stages(bound).stages == ()


@pytest.mark.parametrize("window", ("before-volatile-mark", "after-volatile-mark"))
def test_an_acceptance_whose_result_was_lost_is_re_presented_exactly(shelf, window):
    """Failure mode: the process dies after B5 records acceptance but before the caller
    receives or records the result, so a genuine acceptance is disputed forever, its
    evidence is cleaned up before the caller acknowledges it, or it is re-derived."""
    import sqlite3

    from biblios import pending_stages
    from biblios.derived import VOLATILE_NAME

    shelf, settings = shelf
    embedder = HashEmbedder()
    bound = BoundShelf(shelf.local_binding(), shelf)
    holding = _Holding()
    holding.release = True
    c = shelf.add("notes/c.md", b"# C\n\nkelp\n").affected_entry_ids[0]
    lost = _bring(shelf, settings, embedder, c, holding)  # the caller never records it
    [accepted] = lost.accepted
    if window == "before-volatile-mark":
        # Death between the derived B5 commit and the volatile mark: the flag is stale.
        conn = sqlite3.connect(shelf.palace_store / VOLATILE_NAME, isolation_level=None)
        conn.execute("UPDATE stages SET state='serving' WHERE entry_id=?", (c,))
        conn.close()
    assert [(s.entry_id, s.stage_sha256) for s in pending_stages(bound).stages] == [
        (c, accepted.stage_sha256)
    ]
    calls, promoted, authorized = (
        len(embedder.calls),
        list(holding.promoted),
        list(holding.authorized),
    )
    again = _bring(shelf, settings, embedder, c, holding)
    outcome = again.outcome(c)
    assert (outcome.outcome, outcome.code) == ("searchable", "acceptance-unacknowledged")
    assert again.accepted == (accepted,) and holding.rechecked[-1] == (
        accepted.stage_sha256,
        True,
    )
    assert (len(embedder.calls), holding.promoted, holding.authorized) == (
        calls,
        promoted,
        authorized,
    )
    assert [s.stage_sha256 for s in pending_stages(bound).stages] == [accepted.stage_sha256]
    holding.head.add(accepted.stage_sha256)
    done = _bring(shelf, settings, embedder, c, holding)
    assert (done.outcome(c).outcome, done.outcome(c).code, done.accepted) == (
        "searchable",
        None,
        (),
    )
    assert pending_stages(bound).stages == () and len(embedder.calls) == calls


def test_an_unreadable_store_disputes_a_lost_acceptance_with_palaces_cause(shelf, monkeypatch):
    """Failure mode: a Palace refusal while proving a lost acceptance is re-presented as
    completion, cleaned up, or reported without its dependency cause."""
    import palace.index

    from biblios import pending_stages

    shelf, settings = shelf
    embedder = HashEmbedder()
    holding = _Holding()
    holding.release = True
    c = shelf.add("notes/c.md", b"# C\n\nkelp\n").affected_entry_ids[0]
    [accepted] = _bring(shelf, settings, embedder, c, holding).accepted
    origin = palace.index.IndexError("committed file vector payload is malformed")

    def unreadable(**kwargs):
        raise origin

    monkeypatch.setattr(palace.index, "read_file_vectors", unreadable)
    disputed = _bring(shelf, settings, embedder, c, holding)
    outcome = disputed.outcome(c)
    assert (outcome.code, outcome.cause, disputed.accepted) == (
        "stage-acceptance-disputed",
        origin,
        (),
    )
    bound = BoundShelf(shelf.local_binding(), shelf)
    assert [s.stage_sha256 for s in pending_stages(bound).stages] == [accepted.stage_sha256]


@pytest.mark.parametrize("kind", ("unconverted", "remove"))
def test_a_lost_unconverted_or_removal_acceptance_is_re_presented_exactly(shelf, kind):
    """Failure mode: a recorded unconverted outcome or a retirement whose result never
    reached the caller is disputed forever, redone, or cleaned up unacknowledged."""
    from biblios import pending_stages

    shelf, settings = shelf
    embedder = HashEmbedder()
    bound = BoundShelf(shelf.local_binding(), shelf)
    holding = _Holding()
    holding.release = True
    if kind == "unconverted":
        entry_id = shelf.add("blob.bin", b"\x00\x01 not text \x00").affected_entry_ids[0]
    else:
        entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
        first = _bring(shelf, settings, embedder, entry_id, holding)
        holding.head.update(stage.stage_sha256 for stage in first.accepted)
        assert _bring(shelf, settings, embedder, entry_id, holding).accepted == ()
        shelf.remove("notes/a.md")
    expected = "unconverted" if kind == "unconverted" else "removed"
    lost = _bring(shelf, settings, embedder, entry_id, holding)  # never recorded
    [accepted] = lost.accepted
    assert lost.outcome(entry_id).outcome == expected
    calls, promoted = len(embedder.calls), list(holding.promoted)
    again = _bring(shelf, settings, embedder, entry_id, holding)
    assert (again.outcome(entry_id).outcome, again.outcome(entry_id).code) == (
        expected,
        "acceptance-unacknowledged",
    )
    assert again.accepted == (accepted,)
    assert (len(embedder.calls), holding.promoted) == (calls, promoted)
    assert [s.stage_sha256 for s in pending_stages(bound).stages] == [accepted.stage_sha256]
    holding.head.add(accepted.stage_sha256)
    done = _bring(shelf, settings, embedder, entry_id, holding)
    assert (done.outcome(entry_id).outcome, done.accepted) == (expected, ())
    assert pending_stages(bound).stages == ()


@pytest.mark.parametrize("reached", ("authorized", "promoted-unserved"))
def test_a_forged_acceptance_record_cannot_clear_a_pending_stage(shelf, monkeypatch, reached):
    """Failure mode: a read-grade writer forges the derived record to name a pending stage
    as accepted, so its authentic vectors and evidence are discarded as cleanup, or the
    unserved stage is re-presented as accepted."""
    import sqlite3

    import palace.index

    from biblios import pending_stages
    from biblios.derived import DERIVED_NAME

    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    holding = _Holding()
    if reached == "promoted-unserved":
        holding.release = True

        def failing(**kwargs):
            raise palace.index.IndexError("disk full")

        monkeypatch.setattr(palace.index, "commit_plan", failing)
    _bring(shelf, settings, embedder, entry_id, holding)
    promoted = list(holding.promoted)
    bound = BoundShelf(shelf.local_binding(), shelf)
    [pending] = pending_stages(bound).stages
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME, isolation_level=None)
    conn.execute(
        "INSERT OR REPLACE INTO entries (entry_id, source_cas_id, conversion, converter, "
        "converter_version, mirror_sha256, recorded_ns, state, accepted_stage_sha256, served_seq) "
        "VALUES (?, '', 'converted', 'x', 'x', '', 1, 'accepted', ?, 0)",
        (entry_id, pending.stage_sha256),
    )
    conn.close()
    holding.release = True
    disputed = _bring(shelf, settings, embedder, entry_id, holding)
    assert disputed.outcome(entry_id).code == "stage-acceptance-disputed"
    assert [s.stage_sha256 for s in pending_stages(bound).stages] == [pending.stage_sha256]
    assert holding.promoted == promoted and disputed.accepted == ()


def test_an_explicit_reembed_embeds_every_row_afresh_and_nothing_else_does(shelf):
    """Failure mode: an unchanged entry is re-embedded without being selected, or an entry
    the caller selects for re-embedding silently reuses its committed vectors."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add(
        "notes/a.md", b"# A\n\nlighthouse\n\n## B\n\nharbour\n"
    ).affected_entry_ids[0]
    first = _bring(shelf, settings, embedder, entry_id)
    rows = first.outcome(entry_id).embedded
    pipeline = ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
    )
    bound = BoundShelf(shelf.local_binding(), shelf)
    untouched = bring_current(
        bound, BringCurrentRequest(str(uuid.uuid4()), (entry_id,)), pipeline=pipeline
    )
    selected = bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,), reembed=(entry_id,)),
        pipeline=pipeline,
    )
    assert rows > 0 and untouched.outcome(entry_id).embedded == 0
    assert selected.outcome(entry_id).outcome == "searchable"
    assert selected.outcome(entry_id).embedded == rows and selected.provider_calls >= 1


def test_an_authority_free_call_never_finishes_a_stage_the_callers_authority_signed(shelf):
    """Failure mode: a call that omits the authority finishes, cleans up or re-presents a
    stage the caller authorized, skipping the caller's recheck on that reread (G4)."""
    from biblios import pending_stages

    shelf, settings = shelf
    embedder = HashEmbedder()
    bound = BoundShelf(shelf.local_binding(), shelf)
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    holding = _Holding()
    assert _bring(shelf, settings, embedder, entry_id, holding).outcome(entry_id).code == "held"
    pending = pending_stages(bound).stages
    calls = len(embedder.calls)
    omitted = _bring(shelf, settings, embedder, entry_id)
    assert omitted.outcome(entry_id).code == "stage-authority-required"
    assert omitted.accepted == () and len(embedder.calls) == calls
    assert pending_stages(bound).stages == pending
    holding.release = True
    done = _bring(shelf, settings, embedder, entry_id, holding)
    assert done.outcome(entry_id).outcome == "searchable" and holding.rechecked[-1][1]


def test_a_state_published_without_the_authority_is_reported_with_its_digest(shelf):
    """Failure mode: an entry published by a call that omitted the authority reaches an
    authenticating caller as current with nothing it can check against its own signed
    head, so the caller cannot refuse a state it never authorized."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    unsigned = _bring(shelf, settings, embedder, entry_id)
    (stage,) = unsigned.accepted
    holding = _Holding()
    seen = _bring(shelf, settings, embedder, entry_id, holding)
    assert seen.outcome(entry_id).outcome == "searchable"
    assert seen.outcome(entry_id).stage_sha256 == stage.stage_sha256
    # Nothing was signed for it; the caller's own head is what refuses it.
    assert holding.authorized == [] and stage.stage_sha256 not in holding.head


@pytest.mark.parametrize("phase", ("authorized", "serving"))
def test_a_replaced_sources_persisted_stage_is_never_finished_on_old_authorization(
    shelf, monkeypatch, phase
):
    """Failure mode: after a source replacement (a new ingestion), the old source's
    persisted stage is promoted, written or served because it was authorized before, or
    because its phase marker says B4 began; or the old stage and its evidence are
    dropped without the caller's decision; or a read serves either source meanwhile."""
    import palace.index

    from biblios import pending_stages

    shelf, settings = shelf
    embedder = HashEmbedder()
    bound = BoundShelf(shelf.local_binding(), shelf)
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    holding = _Holding()
    if phase == "serving":
        holding.release = True

        def failing(**kwargs):
            raise palace.index.IndexError("disk full")

        real = palace.index.commit_plan
        monkeypatch.setattr(palace.index, "commit_plan", failing)
        first = _bring(shelf, settings, embedder, entry_id, holding)
        assert first.outcome(entry_id).code == "publication-failed"
        monkeypatch.setattr(palace.index, "commit_plan", real)
    else:
        assert _bring(shelf, settings, embedder, entry_id, holding).outcome(entry_id).code == "held"
    (pending,) = pending_stages(bound).stages
    mirror = shelf.mirror_path(entry_id)
    staged = mirror.read_bytes()
    promoted, calls = list(holding.promoted), len(embedder.calls)
    commits, committing = [], palace.index.commit_plan
    monkeypatch.setattr(
        palace.index, "commit_plan", lambda **kwargs: commits.append(1) or committing(**kwargs)
    )
    shelf.update("notes/a.md", b"# A\n\ncoral reefs\n")
    holding.release = True
    out = _bring(shelf, settings, embedder, entry_id, holding)
    expected = "recovery-required" if phase == "serving" else "pending"
    assert (out.outcome(entry_id).outcome, out.outcome(entry_id).code) == (
        expected,
        "source-replaced",
    )
    assert out.accepted == () and commits == [] and holding.promoted == promoted
    assert len(embedder.calls) == calls and pending_stages(bound).stages == (pending,)
    assert mirror.read_bytes() == staged
    for query in ("lighthouse", "coral"):
        found = shelf.search(query, embedder=embedder, reranker=OverlapReranker())
        assert entry_id not in {hit.entry_id for hit in found.hits}


class _EditingEmbedder(HashEmbedder):
    """Rewrites a mirror the first time it embeds, inside the stage being prepared."""

    def __init__(self, path: Path, data: bytes) -> None:
        super().__init__()
        self.path, self.data = path, data

    def embed(self, texts):
        if self.data is not None:
            self.path.write_bytes(self.data)
            self.data = None
        return super().embed(texts)


def test_an_edit_during_staging_is_never_recorded_as_the_accepted_bytes(shelf):
    shelf, settings = shelf
    entry_id = shelf.add("notes/a.md", b"# A\n\nfirst\n").affected_entry_ids[0]
    shelf.reconcile(settings, embedder=HashEmbedder())
    path = shelf.mirror_path(entry_id)
    staged = path.read_bytes() + b"\nseaward beacons\n"
    path.write_bytes(staged)
    # Same size as the staged bytes, written while they are embedded.
    raced = staged.replace(b"seaward beacons", b"tidal estuaries")
    embedder = _EditingEmbedder(path, raced)
    assert _paths(shelf.reconcile(settings, embedder=embedder, entry_ids=[entry_id])) == {
        entry_id: "searchable"
    }
    reranker = OverlapReranker()
    # The accepted stage holds the staged bytes; the mirror now differs, so serving
    # (which hashes exact bytes) withholds the entry instead of serving it as current.
    assert shelf.search("beacons", embedder=embedder, reranker=reranker).hits == ()
    # The edit was marked: an ordinary call with no name finds and indexes it.
    again = shelf.reconcile(settings, embedder=embedder)
    assert _paths(again) == {entry_id: "searchable"}
    hits = shelf.search("estuaries", embedder=embedder, reranker=reranker).hits
    assert hits and hits[0].entry_id == entry_id
    assert path.read_bytes() == raced


def test_work_for_an_entry_is_done_in_the_same_call_that_finishes_its_earlier_stage(shelf):
    """Failure mode: a call that finishes or cleans up an earlier stage returns for that
    entry, so a direct edit in the same call is dropped and its marks are cleared."""
    from biblios import pending_stages

    shelf, settings = shelf
    embedder = HashEmbedder()
    bound = BoundShelf(shelf.local_binding(), shelf)
    holding = _Holding()
    holding.release = True
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    first = _bring(shelf, settings, embedder, entry_id, holding)
    holding.head.update(stage.stage_sha256 for stage in first.accepted)
    assert pending_stages(bound).stages  # acknowledged next call: cleanup only
    path = shelf.mirror_path(entry_id)
    path.write_bytes(path.read_bytes() + b"\nseaward beacons\n")
    result = _bring(shelf, settings, embedder, entry_id, holding)
    assert result.outcome(entry_id).outcome == "searchable"
    [accepted] = result.accepted
    assert accepted.stage_sha256 not in holding.head
    hits = shelf.search("beacons", embedder=embedder, reranker=OverlapReranker()).hits
    assert hits and hits[0].entry_id == entry_id


def test_an_unreadable_mirror_or_a_failing_entry_never_blocks_the_rest(shelf, monkeypatch):
    """Failure mode: one mirror the process cannot read, or a store error while one entry
    is prepared, aborts the whole call instead of isolating that entry with its cause."""
    import os
    import sqlite3

    from biblios import core

    shelf, settings = shelf
    embedder = HashEmbedder()
    a = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    b = shelf.add("notes/b.md", b"# B\n\nharbour\n").affected_entry_ids[0]
    shelf.reconcile(settings, embedder=embedder)
    for entry_id, word in ((a, b"beacons"), (b, b"pilots")):
        path = shelf.mirror_path(entry_id)
        path.write_bytes(path.read_bytes() + b"\n" + word + b"\n")
    locked = shelf.mirror_path(a)
    os.chmod(locked, 0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("this process can read a mode-000 file (running as root)")
        result = shelf.reconcile(settings, embedder=embedder, entry_ids=[a, b])
    finally:
        os.chmod(locked, 0o644)
    outcome = result.outcome(a)
    assert (outcome.outcome, outcome.code) == ("isolated:mirror-unreadable", None)
    assert isinstance(outcome.cause.__cause__, PermissionError)
    assert result.outcome(b).outcome == "searchable"

    real = core._Run._validated

    def failing(self, entry, data, record):
        if entry.entry_id == a:
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, entry, data, record)

    monkeypatch.setattr(core._Run, "_validated", failing)
    path = shelf.mirror_path(b)
    path.write_bytes(path.read_bytes() + b"\nkelp\n")
    result = shelf.reconcile(settings, embedder=embedder, entry_ids=[a, b])
    outcome = result.outcome(a)
    assert (outcome.outcome, outcome.code) == ("pending", "prepare-failed")
    assert isinstance(outcome.cause, sqlite3.OperationalError)
    assert result.outcome(b).outcome == "searchable"


def test_a_changed_committed_vector_is_never_reused_into_a_new_signed_stage(shelf, monkeypatch):
    """Failure mode: a committed Palace vector altered after acceptance is reused for an
    unchanged chunk of an edited file and so laundered into the next signed stage."""
    import dataclasses

    import palace.index

    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse keepers\n").affected_entry_ids[0]
    shelf.reconcile(settings, embedder=embedder)
    path = shelf.mirror_path(entry_id)
    path.write_bytes(path.read_bytes() + b"\n## More\n\nseaward beacons\n")
    real = palace.index.read_file_vectors
    reads = []

    def altered(**kwargs):
        snapshot = real(**kwargs)
        reads.append(kwargs["path"])
        if snapshot is None or len(reads) > 1:
            return snapshot  # only the reuse read, before anything is committed
        first, *rest = snapshot.chunks
        vector = (first.vector[0] + 0.5, *first.vector[1:])
        return dataclasses.replace(
            snapshot, chunks=(dataclasses.replace(first, vector=vector), *rest)
        )

    monkeypatch.setattr(palace.index, "read_file_vectors", altered)
    embedder.calls.clear()
    result = shelf.reconcile(settings, embedder=embedder, entry_ids=[entry_id])
    assert result.outcome(entry_id).outcome == "searchable"
    embedded = [text for call in embedder.calls for text in call]
    assert any("lighthouse keepers" in text for text in embedded)  # not reused


class _Declining(_Holding):
    refuse = False

    def authorize(self, stage):
        if not self.refuse:
            return super().authorize(stage)
        self.authorized.append(stage.stage_sha256)
        raise StageRefused("not-yet")


def test_an_authorize_refusal_is_not_re_prepared_until_bytes_change_or_it_is_named(shelf):
    """Failure mode: a stage the caller refused is prepared and re-embedded on every
    later call, spending provider calls on bytes the caller already declined."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    refusing = _Declining()
    refusing.release = True
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    refusing.head.update(
        s.stage_sha256 for s in _bring(shelf, settings, embedder, entry_id, refusing).accepted
    )
    path = shelf.mirror_path(entry_id)
    path.write_bytes(path.read_bytes() + b"\nseaward beacons\n")
    refusing.refuse = True
    first = _bring(shelf, settings, embedder, entry_id, refusing)
    assert (first.outcome(entry_id).outcome, first.outcome(entry_id).code) == (
        "pending",
        "not-yet",
    )
    pipeline = ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
    )
    calls, authorized = len(embedder.calls), len(refusing.authorized)
    unnamed = bring_current(
        BoundShelf(shelf.local_binding(), shelf),
        BringCurrentRequest(str(uuid.uuid4()), None),
        pipeline=pipeline,
        authority=refusing,
    )
    assert unnamed.outcome(entry_id).code == "not-yet"  # requeued, answered from record
    assert (len(embedder.calls), len(refusing.authorized)) == (calls, authorized)
    named = _bring(shelf, settings, embedder, entry_id, refusing)  # asking again
    assert named.outcome(entry_id).code == "not-yet"
    assert len(refusing.authorized) == authorized + 1
    path.write_bytes(path.read_bytes() + b"\nharbour pilots\n")  # new bytes: a new key
    calls = len(embedder.calls)
    changed = bring_current(
        BoundShelf(shelf.local_binding(), shelf),
        BringCurrentRequest(str(uuid.uuid4()), None, sweep=True),
        pipeline=pipeline,
        authority=refusing,
    )
    assert changed.outcome(entry_id).code == "not-yet" and len(embedder.calls) > calls


class _Anchored(_Holding):
    """Signs a stage only when the rows it reuses come from a stage it accepted."""

    def authorize(self, stage):
        if stage.reused_from_sha256 not in (None, *self.head):
            raise StageRefused("reuse-unknown")
        return super().authorize(stage)


def test_a_substituted_accepted_stage_never_anchors_reuse_into_a_signed_stage(shelf):
    """Failure mode: after a source replacement (no predecessor), the stored record of
    the old accepted stage is substituted by a self-consistent one whose digests the
    committed rows reproduce, and its rows are reused into a stage the caller signs."""
    import dataclasses
    import sqlite3

    from biblios.core import _parse_stage, build_stage
    from biblios.derived import DERIVED_NAME

    shelf, settings = shelf
    embedder = HashEmbedder()
    anchored = _Anchored()
    anchored.release = True
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse keepers\n").affected_entry_ids[0]
    first = _bring(shelf, settings, embedder, entry_id, anchored)
    [accepted] = first.accepted
    anchored.head.add(accepted.stage_sha256)
    shelf.update("notes/a.md", b"# A\n\nlighthouse keepers\n\n## More\n\nkelp\n")

    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME, isolation_level=None)
    sha, canonical = conn.execute(
        "SELECT stage_sha256, canonical FROM accepted_stages WHERE entry_id=?", (entry_id,)
    ).fetchone()
    stage = _parse_stage(bytes(canonical), sha)
    fields = {f.name: getattr(stage, f.name) for f in dataclasses.fields(stage)}
    for name in ("stage_sha256", "canonical"):
        fields.pop(name)
    forged = build_stage(**{**fields, "operation_id": str(uuid.uuid4())})
    conn.execute(
        "UPDATE accepted_stages SET stage_sha256=?, canonical=? WHERE entry_id=?",
        (forged.stage_sha256, forged.canonical, entry_id),
    )
    conn.close()

    result = _bring(shelf, settings, embedder, entry_id, anchored)
    outcome = result.outcome(entry_id)
    assert (outcome.outcome, outcome.code) == ("pending", "reuse-unknown")
    assert result.accepted == ()


@pytest.mark.parametrize("promotion", ("text", b"x" * (16 * 1024 + 1)))
def test_a_malformed_promotion_is_refused_and_never_persisted_or_served(shelf, promotion):
    """Failure mode: a promotion that is not bounded bytes is stored as the stage's
    evidence and the stage is served on it."""
    shelf, settings = shelf
    embedder = HashEmbedder()

    class Malformed(_Holding):
        def promote(self, stage, verified):
            return promotion

    authority = Malformed()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    result = _bring(shelf, settings, embedder, entry_id, authority)
    assert result.outcome(entry_id).code == "evidence-invalid" and result.accepted == ()
    assert shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker()).hits == ()


def test_a_user_file_under_an_unconverted_record_is_never_moved_on_replacement(shelf):
    """Failure mode: a file the user placed at the mirror path of an entry whose recorded
    outcome is unconverted is displaced or overwritten when its source is replaced, the
    absence of a prior conversion being taken as authority over it."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/blob.md", b"\x00\x01 not text \x00").affected_entry_ids[0]
    assert _paths(shelf.reconcile(settings, embedder=embedder)) == {entry_id: "unconverted"}
    path = shelf.mirror_path(entry_id)
    path.write_bytes(b"# My own notes\n\nwritten by hand\n")
    inode = path.stat().st_ino
    shelf.update("notes/blob.md", b"# Blob\n\nnow text\n")
    result = shelf.reconcile(settings, embedder=embedder, entry_ids=[entry_id])
    outcome = result.outcome(entry_id)
    assert (outcome.outcome, outcome.code) == ("isolated:mirror-user-owned", None)
    assert path.read_bytes() == b"# My own notes\n\nwritten by hand\n"
    assert path.stat().st_ino == inode and result.accepted == ()


def test_hits_carry_the_accepted_stage_a_caller_can_check_against_its_own_record(shelf):
    """Failure mode: a search serves state the caller never accepted and gives it nothing
    to check, or answers from mutable rows that no longer agree with the accepted stage."""
    import hashlib
    import sqlite3

    from biblios.derived import DERIVED_NAME

    shelf, settings = shelf
    embedder = HashEmbedder()
    holding = _Holding()
    holding.release = True
    ours = shelf.add("notes/a.md", b"# A\n\nlighthouse keepers\n").affected_entry_ids[0]
    holding.head.update(
        s.stage_sha256 for s in _bring(shelf, settings, embedder, ours, holding).accepted
    )
    theirs = shelf.add("notes/b.md", b"# B\n\nlighthouse pilots\n").affected_entry_ids[0]
    _bring(shelf, settings, embedder, theirs)  # published without the caller's authority
    result = shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker())
    stages = {stage.entry_id: stage for stage in result.stages}
    assert {hit.entry_id for hit in result.hits} == {ours, theirs} == set(stages)
    for hit in result.hits:
        stage = stages[hit.entry_id]
        assert hashlib.sha256(stage.canonical).hexdigest() == stage.stage_sha256 == hit.stage_sha256
        assert hit.mirror_sha256 == stage.mirror_sha256
    # The caller's own discipline: only stages in its signed head are its accepted state.
    assert {h.entry_id for h in result.hits if h.stage_sha256 in holding.head} == {ours}

    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME, isolation_level=None)
    conn.execute("UPDATE accepted_chunks SET body_sha256=? WHERE entry_id=?", ("0" * 64, theirs))
    conn.execute("UPDATE entries SET accepted_stage_sha256=? WHERE entry_id=?", ("f" * 64, ours))
    conn.close()
    again = shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker())
    assert again.hits == () and again.stages == ()
    assert {(w.entry_id, w.category) for w in again.withheld} == {
        (ours, "stage-inconsistent"),
        (theirs, "proof-invalid"),  # the changed row no longer reaches the signed root
    }


def _budgeted(shelf, settings, embedder, entry_id, authority, budget):
    pipeline = ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
        max_snapshot_bytes=budget,
    )
    return bring_current(
        BoundShelf(shelf.local_binding(), shelf),
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=pipeline,
        authority=authority,
    )


def test_a_plan_certainly_over_the_snapshot_budget_is_refused_before_any_spend(shelf, monkeypatch):
    """Failure mode: an entry whose committed snapshot must exceed the budget (its bodies
    and vectors alone exceed it) is embedded, authorized and staged before Palace refuses
    it at B4; and, just above that floor, a B4 refusal is hidden or misreported."""
    from biblios import core
    from biblios.derived import VOLATILE_NAME

    shelf, settings = shelf
    text = b"# A\n\n" + b"".join(b"## part %d\n\nlighthouse %d\n\n" % (i, i) for i in range(5))
    entry_id = shelf.add("notes/a.md", text).affected_entry_ids[0]
    floors = []
    measured = core._Run._snapshot_floor
    monkeypatch.setattr(
        core._Run,
        "_snapshot_floor",
        lambda self, plan: floors.append(measured(self, plan)) or floors[-1],
    )
    embedder, caller = HashEmbedder(), _Holding()
    caller.release = True
    refused = _budgeted(shelf, settings, embedder, entry_id, caller, budget=1024)
    assert (refused.outcome(entry_id).outcome, refused.outcome(entry_id).code) == (
        "isolated:snapshot-too-large",
        None,
    )
    assert embedder.calls == [] and caller.authorized == [] and caller.promoted == []
    conn = sqlite3.connect(shelf.palace_store / VOLATILE_NAME)
    assert conn.execute("SELECT COUNT(*) FROM stages").fetchone()[0] == 0
    conn.close()
    # At exactly the floor the plan passes the precheck; Palace counts more than the floor,
    # so the refusal comes at B4, after authorization, and is reported with its cause.
    floor = floors[-1]
    late = _budgeted(shelf, settings, embedder, entry_id, caller, budget=floor)
    outcome = late.outcome(entry_id)
    assert (outcome.outcome, outcome.code) == ("recovery-required", "publication-failed")
    assert "exceeds the requested bounds" in str(outcome.cause)
    assert caller.authorized and embedder.calls


def _castle_counts(tmp_path: Path, n: int, monkeypatch) -> tuple[dict, dict, dict]:
    """Castle record validations, admissions and whole-namespace reads for one new entry
    brought current, a no-change call and a one-hit search, on a shelf of ``n`` entries."""
    from castle.namespace_store import NamespaceHandle

    counts = {"validations": 0, "admissions": 0, "whole_reads": 0}
    touch, admitted, entries = (
        NamespaceHandle.touch_entry,
        NamespaceHandle.admitted,
        NamespaceHandle.entries,
    )

    def counted_touch(self, identity, revision):
        counts["validations"] += identity not in self._entry_memo
        return touch(self, identity, revision)

    def counted_admitted(self):
        counts["admissions"] += 1
        return admitted(self)

    def counted_entries(self, **kwargs):
        counts["whole_reads"] += 1
        return entries(self, **kwargs)

    monkeypatch.setattr(NamespaceHandle, "touch_entry", counted_touch)
    monkeypatch.setattr(NamespaceHandle, "admitted", counted_admitted)
    monkeypatch.setattr(NamespaceHandle, "entries", counted_entries)

    def measured(call) -> dict:
        counts.update(validations=0, admissions=0, whole_reads=0)
        result = call()
        assert not hasattr(result, "hits") or len(result.hits) == 1
        return dict(counts)

    shelf, settings = make_shelf(tmp_path / str(n))
    with shelf:
        shelf.mkdir("notes")
        embedder = HashEmbedder()
        for index in range(n):
            shelf.add(f"notes/{index}.md", f"# {index}\n\nharbour log {index}\n".encode())
        shelf.reconcile(settings, embedder=embedder)
        shelf.add("notes/new.md", b"# New\n\nlighthouse keepers\n")
        changed = measured(lambda: shelf.reconcile(settings, embedder=embedder))
        unchanged = measured(lambda: shelf.reconcile(settings, embedder=embedder))
        searched = measured(
            lambda: shelf.search(
                "lighthouse", limit=1, embedder=embedder, reranker=OverlapReranker()
            )
        )
    return changed, unchanged, searched


def test_castle_validation_and_admission_per_brought_current_entry_do_not_grow_with_n(
    tmp_path: Path, monkeypatch
):
    """ONE-02, Castle half, against Castle's touched-record reads: bringing one new entry
    current, a no-change call and a search validate and admit the same number of Castle
    records at N=20, 60 and 200, and none reads the whole namespace."""
    measured = [_castle_counts(tmp_path, n, monkeypatch) for n in (20, 60, 200)]
    assert measured[0] == measured[1] == measured[2]
    for counts in measured[0]:
        assert counts["whole_reads"] == 0
    assert measured[0][1]["validations"] == 0
