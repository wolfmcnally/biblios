"""An entry removed before any stage was accepted: the core's own exact first mirror is
retired only by an ordinary removal stage (no predecessor) that the caller authenticates;
anything else at the path stays the user's, and a refusal moves, persists and voids
nothing."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from biblios import pending_stages
from biblios.consumer import BoundShelf
from biblios.core import StageRefused
from biblios.derived import VOLATILE_NAME
from biblios.testing import HashEmbedder
from tests.support import make_shelf
from tests.test_core import _bring, _Holding


@pytest.fixture
def shelf(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("notes")
        yield shelf, settings


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Origin(_Holding):
    """A caller that keeps its own record of the first mirror digest each authorized
    first publication staged, and authorizes a removal before acceptance only against
    that record. ``refuse_publish`` withholds B1 for publications, so the first mirror is
    written and nothing is accepted."""

    def __init__(self) -> None:
        super().__init__()
        self.release = True
        self.refuse_publish = True
        self.origin: dict[str, str] = {}
        self.removals: list = []

    def authorize(self, stage):
        entry = stage.entry
        if entry.action == "publish":
            self.origin[entry.entry_id] = entry.mirror_sha256
            if self.refuse_publish:
                raise StageRefused("not-yet")
            return super().authorize(stage)
        self.removals.append(stage)
        if stage.predecessor_sha256 is None and (
            self.origin.get(entry.entry_id) != entry.prior_mirror_sha256
        ):
            raise StageRefused("origin-unproved")
        return super().authorize(stage)


def _unaccepted(shelf, settings, embedder, authority):
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    first = _bring(shelf, settings, embedder, entry_id, authority)
    assert first.outcome(entry_id).code == "not-yet" and first.accepted == ()
    mirror = shelf.mirror_path(entry_id)
    assert mirror.is_file()
    return entry_id, mirror, mirror.read_bytes()


def _retained(mirror: Path) -> list[Path]:
    return sorted(mirror.parent.glob(f"{mirror.name}.retained-*"))


def test_the_cores_own_first_mirror_is_retired_by_an_authenticated_removal_stage(shelf):
    """Failure mode: an entry removed before acceptance leaves the core's own first
    mirror at its live path forever, or retires it without the caller authenticating the
    removal's curation provenance, source and exact bytes, or calls a provider."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    authority = _Origin()
    entry_id, mirror, staged = _unaccepted(shelf, settings, embedder, authority)
    removal_op = shelf.remove("notes/a.md")
    calls = len(embedder.calls)
    done = _bring(shelf, settings, embedder, entry_id, authority)
    assert done.outcome(entry_id).outcome == "removed" and len(embedder.calls) == calls
    (stage,) = authority.removals
    assert stage.predecessor_sha256 is None
    assert (stage.entry.action, stage.entry.prior_mirror_sha256, stage.entry.mirror_sha256) == (
        "remove",
        _sha(staged),
        None,
    )
    assert stage.entry.source_cas_id is not None
    assert stage.creation is not None and stage.creation.role == "removed"
    assert stage.creation.cas_id == stage.entry.source_cas_id
    assert stage.creation.operation_id is not None
    del removal_op
    assert not mirror.exists()
    (kept,) = _retained(mirror)
    assert kept.read_bytes() == staged
    # Until the caller's signed head records it, the stage stays; acknowledged, it goes.
    bound = BoundShelf(shelf.local_binding(), shelf)
    assert [s.entry_id for s in pending_stages(bound).stages] == [entry_id]
    authority.head.add(stage.stage_sha256)
    assert _bring(shelf, settings, embedder, entry_id, authority).accepted == ()
    assert pending_stages(bound).stages == ()


def test_a_lost_removal_result_is_re_presented_without_another_effect(shelf):
    """Failure mode: a removal before acceptance whose result the caller never recorded
    is retired again, disputed, or silently cleared instead of re-presented exactly."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    authority = _Origin()
    entry_id, mirror, _staged = _unaccepted(shelf, settings, embedder, authority)
    shelf.remove("notes/a.md")
    done = _bring(shelf, settings, embedder, entry_id, authority)
    (stage,) = done.accepted
    retained = _retained(mirror)
    again = _bring(shelf, settings, embedder, entry_id, authority)
    assert again.outcome(entry_id).code == "acceptance-unacknowledged"
    assert [s.stage_sha256 for s in again.accepted] == [stage.stage_sha256]
    assert _retained(mirror) == retained and len(authority.removals) == 1


def test_bytes_that_are_not_the_cores_own_first_mirror_never_move(shelf):
    """Failure mode: a user's edit of a never-accepted mirror is retired as the core's
    own because the entry was removed."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    authority = _Origin()
    entry_id, mirror, _ = _unaccepted(shelf, settings, embedder, authority)
    mirror.write_bytes(b"# mine\n\nthe user's own words\n")
    shelf.remove("notes/a.md")
    out = _bring(shelf, settings, embedder, entry_id, authority)
    assert (out.outcome(entry_id).outcome, out.outcome(entry_id).code) == (
        "removed",
        "mirror-user-owned",
    )
    assert authority.removals == [] and _retained(mirror) == []
    assert mirror.read_bytes() == b"# mine\n\nthe user's own words\n"


