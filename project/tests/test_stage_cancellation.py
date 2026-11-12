"""Caller-signed cancellation of a serving stage: each B4 window, restart from the
persisted bytes without a new signature, replay only on the caller's completed
recognition, conflicts and equivocation with no effect, and no restoration."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from pathlib import Path

import palace.index
import pytest

import biblios.core as core
from biblios import (
    StageCancellation,
    cancel_stage,
    discard_stage,
    pending_stages,
    stage_status,
)
from biblios.consumer import BoundShelf
from biblios.control import ConsumerError
from biblios.core import BringCurrentRequest, GenerationIdentity, StageRefused, bring_current
from biblios.derived import DERIVED_NAME, VOLATILE_NAME, DerivedStore
from biblios.mirrors import digest
from biblios.pipeline import ConsumerPipeline
from biblios.progress import indexed_files
from biblios.representation import default_policy, drawbridge_converter, envelope_renderer
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import make_shelf


class _Signer:
    """A caller that signs every decision with its own key and verifies on reread."""

    def __init__(self) -> None:
        self.key = secrets.token_bytes(32)
        self.head: set[str] = set()
        self.completed: set[str] = set()
        """Cancellation operation ids the caller's own records recognise as completed."""
        self.cancel_signed: list[str] = []
        self.cancel_rechecked: list[tuple[str, object]] = []
        self.refuse_cancellation = False

    def _sign(self, *parts: str) -> bytes:
        return hmac.new(self.key, "|".join(parts).encode(), hashlib.sha256).digest()

    def authorize_creation(self, creation, entry_id):
        return b"ok"

    def authorize(self, stage):
        return self._sign("authorize", stage.stage_sha256)

    def promote(self, stage, verified):
        return self._sign("promote", stage.stage_sha256)

    def seal_checkpoint(self, checkpoint):
        return self._sign("checkpoint", checkpoint.sha256)

    def verify_checkpoint(self, checkpoint, seal):
        return hmac.compare_digest(seal, self._sign("checkpoint", checkpoint.sha256))

    def recheck(self, stage, verified, authorization, promotion):
        if not hmac.compare_digest(authorization, self._sign("authorize", stage.stage_sha256)):
            raise StageRefused("evidence-not-ours")
        return "accepted" if stage.stage_sha256 in self.head else "pending"

    def authorize_cancellation(self, stage, request):
        if self.refuse_cancellation:
            raise StageRefused("cancel-held")
        assert request.stage_sha256 == stage.stage_sha256
        self.cancel_signed.append(request.operation_id)
        return self._sign("cancel", hashlib.sha256(request.canonical).hexdigest())

    def recheck_cancellation(self, stage, request, evidence, observed):
        expected = self._sign("cancel", hashlib.sha256(request.canonical).hexdigest())
        if not hmac.compare_digest(evidence, expected):
            raise StageRefused("evidence-not-ours")
        self.cancel_rechecked.append((request.operation_id, observed))
        return "completed" if request.operation_id in self.completed else "pending"


@pytest.fixture
def shelf(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("notes")
        yield shelf, settings


def _pipeline(settings, embedder):
    return ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
    )


def _hits(shelf, embedder, query) -> set[str]:
    found = shelf.search(query, embedder=embedder, reranker=OverlapReranker())
    return {hit.entry_id for hit in found.hits}


def _accepted(shelf, settings, embedder):
    """One entry accepted without an authority: the predecessor of the cancelled stage."""
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse keepers\n").affected_entry_ids[0]
    assert shelf.reconcile(settings, embedder=embedder).outcome(entry_id).outcome == "searchable"
    return entry_id


def _windows(monkeypatch, window):
    """Interrupt B4 of the next stage at one window."""

    def fail(*args, **kwargs):
        raise RuntimeError("interrupted")

    if window == "W1":  # marked serving, nothing else written
        real = core._Run._served_seq
        monkeypatch.setattr(
            core._Run,
            "_served_seq",
            lambda self, entry_id, *, odd: fail() if odd else real(self, entry_id, odd=odd),
        )
    elif window == "W2":  # served_seq odd, mirror not yet written
        monkeypatch.setattr(core, "write_mirror", fail)
    elif window == "W3":  # mirror written, Palace commit not landed
        monkeypatch.setattr(palace.index, "commit_plan", fail)
    else:  # W4: Palace committed, B5 not recorded
        monkeypatch.setattr(core._Run, "_accept", fail)


