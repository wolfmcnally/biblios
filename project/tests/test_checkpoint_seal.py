"""Conversion checkpoints under a stage authority: the caller seals each result this call
produced before it is persisted for reuse, and verifies every persisted result before it
is used; persisted content is never sealed, and a stage names the checkpoint it used."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from biblios.core import StageRefused
from biblios.derived import DERIVED_NAME
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import consumer_fixture, identifier, lane_pipeline
from tests.test_core import _Holding


class _Sealer(_Holding):
    """A caller that seals and verifies checkpoints with its own key, records the thread
    each checkpoint callback ran on, and acknowledges every stage it signs."""

    def __init__(self) -> None:
        super().__init__()
        self.release = True
        self.head = self.authorized
        self.refuse_seal = False
        self.raise_seal = False
        self.refuse_stage = False
        self.threads: set[int] = set()

    def seal_checkpoint(self, checkpoint):
        self.threads.add(threading.get_ident())
        if self.raise_seal:
            raise RuntimeError("signer unavailable")
        if self.refuse_seal:
            raise StageRefused("not-my-conversion")
        return super().seal_checkpoint(checkpoint)

    def verify_checkpoint(self, checkpoint, seal):
        self.threads.add(threading.get_ident())
        return super().verify_checkpoint(checkpoint, seal)

    def authorize(self, stage):
        if self.refuse_stage:
            raise StageRefused("not-yet")
        return super().authorize(stage)


@pytest.fixture
def lane(tmp_path: Path):
    fixture = consumer_fixture(tmp_path)
    calls: list = []
    embedder = HashEmbedder()
    try:
        pipeline = lane_pipeline(fixture, calls, embedder=embedder)
        entry_id = fixture.bound.curate(
            fixture.request("one.txt", b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        yield fixture, pipeline, calls, embedder, entry_id
    finally:
        fixture.close()


def _derive(fixture, pipeline, entry_id, authority, **options):
    return fixture.bound.derive(
        pipeline, entry_id, operation_id=identifier(), authority=authority, **options
    )


def _outcome(result, entry_id):
    item = result.outcome(entry_id)
    return item.outcome, item.code


def _document(fixture, entry_id) -> dict:
    conn = sqlite3.connect(fixture.binding.palace_root / DERIVED_NAME)
    try:
        [document] = conn.execute(
            "SELECT document FROM checkpoints WHERE entry_id=?", (entry_id,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(document)


def _store(fixture, entry_id, document: dict) -> None:
    conn = sqlite3.connect(fixture.binding.palace_root / DERIVED_NAME, isolation_level=None)
    try:
        conn.execute(
            "UPDATE checkpoints SET document=? WHERE entry_id=?",
            (json.dumps(document, sort_keys=True), entry_id),
        )
    finally:
        conn.close()


def _accepted_stage(fixture, entry_id) -> dict:
    conn = sqlite3.connect(fixture.binding.palace_root / DERIVED_NAME)
    try:
        [canonical] = conn.execute(
            "SELECT canonical FROM accepted_stages WHERE entry_id=?", (entry_id,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(bytes(canonical))


def test_a_fresh_conversion_is_sealed_before_it_is_persisted_and_named_by_its_stage(lane):
    """Failure mode: a conversion or rendering is persisted for reuse before the caller
    seals it, the seal covers other bytes than the identity persisted, a callback runs
    off the invoking thread, or the stage names no checkpoint (or the wrong one), so the
    caller cannot tell fresh work from reuse."""
    fixture, pipeline, calls, embedder, entry_id = lane
    sealer = _Sealer()
    persisted_at_seal: list[dict | None] = []
    seal = sealer.seal_checkpoint

    def observing(checkpoint):
        conn = sqlite3.connect(fixture.binding.palace_root / DERIVED_NAME)
        row = conn.execute(
            "SELECT document FROM checkpoints WHERE entry_id=?", (entry_id,)
        ).fetchone()
        conn.close()
        persisted_at_seal.append(None if row is None else json.loads(row[0]))
        return seal(checkpoint)

    sealer.seal_checkpoint = observing
    result = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(result, entry_id) == ("searchable", None)
    assert [item.state for item in sealer.sealed] == ["converted", "complete"]
    # When each seal was asked for, nothing reusable for that level was persisted yet.
    assert [document.get("state") for document in persisted_at_seal] == [
        "dispatched",
        "render-dispatched",
    ]
    assert "seal" not in persisted_at_seal[0] and "mirror" not in persisted_at_seal[1]
    for checkpoint in sealer.sealed:
        assert hashlib.sha256(checkpoint.canonical).hexdigest() == checkpoint.sha256
        assert json.loads(checkpoint.canonical)["schema"] == "biblios.checkpoint/1"
        assert (checkpoint.entry_id, checkpoint.source_cas_id) == (
            entry_id,
            json.loads(checkpoint.canonical)["source_cas_id"],
        )
    stage = _accepted_stage(fixture, entry_id)
    assert stage["entry"]["checkpoint"] == {"sha256": sealer.sealed[-1].sha256, "reused": False}
    assert sealer.verified == [] and len(calls) == 1
    assert sealer.threads == {threading.get_ident()}


def test_a_persisted_checkpoint_is_only_verified_and_never_sealed_again(lane):
    """Failure mode: a checkpoint read back from storage is presented to the caller as a
    fresh seal request (so a store write launders into the caller's seal), is reused
    without verification, or re-runs the converter."""
    fixture, pipeline, calls, embedder, entry_id = lane
    sealer = _Sealer()
    sealer.refuse_stage = True
    refused = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(refused, entry_id) == ("pending", "not-yet")
    sealed = list(sealer.sealed)
    sealer.refuse_stage = False
    resumed = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(resumed, entry_id) == ("searchable", None)
    assert sealer.sealed == sealed and len(calls) == 1
    assert [item.sha256 for item in sealer.verified] == [sealed[-1].sha256]
    stage = _accepted_stage(fixture, entry_id)
    assert stage["entry"]["checkpoint"] == {"sha256": sealed[-1].sha256, "reused": True}


@pytest.mark.parametrize(
    "tamper", ("conversion-body", "mirror-markdown", "foreign-seal", "no-seal")
)
def test_a_changed_or_unsealed_checkpoint_is_never_reused_converted_or_sealed(lane, tamper):
    """Failure mode: a checkpoint changed in the store (its conversion, its rendering or
    a seal copied from another checkpoint), or one
    persisted without a seal, reaches a stage the caller signs, is sealed as if fresh, or
    makes Biblios convert or embed again by itself."""
    fixture, pipeline, calls, embedder, entry_id = lane
    sealer = _Sealer()
    sealer.refuse_stage = True
    _derive(fixture, pipeline, entry_id, sealer)
    document = _document(fixture, entry_id)
    if tamper == "conversion-body":
        document["conversion"]["body"] = "Lighthouse lens sabotage."
    elif tamper == "mirror-markdown":
        document["mirror"]["markdown"] = document["mirror"]["markdown"].replace("lens", "lamp")
    elif tamper == "foreign-seal":
        # The caller's genuine seal, but over another checkpoint (the converted level).
        document["seal"] = sealer._sign("checkpoint", sealer.sealed[0].sha256).hex()
    else:
        del document["seal"]
    _store(fixture, entry_id, document)
    sealed, embedded = list(sealer.sealed), len(embedder.calls)
    sealer.refuse_stage = False
    authorized = len(sealer.authorized)
    result = _derive(fixture, pipeline, entry_id, sealer)
    expected = "checkpoint-unsealed" if tamper == "no-seal" else "checkpoint-not-ours"
    assert _outcome(result, entry_id) == ("pending", expected)
    assert sealer.sealed == sealed and len(calls) == 1
    assert len(embedder.calls) == embedded and len(sealer.authorized) == authorized
    assert _document(fixture, entry_id) == document  # never rewritten or re-sealed


def test_a_checkpoint_of_another_store_is_refused_without_asking_the_caller(lane):
    """Failure mode: a checkpoint carried over from another store or lineage is offered
    to the caller's verification (or reused) as though it were this shelf's."""
    fixture, pipeline, calls, embedder, entry_id = lane
    sealer = _Sealer()
    sealer.refuse_stage = True
    _derive(fixture, pipeline, entry_id, sealer)
    document = _document(fixture, entry_id)
    document["generation"]["lineage_id"] = "other-lineage"
    _store(fixture, entry_id, document)
    sealer.refuse_stage = False
    result = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(result, entry_id) == ("pending", "checkpoint-not-ours")
    assert sealer.verified == [] and len(calls) == 1


def test_a_refused_seal_persists_nothing_reusable_and_is_never_retried_by_itself(lane):
    """Failure mode: a conversion the caller refused to seal is persisted for reuse,
    reaches a stage, or is converted again (a repeated spend) by a later call, including
    one that asks to retry uncertain dispatches."""
    fixture, pipeline, calls, embedder, entry_id = lane
    sealer = _Sealer()
    sealer.refuse_seal = True
    first = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(first, entry_id) == ("pending", "checkpoint-seal-refused")
    document = _document(fixture, entry_id)
    assert document["state"] == "seal-refused" and "conversion" not in document
    sealer.refuse_seal = False
    for retry in (False, True):
        again = _derive(fixture, pipeline, entry_id, sealer, retry_uncertain=retry)
        assert _outcome(again, entry_id) == ("pending", "checkpoint-seal-refused")
    assert len(calls) == 1 and sealer.authorized == [] and embedder.calls == []


def test_a_failed_seal_leaves_the_dispatch_uncertain_until_the_caller_retries(lane):
    """Failure mode: a seal that raised is treated as refused or as sealed, the result is
    persisted unsealed, or the converter runs again without the caller's retry."""
    fixture, pipeline, calls, embedder, entry_id = lane
    sealer = _Sealer()
    sealer.raise_seal = True
    first = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(first, entry_id) == ("uncertain", "callback-failed")
    assert isinstance(first.outcome(entry_id).cause.__cause__, RuntimeError)
    document = _document(fixture, entry_id)
    assert document["state"] == "dispatched" and "conversion" not in document
    sealer.raise_seal = False
    held = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(held, entry_id) == ("uncertain", "external-outcome-uncertain")
    assert len(calls) == 1
    retried = _derive(fixture, pipeline, entry_id, sealer, retry_uncertain=True)
    assert _outcome(retried, entry_id) == ("searchable", None) and len(calls) == 2


def test_a_direct_edit_is_indexed_from_every_mirror_byte_whatever_the_store_records(lane):
    """Failure mode: under a stage authority a direct edit's indexed bytes depend on a
    recorded rendering in the store (which a store write could alter), so a store write
    decides which of the user's bytes are dropped from the index."""
    fixture, pipeline, calls, embedder, entry_id = lane
    sealer = _Sealer()
    _derive(fixture, pipeline, entry_id, sealer)
    shelf = fixture.bound._shelf
    path = shelf.mirror_path(entry_id)
    path.write_text(path.read_text().replace("Independent body row.", "Edited by the user."))
    document = _document(fixture, entry_id)
    document["mirror"]["markdown"] = "# Forged\n"
    _store(fixture, entry_id, document)
    edited = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(edited, entry_id) == ("searchable", None)
    stage = _accepted_stage(fixture, entry_id)
    assert stage["entry"]["projection"] == "appended" and stage["entry"]["checkpoint"] is None
    hits = fixture.bound.search("Edited by the user", embedder=embedder, reranker=OverlapReranker())
    assert any("Edited by the user." in hit.chunk for hit in hits.hits)
    assert len(calls) == 1


def test_an_explicit_reembedding_over_an_unsealed_checkpoint_indexes_every_mirror_byte(lane):
    """Security proof: an entry accepted without a stage authority has an unsealed
    checkpoint; a later caller-requested re-embedding under one never uses it. Every
    mirror byte is indexed (``appended``) and the stage names no checkpoint. Failure
    mode: the unsealed rendering decides the indexed bytes of a stage the caller signs,
    or is sealed as if this call produced it."""
    fixture, pipeline, calls, embedder, entry_id = lane
    assert _outcome(_derive(fixture, pipeline, entry_id, None), entry_id) == ("searchable", None)
    assert _accepted_stage(fixture, entry_id)["entry"]["projection"] == "appended"
    from biblios.core import BringCurrentRequest, bring_current

    sealer = _Sealer()
    result = bring_current(
        fixture.bound,
        BringCurrentRequest(identifier(), (entry_id,), reembed=(entry_id,)),
        pipeline=pipeline,
        authority=sealer,
    )
    assert _outcome(result, entry_id) == ("searchable", None)
    stage = _accepted_stage(fixture, entry_id)
    assert stage["entry"]["projection"] == "appended" and stage["entry"]["checkpoint"] is None
    assert sealer.sealed == [] and sealer.verified == [] and len(calls) == 1


@pytest.mark.parametrize("case", ("signed", "tampered", "unsigned"))
def test_a_donor_body_is_reused_only_from_the_checkpoint_its_signed_stage_names(tmp_path, case):
    """Failure mode: a nominated donor's converted body is taken from a checkpoint its
    caller-signed accepted stage does not name (changed in the store, or never sealed),
    or the destination silently converts instead, so the destination's mirror is a body
    no caller sealed, presented as the donor's."""
    fixture = consumer_fixture(tmp_path)
    calls: list = []
    try:
        pipeline = lane_pipeline(fixture, calls, reuse_converted_body=True)
        sealer = _Sealer()
        source = fixture.bound.curate(
            fixture.request("one.txt", b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        _derive(fixture, pipeline, source, None if case == "unsigned" else sealer)
        donor = fixture.bound.donor(source)
        signed = _accepted_stage(fixture, source)["entry"]["checkpoint"]
        if case == "tampered":
            document = _document(fixture, source)
            document["conversion"]["body"] = "Lighthouse lens sabotage."
            _store(fixture, source, document)
        destination = fixture.bound.curate(
            fixture.request("two.txt", b"Lighthouse lens maintenance.")
        ).mutation.affected_entry_ids[0]
        sealed = len(sealer.sealed)
        result = fixture.bound.derive(
            pipeline, destination, operation_id=identifier(), donor=donor, authority=sealer
        )
        if case == "signed":
            assert _outcome(result, destination) == ("searchable", None)
            stage = _accepted_stage(fixture, destination)
            assert stage["donor"]["checkpoint_sha256"] == signed["sha256"]
            # The destination's own checkpoint is sealed fresh and names the donor's.
            [converted, complete] = sealer.sealed[sealed:]
            assert json.loads(converted.canonical)["donor_checkpoint_sha256"] == signed["sha256"]
            assert stage["entry"]["checkpoint"] == {"sha256": complete.sha256, "reused": False}
        else:
            assert _outcome(result, destination) == ("pending", "donor-checkpoint-unproven")
            assert len(sealer.sealed) == sealed
        assert len(calls) == 1  # never converted again, either way
    finally:
        fixture.close()


def test_a_rename_before_acceptance_neither_renders_nor_seals_again(lane):
    """Failure mode: a rename before acceptance re-runs the converter or renderer, seals
    new content as though this call produced it, or leaves the old name in the index."""
    from biblios.curation import CurationRequest, RelocateEntry

    fixture, pipeline, calls, embedder, entry_id = lane
    sealer = _Sealer()
    sealer.refuse_stage = True
    _derive(fixture, pipeline, entry_id, sealer)
    sealed = list(sealer.sealed)
    fixture.bound.curate(
        CurationRequest(
            identifier(),
            fixture.bound.view.revision,
            RelocateEntry(entry_id, fixture.root_id, "renamed.txt"),
        )
    )
    sealer.refuse_stage = False
    result = _derive(fixture, pipeline, entry_id, sealer)
    assert _outcome(result, entry_id) == ("searchable", None)
    assert len(calls) == 1 and sealer.sealed == sealed
    found = fixture.bound.search("renamed.txt", embedder=embedder, reranker=OverlapReranker())
    assert entry_id in {hit.entry_id for hit in found.hits}


class _Binding(_Sealer):
    """A caller that keeps every conversion it sealed or verified, by checkpoint digest,
    and seals a rendering only when the conversion it renders is exactly the one its
    ``conversion_checkpoint_sha256`` names."""

    def __init__(self) -> None:
        super().__init__()
        self.conversions: dict[str, dict] = {}
        self.refused: list[str] = []

    def _remember(self, checkpoint) -> None:
        document = json.loads(checkpoint.canonical)
        if checkpoint.state == "converted":
            self.conversions[checkpoint.sha256] = document["conversion"]

    def seal_checkpoint(self, checkpoint):
        if checkpoint.state == "complete":
            document = json.loads(checkpoint.canonical)
            named = self.conversions.get(document["conversion_checkpoint_sha256"])
            if named is None or named != document["conversion"]:
                self.refused.append(checkpoint.sha256)
                raise StageRefused("conversion-not-bound")
        sealed = super().seal_checkpoint(checkpoint)
        self._remember(checkpoint)
        return sealed

    def verify_checkpoint(self, checkpoint, seal):
        accepted = super().verify_checkpoint(checkpoint, seal)
        if accepted:
            self._remember(checkpoint)
        return accepted


@pytest.mark.parametrize("substituted", (False, True), ids=("rerender", "substituted"))
def test_a_fresh_rendering_names_the_verified_conversion_it_renders(lane, monkeypatch, substituted):
    """Failure mode: after a verified conversion, the seal request for a fresh rendering
    carries other conversion content (a substitution between verification and seal) and
    nothing lets the caller tell, so it seals a rendering of a conversion it never
    sealed; or a genuine re-rendering cannot be bound to its conversion. The rendering
    is fresh because the first call's renderer failed after its conversion was sealed."""
    from biblios import core

    fixture, pipeline, calls, embedder, entry_id = lane
    caller = _Binding()
    renderer = pipeline.renderer

    def failing(result):
        raise RuntimeError("renderer unavailable")

    pipeline.renderer = failing
    first = _derive(fixture, pipeline, entry_id, caller)
    assert _outcome(first, entry_id)[0] != "searchable"
    pipeline.renderer = renderer
    [converted] = caller.sealed
    if substituted:
        real = core._Run._checkpoint_identity

        def substituting(self, entry, document, generation):
            if "mirror" in document:
                document = {
                    **document,
                    "conversion": {**document["conversion"], "body": "Substituted body."},
                }
            return real(self, entry, document, generation)

        monkeypatch.setattr(core._Run, "_checkpoint_identity", substituting)
    result = _derive(fixture, pipeline, entry_id, caller, retry_uncertain=True)
    [rendered] = [json.loads(item.canonical) for item in caller.sealed[1:]] or [None]
    if substituted:
        assert _outcome(result, entry_id) == ("pending", "checkpoint-seal-refused")
        assert len(caller.refused) == 1 and rendered is None
        assert caller.authorized == []
    else:
        assert _outcome(result, entry_id) == ("searchable", None)
        assert rendered["conversion_checkpoint_sha256"] == converted.sha256
        assert caller.refused == []
    assert len(calls) == 1