def test_a_forged_first_mirror_record_moves_nothing_without_the_callers_origin(shelf):
    """Failure mode: a mutable grant forged to the digest of bytes now at the path is
    taken as the first mirror's origin, so the user's bytes are retired; or the refusal
    persists, voids or discards a stage."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    authority = _Origin()
    entry_id, mirror, _ = _unaccepted(shelf, settings, embedder, authority)
    users = b"# mine\n\nnot the core's bytes\n"
    mirror.write_bytes(users)
    shelf.remove("notes/a.md")
    volatile = BoundShelf(shelf.local_binding(), shelf).binding.palace_root / VOLATILE_NAME
    with sqlite3.connect(volatile) as conn:
        assert conn.execute(
            "UPDATE grants SET intended_mirror_sha256=? WHERE entry_id=?", (_sha(users), entry_id)
        ).rowcount
    out = _bring(shelf, settings, embedder, entry_id, authority)
    assert (out.outcome(entry_id).outcome, out.outcome(entry_id).code) == (
        "pending",
        "origin-unproved",
    )
    assert out.accepted == () and mirror.read_bytes() == users and _retained(mirror) == []
    assert pending_stages(BoundShelf(shelf.local_binding(), shelf)).stages == ()


class _CurrentHead(_Origin):
    """A caller whose promotion and recheck consult its CURRENT signed membership: a
    stage for an entry its head no longer holds is never promoted on old authorization."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse_publish = False
        self.release = False
        self.members: set[str] = set()

    def promote(self, stage, verified):
        if stage.entry.action != "remove" and stage.entry.entry_id not in self.members:
            raise StageRefused("entry-not-current")
        return super().promote(stage, verified)


def _hits(shelf, embedder) -> set[str]:
    from biblios.testing import OverlapReranker

    found = shelf.search("lighthouse", embedder=embedder, reranker=OverlapReranker())
    return {hit.entry_id for hit in found.hits}


def test_a_removed_entrys_pending_publication_is_never_promoted_served_or_revived(shelf):
    """Failure mode: after an entry is removed, its persisted first-publication stage is
    promoted and served on the old authorization, reported current or searchable, or
    dropped without the caller's decision; or the first mirror is retired while that
    stage is unresolved."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    caller = _CurrentHead()
    entry_id = shelf.add("notes/a.md", b"# A\n\nlighthouse\n").affected_entry_ids[0]
    caller.members.add(entry_id)
    assert _bring(shelf, settings, embedder, entry_id, caller).outcome(entry_id).code == "held"
    bound = BoundShelf(shelf.local_binding(), shelf)
    (pending,) = pending_stages(bound).stages
    mirror = shelf.mirror_path(entry_id)
    staged = mirror.read_bytes()
    shelf.remove("notes/a.md")
    caller.members.discard(entry_id)
    caller.release = True  # releasing promotion is no current authorization
    for _ in range(2):
        out = _bring(shelf, settings, embedder, entry_id, caller)
        assert (out.outcome(entry_id).outcome, out.outcome(entry_id).code) == (
            "pending",
            "entry-removed",
        )
        assert out.accepted == () and caller.promoted == []
        assert pending_stages(bound).stages == (pending,) and mirror.read_bytes() == staged
        assert _retained(mirror) == [] and entry_id not in _hits(shelf, embedder)
    # Only the caller's own decision disposes of the stage; the origin-checked removal of
    # the core's own first mirror follows.
    from biblios import discard_stage

    assert discard_stage(bound, entry_id, pending.stage_sha256)
    done = _bring(shelf, settings, embedder, entry_id, caller)
    assert done.outcome(entry_id).outcome == "removed" and not mirror.exists()
    assert [path.read_bytes() for path in _retained(mirror)] == [staged]
    assert caller.promoted == [done.accepted[0].stage_sha256]
    assert entry_id not in _hits(shelf, embedder)