def _interrupted_update(shelf, settings, embedder, signer, monkeypatch, window):
    entry_id = _accepted(shelf, settings, embedder)
    bound = BoundShelf(shelf.local_binding(), shelf)
    path = shelf.mirror_path(entry_id)
    before = path.read_bytes()
    record = DerivedStore(shelf.palace_store, read_only=True).record(entry_id)
    edited = before.decode() + "\ncoral reefs\n"
    with monkeypatch.context() as patch:
        _windows(patch, window)
        out = bound.update_mirror(
            _pipeline(settings, embedder),
            entry_id,
            operation_id=str(uuid.uuid4()),
            markdown=edited,
            expected_sha256=digest(before),
            authority=signer,
        )
    assert out.outcome(entry_id).code == "publication-failed"
    (pending,) = pending_stages(bound).stages
    assert pending.serving and pending.state == "serving"
    return bound, entry_id, before, edited.encode(), record, pending


def _generation(bound, **changes):
    import dataclasses

    from biblios.binding import validate_handle

    view = validate_handle(bound.binding, bound._shelf._handle)
    return dataclasses.replace(
        GenerationIdentity(
            view.namespace_id, view.store_id, view.lineage_id, view.database_generation, view.epoch
        ),
        **changes,
    )


def _request(bound, entry_id, pending, record, **changes):
    fields = dict(
        operation_id=str(uuid.uuid4()),
        entry_id=entry_id,
        stage_sha256=pending.stage_sha256,
        predecessor_sha256=None if record is None else record.accepted_stage_sha256,
        stage_generation=_generation(bound),
        current_generation=_generation(bound),
        reason="caller-cleanup",
    )
    fields.update(changes)
    return StageCancellation(**fields)


@pytest.mark.parametrize("window", ("W1", "W2", "W3", "W4"))
def test_each_serving_window_is_cancelled_undoing_only_what_b4_wrote(shelf, monkeypatch, window):
    """Failure mode: a serving stage of an abandoned update has no disposition (finish is
    stale, discard is refused), or cancelling it moves bytes B4 never wrote, deletes user
    bytes, restores or re-serves anything implicitly, embeds, or leaves the cancelled
    rows served; or the receipt drops the exact signed bytes."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, before, edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, window
    )
    path = shelf.mirror_path(entry_id)
    index_before = indexed_files(shelf).get(entry_id)
    request = _request(bound, entry_id, pending, record)
    calls = len(embedder.calls)
    result = cancel_stage(bound, request, authority=signer)
    assert (result.state, result.stage_sha256) == ("cancelled", pending.stage_sha256)
    assert signer.cancel_signed == [request.operation_id] and len(embedder.calls) == calls
    assert pending_stages(bound).stages == ()
    # Its digest is now answered from the receipt, as history only.
    cancelled = stage_status(bound, pending.stage_sha256)
    assert (cancelled.state, cancelled.entry_id) == ("cancelled", entry_id)
    after = DerivedStore(shelf.palace_store, read_only=True).record(entry_id)
    assert after.accepted_stage_sha256 == record.accepted_stage_sha256
    assert after.served_seq % 2 == 0
    if window in ("W1", "W2"):
        # Nothing moved: the predecessor is served again, exactly.
        assert result.retained == () and path.read_bytes() == before
        assert indexed_files(shelf).get(entry_id) == index_before == record.index_digest
        assert entry_id in _hits(shelf, embedder, "lighthouse")
    else:
        # The mirror B4 wrote is retained, never deleted; the predecessor's displaced
        # bytes stay retained too and nothing is restored, so reads withhold the entry.
        assert not path.exists() and len(result.retained) == 1
        assert (path.parent / result.retained[0]).read_bytes() == edited
        assert entry_id not in _hits(shelf, embedder, "lighthouse")
        assert entry_id not in _hits(shelf, embedder, "coral")
        expected = None if window == "W4" else record.index_digest
        assert indexed_files(shelf).get(entry_id) == expected
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        (stored,) = conn.execute(
            "SELECT request_sha256, request_canonical, stage_sha256 FROM cancellations"
        ).fetchall()
    assert stored == (request.sha256, request.canonical, pending.stage_sha256)
    # An ordinary call afterwards finishes nothing and embeds nothing for the cancelled
    # stage: the user's later decision decides what comes next.
    assert pending.stage_sha256 not in {
        s.stage_sha256
        for s in bring_current(
            bound,
            BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
            pipeline=_pipeline(settings, embedder),
            authority=signer,
        ).accepted
    }


def test_replay_needs_the_callers_completed_recognition_and_conflicts_have_no_effect(
    shelf, monkeypatch
):
    """Failure mode: a repeated request re-runs effects or is answered from Biblios's own
    receipt; a different request for the same stage (equivocation), or a changed
    request under the same operation id, is honoured."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, _edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W2"
    )
    request = _request(bound, entry_id, pending, record)
    cancel_stage(bound, request, authority=signer)
    with pytest.raises(ConsumerError, match="cancellation-unverified"):
        cancel_stage(bound, request, authority=signer)  # the caller has not recorded it
    signer.completed.add(request.operation_id)
    replayed = cancel_stage(bound, request, authority=signer)
    assert (replayed.state, replayed.stage_sha256) == ("completed-replay", pending.stage_sha256)
    assert signer.cancel_signed == [request.operation_id]  # never signed again
    with pytest.raises(ConsumerError, match="cancellation-conflict"):
        cancel_stage(bound, _request(bound, entry_id, pending, record), authority=signer)
    with pytest.raises(ConsumerError, match="cancellation-conflict"):
        changed = _request(
            bound, entry_id, pending, record, operation_id=request.operation_id, reason="other"
        )
        cancel_stage(bound, changed, authority=signer)


def test_a_cancellation_interrupted_after_its_signature_resumes_without_a_new_one(
    shelf, monkeypatch
):
    """Failure mode: a restart asks the caller to sign again over mutable rows, finishes
    on tampered persisted bytes, lets ordinary work or a discard touch the stage
    meanwhile, or lets a second request take it over."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, _edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    request = _request(bound, entry_id, pending, record)
    with monkeypatch.context() as patch:
        patch.setattr(core._Cancellation, "_undo", lambda *a: (_ for _ in ()).throw(OSError()))
        with pytest.raises(OSError):
            cancel_stage(bound, request, authority=signer)
    (cancelling,) = pending_stages(bound).stages
    assert cancelling.state == "cancelling" and cancelling.serving
    with pytest.raises(ConsumerError, match="stage-serving"):
        discard_stage(bound, entry_id, pending.stage_sha256)
    ordinary = bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=_pipeline(settings, embedder),
        authority=signer,
    )
    assert ordinary.outcome(entry_id).code == "stage-cancelling" and ordinary.accepted == ()
    with pytest.raises(ConsumerError, match="cancellation-conflict"):
        cancel_stage(bound, _request(bound, entry_id, pending, record), authority=signer)
    # Tampered persisted bytes are refused before any effect.
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME, isolation_level=None)
    (saved,) = conn.execute("SELECT request_canonical FROM cancellation_chain").fetchone()
    conn.execute("UPDATE cancellation_chain SET request_canonical=?", (bytes(saved) + b" ",))
    with pytest.raises(ConsumerError, match="cancellation-tampered"):
        cancel_stage(bound, request, authority=signer)
    conn.execute("UPDATE cancellation_chain SET request_canonical=?", (saved,))
    # An interruption between the receipt and the stage drop also resumes.
    real = core._drop_stage
    with monkeypatch.context() as patch:
        patch.setattr(core, "_drop_stage", lambda *a: (_ for _ in ()).throw(OSError()))
        with pytest.raises(OSError):
            cancel_stage(bound, request, authority=signer)
    assert core._drop_stage is real
    done = cancel_stage(bound, request, authority=signer)
    conn.close()
    assert done.state == "cancelled" and len(done.retained) == 1
    assert signer.cancel_signed == [request.operation_id]  # one signature, ever
    assert [op for op, _ in signer.cancel_rechecked] == [request.operation_id] * 2
    assert pending_stages(bound).stages == ()


def test_unserved_unbound_or_refused_cancellations_have_no_effect(shelf, monkeypatch):
    """Failure mode: cancellation reaches an unserved stage (discard's job), runs without
    an authority, honours an obsolete predecessor or epoch, or moves anything when the
    caller refuses."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, before, _edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    path = shelf.mirror_path(entry_id)
    written = path.read_bytes()
    with pytest.raises(ConsumerError, match="stage-authority-required"):
        cancel_stage(bound, _request(bound, entry_id, pending, record), authority=None)
    for changes in (
        {"predecessor_sha256": "0" * 64},
        {"stage_generation": _generation(bound, epoch="other-epoch")},
        {"current_generation": _generation(bound, epoch="other-epoch")},
        {"current_generation": _generation(bound, store_id="other-store")},
        {"current_generation": _generation(bound, lineage_id="other-lineage")},
    ):
        with pytest.raises(ConsumerError, match="cancellation-conflict"):
            cancel_stage(
                bound, _request(bound, entry_id, pending, record, **changes), authority=signer
            )
    signer.refuse_cancellation = True
    with pytest.raises(StageRefused, match="cancel-held"):
        cancel_stage(bound, _request(bound, entry_id, pending, record), authority=signer)
    assert pending_stages(bound).stages == (pending,) and path.read_bytes() == written
    assert signer.cancel_signed == []

    # An unserved stage is the caller's discard, never a cancellation.
    other = shelf.add("notes/b.md", b"# B\n\nharbour\n").affected_entry_ids[0]
    holding = _Signer()
    holding.promote = lambda stage, verified: (_ for _ in ()).throw(StageRefused("held"))
    bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (other,)),
        pipeline=_pipeline(settings, embedder),
        authority=holding,
    )
    (unserved,) = [s for s in pending_stages(bound).stages if s.entry_id == other]
    assert not unserved.serving
    with pytest.raises(ConsumerError, match="stage-not-serving"):
        cancel_stage(
            bound,
            _request(bound, other, unserved, record, predecessor_sha256=None),
            authority=signer,
        )
    assert before != written


def test_identical_indexed_bytes_keep_palace_rows_only_when_they_are_the_predecessors(
    shelf, monkeypatch
):
    """Failure mode: a stage that indexes exactly the predecessor's bytes but commits
    other vectors is cancelled with its vectors left in place and served as the
    predecessor's, because the file digest alone looked unchanged."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse keepers\n").affected_entry_ids[0]
    bound = BoundShelf(shelf.local_binding(), shelf)
    # Accepted by the same caller, so the recorded rendering is its sealed checkpoint and
    # the re-embedding indexes exactly the predecessor's bytes.
    first = bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=_pipeline(settings, embedder),
        authority=signer,
    )
    assert first.outcome(entry_id).outcome == "searchable"
    signer.head.update(stage.stage_sha256 for stage in first.accepted)
    record = DerivedStore(shelf.palace_store, read_only=True).record(entry_id)

    class Shifted(HashEmbedder):
        def embed(self, texts):
            return [[value * -1.0 for value in vector] for vector in super().embed(texts)]

    for shifted in (False, True):
        with monkeypatch.context() as patch:
            _windows(patch, "W4")
            out = bring_current(
                bound,
                BringCurrentRequest(str(uuid.uuid4()), (entry_id,), reembed=(entry_id,)),
                pipeline=_pipeline(settings, Shifted() if shifted else embedder),
                authority=signer,
            )
        assert out.outcome(entry_id).code == "publication-failed"
        (pending,) = pending_stages(bound).stages
        assert indexed_files(shelf).get(entry_id) == record.index_digest
        if shifted:
            # The mutable accepted rows forged to match the committed vectors are no
            # evidence: only the signed predecessor stage is.
            with sqlite3.connect(shelf.palace_store / VOLATILE_NAME) as volatile:
                staged = volatile.execute(
                    "SELECT chunk_id, vector FROM stage_rows WHERE entry_id=?", (entry_id,)
                ).fetchall()
            with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as derived:
                derived.executemany(
                    "UPDATE accepted_chunks SET vector_sha256=? WHERE entry_id=? AND chunk_id=?",
                    [(hashlib.sha256(v).hexdigest(), entry_id, c) for c, v in staged],
                )
        cancel_stage(bound, _request(bound, entry_id, pending, record), authority=signer)
        if shifted:
            # Other vectors behind the same digest: deleted, and the entry withheld.
            assert indexed_files(shelf).get(entry_id) is None
            assert entry_id not in _hits(shelf, embedder, "lighthouse")
        else:
            # Exactly the predecessor's accepted rows and vectors: left, and served.
            assert indexed_files(shelf).get(entry_id) == record.index_digest
            assert entry_id in _hits(shelf, embedder, "lighthouse")


def test_a_forged_resume_document_never_steers_a_restart_or_a_cancellation(shelf, monkeypatch):
    """Failure mode: an unsigned resume row claiming an exchange makes a cancellation
    retire a first mirror that B4 never wrote (materialized before staging, and the
    user's once the stage is gone), or makes a restart exchange over it."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    bound = BoundShelf(shelf.local_binding(), shelf)
    with monkeypatch.context() as patch:
        _windows(patch, "W3")
        out = bring_current(
            bound,
            BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
            pipeline=_pipeline(settings, embedder),
            authority=signer,
        )
    assert out.outcome(entry_id).code == "publication-failed"
    (pending,) = pending_stages(bound).stages
    path = shelf.mirror_path(entry_id)
    first = path.read_bytes()
    with sqlite3.connect(shelf.palace_store / VOLATILE_NAME) as conn:
        (document,) = conn.execute("SELECT document FROM stage_resume").fetchone()
        forged = json.loads(document)
        forged["expected_exchange"] = digest(first)
        conn.execute("UPDATE stage_resume SET document=?", (json.dumps(forged, sort_keys=True),))
    request = _request(bound, entry_id, pending, None, predecessor_sha256=None)
    with pytest.raises(ConsumerError, match="stage-tampered"):
        cancel_stage(bound, request, authority=signer)
    assert signer.cancel_signed == [] and pending_stages(bound).stages == (pending,)
    resumed = bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=_pipeline(settings, embedder),
        authority=signer,
    )
    assert resumed.outcome(entry_id).code == "stage-tampered" and resumed.accepted == ()
    assert path.read_bytes() == first


def _crash_once(patch, target, name):
    """Make ``target.name`` run once and then raise, simulating a crash after it."""
    real = getattr(target, name)

    def crashing(*args, **kwargs):
        real(*args, **kwargs)
        patch.setattr(target, name, real)
        raise OSError("crash")

    patch.setattr(target, name, crashing)


def test_a_crash_after_retirement_or_after_the_receipt_loses_no_name_and_no_answer(
    shelf, monkeypatch
):
    """Failure mode: a crash right after the mirror was retired leaves the retained file
    out of the result; after the receipt, a caller that already recognises completion
    is refused or the effects run again."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    request = _request(bound, entry_id, pending, record)
    with monkeypatch.context() as patch:
        _crash_once(patch, core, "retire_mirror")
        with pytest.raises(OSError):
            cancel_stage(bound, request, authority=signer)
    with monkeypatch.context() as patch:
        _crash_once(patch, core, "_drop_stage")
        with pytest.raises(OSError):
            cancel_stage(bound, request, authority=signer)
    (left,) = pending_stages(bound).stages
    assert left.state == "cancelling"
    signer.completed.add(request.operation_id)
    retirements = []
    monkeypatch.setattr(core, "retire_mirror", lambda *a, **k: retirements.append(1))
    done = cancel_stage(bound, request, authority=signer)
    assert done.state == "completed-replay" and retirements == []
    assert pending_stages(bound).stages == () and len(done.retained) == 1
    assert (shelf.mirror_path(entry_id).parent / done.retained[0]).read_bytes() == edited


def test_only_the_grant_the_stage_consumed_is_dropped(shelf, monkeypatch):
    """Failure mode: a cancellation deletes another grant row for the same entry and
    source (a later curation's, or a removal's) by matching the source alone."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, _edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W2"
    )
    with sqlite3.connect(shelf.palace_store / VOLATILE_NAME) as conn:
        conn.execute(
            "INSERT INTO grants VALUES (?, ?, 'created', ?, NULL, NULL, NULL, 'granted', NULL)",
            (entry_id, record.source_cas_id, str(uuid.uuid4())),
        )
    cancel_stage(bound, _request(bound, entry_id, pending, record), authority=signer)
    with sqlite3.connect(shelf.palace_store / VOLATILE_NAME) as conn:
        kept = conn.execute("SELECT COUNT(*) FROM grants WHERE entry_id=?", (entry_id,))
        assert kept.fetchone()[0] == 1


def test_a_changed_epoch_or_predecessor_after_the_signature_moves_nothing(shelf, monkeypatch):
    """Failure mode: an interrupted cancellation continues its effects after the shelf's
    epoch changed or the entry's accepted stage was changed out of band, or such a
    change is answered by discarding the stage."""
    import dataclasses

    import biblios.binding as binding_module

    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    request = _request(bound, entry_id, pending, record)
    with monkeypatch.context() as patch:
        patch.setattr(core._Cancellation, "_undo", lambda *a: (_ for _ in ()).throw(OSError()))
        with pytest.raises(OSError):
            cancel_stage(bound, request, authority=signer)
    path = shelf.mirror_path(entry_id)
    real = binding_module.validate_handle
    with monkeypatch.context() as patch:
        patch.setattr(
            binding_module,
            "validate_handle",
            lambda b, h: dataclasses.replace(real(b, h), epoch="next-epoch"),
        )
        with pytest.raises(ConsumerError, match="stage-fenced") as fenced:
            cancel_stage(bound, request, authority=signer)
    assert fenced.value.effect == "recovery-required"
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        conn.execute(
            "UPDATE entries SET accepted_stage_sha256=? WHERE entry_id=?", ("f" * 64, entry_id)
        )
    with pytest.raises(ConsumerError, match="cancellation-conflict") as moved:
        cancel_stage(bound, request, authority=signer)
    assert moved.value.effect == "recovery-required"
    assert signer.cancel_rechecked == [] and path.read_bytes() == edited
    assert [s.state for s in pending_stages(bound).stages] == ["cancelling"]
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        conn.execute(
            "UPDATE entries SET accepted_stage_sha256=? WHERE entry_id=?",
            (record.accepted_stage_sha256, entry_id),
        )
    assert cancel_stage(bound, request, authority=signer).state == "cancelled"


def test_a_crash_between_retirement_and_its_record_is_found_and_recorded(shelf, monkeypatch):
    """Failure mode: a crash after the mirror moved but before its provenance record
    leaves those bytes unlisted in the result and unattributed, or a retry moves or
    deletes anything else to find them."""
    import biblios.mirrors as mirrors
    from biblios.mirrors import retention_record

    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    request = _request(bound, entry_id, pending, record)
    real = mirrors._record_retention

    def crash(*args, **kwargs):
        monkeypatch.setattr(mirrors, "_record_retention", real)
        raise OSError("crash after rename")

    monkeypatch.setattr(mirrors, "_record_retention", crash)
    with pytest.raises(OSError):
        cancel_stage(bound, request, authority=signer)
    before_retry = mirrors.retained_files(shelf.mirror_root, entry_id)
    (moved,) = [f for f in before_retry if "cancelled-" + request.stage_sha256 in f.name]
    assert moved.read_bytes() == edited and retention_record(shelf.palace_store, moved) is None
    done = cancel_stage(bound, request, authority=signer)
    assert done.retained == (moved.name,)
    written = retention_record(shelf.palace_store, moved)
    assert (written["reason"], written["operation_id"]) == ("cancelled", request.operation_id)
    assert mirrors.retained_files(shelf.mirror_root, entry_id) == before_retry


def _next_generation(monkeypatch, **changes):
    """From now on the shelf reports a changed generation (epoch and/or database)."""
    import dataclasses

    import biblios.binding as binding_module

    real = binding_module.validate_handle
    monkeypatch.setattr(
        binding_module,
        "validate_handle",
        lambda b, h: dataclasses.replace(real(b, h), **changes),
    )


def test_a_stage_fenced_by_a_changed_epoch_is_cancelled_only_by_an_explicit_request(
    shelf, monkeypatch
):
    """Failure mode: a serving stage whose shelf epoch changed has no disposition at all,
    or is disposed of by inference from the mismatch (a request naming only one
    generation, or a guessed one)."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    old = _generation(bound)
    _next_generation(monkeypatch, epoch="next-epoch", database_generation="next-db")
    new = _generation(bound)
    ordinary = bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=_pipeline(settings, embedder),
        authority=signer,
    )
    assert ordinary.outcome(entry_id).code == "stage-fenced"
    with pytest.raises(ConsumerError, match="stage-serving"):
        discard_stage(bound, entry_id, pending.stage_sha256)
    for stage_generation, current_generation in ((new, new), (old, old)):
        with pytest.raises(ConsumerError, match="cancellation-conflict"):
            cancel_stage(
                bound,
                _request(
                    bound,
                    entry_id,
                    pending,
                    record,
                    stage_generation=stage_generation,
                    current_generation=current_generation,
                ),
                authority=signer,
            )
    assert signer.cancel_signed == []
    with monkeypatch.context() as patch:
        # Another store or lineage is never this stage's successor, whatever is signed.
        for change in ({"store_id": "other-store"}, {"lineage_id": "other-lineage"}):
            _next_generation(patch, **change)
            with pytest.raises(ConsumerError, match="cancellation-conflict"):
                cancel_stage(
                    bound,
                    _request(
                        bound,
                        entry_id,
                        pending,
                        record,
                        stage_generation=old,
                        current_generation=_generation(bound),
                    ),
                    authority=signer,
                )
    assert signer.cancel_signed == []
    request = _request(
        bound, entry_id, pending, record, stage_generation=old, current_generation=new
    )
    done = cancel_stage(bound, request, authority=signer)
    assert done.state == "cancelled" and signer.cancel_signed == [request.operation_id]
    assert (shelf.mirror_path(entry_id).parent / done.retained[0]).read_bytes() == edited
    assert pending_stages(bound).stages == ()


def test_supersession_is_one_linear_chain_that_keeps_every_link_and_moves_bytes_once(
    shelf, monkeypatch
):
    """Failure mode: a request whose generation the shelf no longer has blocks the stage
    forever, a successor discards the earlier signed request, two successors fork the
    chain, a superseded request acts again, or the bytes an earlier request retired are
    lost from the result or moved twice."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    retirements = []
    real_retire = core.retire_mirror
    monkeypatch.setattr(
        core, "retire_mirror", lambda *a, **k: retirements.append(1) or real_retire(*a, **k)
    )
    old = _generation(bound)

    def interrupted(request):
        with monkeypatch.context() as patch:
            # Crash after C3's retirement, before the receipt.
            patch.setattr(
                core._Cancellation, "_finish", lambda self, *a: (_ for _ in ()).throw(OSError())
            )
            with pytest.raises(OSError):
                cancel_stage(bound, request, authority=signer)

    first = _request(bound, entry_id, pending, record)
    with monkeypatch.context() as patch:
        _crash_once(patch, core._Cancellation, "_undo")
        with pytest.raises(OSError):
            cancel_stage(bound, first, authority=signer)
    # The tip can still continue under its own generation: a second request is a fork.
    with pytest.raises(ConsumerError, match="cancellation-conflict"):
        cancel_stage(
            bound,
            _request(bound, entry_id, pending, record, supersedes_sha256=first.sha256),
            authority=signer,
        )
    _next_generation(monkeypatch, epoch="e2")
    with pytest.raises(ConsumerError, match="stage-fenced"):
        cancel_stage(bound, first, authority=signer)
    second = _request(
        bound,
        entry_id,
        pending,
        record,
        stage_generation=old,
        current_generation=_generation(bound),
        supersedes_sha256=first.sha256,
    )
    interrupted(second)
    with pytest.raises(ConsumerError, match="cancellation-superseded"):
        cancel_stage(bound, first, authority=signer)
    # A link whose position no longer follows its predecessor is refused before use.
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        conn.execute("UPDATE cancellation_chain SET position=7 WHERE position=1")
    with pytest.raises(ConsumerError, match="cancellation-tampered"):
        cancel_stage(bound, second, authority=signer)
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        conn.execute("UPDATE cancellation_chain SET position=1 WHERE position=7")
    # A first link re-hashed coherently to claim it superseded something is refused.
    import dataclasses

    forged = dataclasses.replace(first, supersedes_sha256="a" * 64)
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        conn.execute(
            "UPDATE cancellation_chain SET request_sha256=?, request_canonical=? WHERE position=0",
            (forged.sha256, forged.canonical),
        )
    with pytest.raises(ConsumerError, match="cancellation-tampered"):
        cancel_stage(bound, second, authority=signer)
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        conn.execute(
            "UPDATE cancellation_chain SET request_sha256=?, request_canonical=? WHERE position=0",
            (first.sha256, first.canonical),
        )
    _next_generation(monkeypatch, epoch="e3", database_generation="db3")
    stale = _request(
        bound,
        entry_id,
        pending,
        record,
        stage_generation=old,
        current_generation=_generation(bound),
        supersedes_sha256=first.sha256,  # not the tip
    )
    with pytest.raises(ConsumerError, match="cancellation-conflict"):
        cancel_stage(bound, stale, authority=signer)
    third = _request(
        bound,
        entry_id,
        pending,
        record,
        stage_generation=old,
        current_generation=_generation(bound),
        supersedes_sha256=second.sha256,
    )
    done = cancel_stage(bound, third, authority=signer)
    assert done.state == "cancelled" and retirements == [1]
    assert (shelf.mirror_path(entry_id).parent / done.retained[0]).read_bytes() == edited
    for superseded in (first, second):
        with pytest.raises(ConsumerError, match="cancellation-superseded"):
            cancel_stage(bound, superseded, authority=signer)
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        chain = conn.execute(
            "SELECT position, request_sha256, request_canonical FROM cancellation_chain"
            " WHERE stage_sha256=? ORDER BY position",
            (pending.stage_sha256,),
        ).fetchall()
    assert chain == [
        (0, first.sha256, first.canonical),
        (1, second.sha256, second.canonical),
        (2, third.sha256, third.canonical),
    ]
    assert signer.cancel_signed == [first.operation_id, second.operation_id, third.operation_id]


def test_a_completed_cancellation_replays_after_its_successor_generation(shelf, monkeypatch):
    """Failure mode: a completed cancellation's repeat is refused by guards about the
    current generation or predecessor (which a later cutover legitimately changed)
    before the caller can recognise its own completion, or is answered without it."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, _edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W4"
    )
    request = _request(bound, entry_id, pending, record)
    cancel_stage(bound, request, authority=signer)
    _next_generation(monkeypatch, epoch="successor", database_generation="successor-db")
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        conn.execute(
            "UPDATE entries SET accepted_stage_sha256=? WHERE entry_id=?", ("e" * 64, entry_id)
        )
    with pytest.raises(ConsumerError, match="cancellation-unverified"):
        cancel_stage(bound, request, authority=signer)
    signer.completed.add(request.operation_id)
    assert cancel_stage(bound, request, authority=signer).state == "completed-replay"
    assert signer.cancel_signed == [request.operation_id]


def test_a_crash_between_the_link_and_the_flag_admits_no_ordinary_finish(shelf, monkeypatch):
    """Failure mode: after the caller's signature is persisted but before the stage is
    flagged, an ordinary call finishes, re-presents or discards the stage, or the retry
    asks the caller to sign again."""
    import palace.index

    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, _edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    request = _request(bound, entry_id, pending, record)
    with monkeypatch.context() as patch:
        patch.setattr(
            core._Cancellation, "_flag", lambda self, stage: (_ for _ in ()).throw(OSError())
        )
        with pytest.raises(OSError):
            cancel_stage(bound, request, authority=signer)
    (left,) = pending_stages(bound).stages
    assert left.state == "serving"
    commits = []
    real = palace.index.commit_plan
    monkeypatch.setattr(
        palace.index, "commit_plan", lambda **kwargs: commits.append(1) or real(**kwargs)
    )
    ordinary = bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=_pipeline(settings, embedder),
        authority=signer,
    )
    assert ordinary.outcome(entry_id).code == "stage-cancelling" and commits == []
    with pytest.raises(ConsumerError, match="stage-serving"):
        discard_stage(bound, entry_id, pending.stage_sha256)
    # Even with the mutable flags forged to look unserved, the signed link refuses a
    # discard.
    with sqlite3.connect(shelf.palace_store / VOLATILE_NAME) as conn:
        conn.execute("UPDATE stages SET state='promoted'")
    with sqlite3.connect(shelf.palace_store / DERIVED_NAME) as conn:
        (seq,) = conn.execute(
            "SELECT served_seq FROM entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        conn.execute("UPDATE entries SET served_seq=? WHERE entry_id=?", (seq + seq % 2, entry_id))
    with pytest.raises(ConsumerError, match="stage-serving"):
        discard_stage(bound, entry_id, pending.stage_sha256)
    with sqlite3.connect(shelf.palace_store / VOLATILE_NAME) as conn:
        conn.execute("UPDATE stages SET state='serving'")
    assert cancel_stage(bound, request, authority=signer).state == "cancelled"
    assert signer.cancel_signed == [request.operation_id]


def test_other_bytes_at_the_fixed_retained_name_are_left_untouched(shelf, monkeypatch):
    """Failure mode: after a crash, a file at the cancellation's fixed retained name that
    holds other bytes is listed, recorded as this cancellation's, replaced or moved."""
    import biblios.mirrors as mirrors

    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, _edited, record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    request = _request(bound, entry_id, pending, record)
    with monkeypatch.context() as patch:
        _crash_once(patch, core, "retire_mirror")
        with pytest.raises(OSError):
            cancel_stage(bound, request, authority=signer)
    (target,) = [
        f
        for f in mirrors.retained_files(shelf.mirror_root, entry_id)
        if "cancelled-" + pending.stage_sha256 in f.name
    ]
    target.write_bytes(b"someone else's bytes")
    record_path = mirrors.retention_directory(shelf.palace_store, entry_id) / (
        target.name + ".json"
    )
    recorded = record_path.read_bytes()
    with pytest.raises(ConsumerError, match="mirror-retention-conflict"):
        cancel_stage(bound, request, authority=signer)
    assert target.read_bytes() == b"someone else's bytes"
    assert record_path.read_bytes() == recorded
    assert [s.state for s in pending_stages(bound).stages] == ["cancelling"]


@pytest.mark.parametrize("window", ("W1", "W3"))
def test_a_mirror_that_already_held_the_update_is_never_retired_by_its_cancellation(
    shelf, monkeypatch, window
):
    """Failure mode: an update seam repeating bytes the user had already saved stages an
    exchange B4 never performs, and cancelling it moves the user's own file away."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    entry_id = _accepted(shelf, settings, embedder)
    bound = BoundShelf(shelf.local_binding(), shelf)
    path = shelf.mirror_path(entry_id)
    before = path.read_bytes()
    record = DerivedStore(shelf.palace_store, read_only=True).record(entry_id)
    edited = before.decode() + "\ncoral reefs\n"
    path.write_text(edited)  # the user's own save, before the update call repeats it
    with monkeypatch.context() as patch:
        _windows(patch, window)
        out = bound.update_mirror(
            _pipeline(settings, embedder),
            entry_id,
            operation_id=str(uuid.uuid4()),
            markdown=edited,
            expected_sha256=digest(before),
            authority=signer,
        )
    assert out.outcome(entry_id).code == "publication-failed"
    (pending,) = pending_stages(bound).stages
    result = cancel_stage(bound, _request(bound, entry_id, pending, record), authority=signer)
    assert result.state == "cancelled" and result.retained == ()
    assert path.read_text() == edited


def test_a_changed_database_generation_alone_fences_an_ordinary_resume(shelf, monkeypatch):
    """Failure mode: the ordinary resume fence compares only the epoch, so a stage from a
    replaced database generation under the same epoch is resumed and served."""
    shelf, settings = shelf
    embedder, signer = HashEmbedder(), _Signer()
    bound, entry_id, _before, _edited, _record, pending = _interrupted_update(
        shelf, settings, embedder, signer, monkeypatch, "W3"
    )
    _next_generation(monkeypatch, database_generation="next-db")
    ordinary = bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (entry_id,)),
        pipeline=_pipeline(settings, embedder),
        authority=signer,
    )
    assert ordinary.outcome(entry_id).code == "stage-fenced" and ordinary.accepted == ()
    assert [s.stage_sha256 for s in pending_stages(bound).stages] == [pending.stage_sha256]
